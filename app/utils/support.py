"""Lógica del chat de soporte.

Todo lo que no sea HTTP vive aquí para que las rutas del cliente
(`routes/support.py`) y las del admin (`routes/admin_support.py`) compartan
las mismas reglas: sin esto, el límite de mensajes o el auto-etiquetado
acabarían implementados dos veces y divergiendo.

La decisión de fondo: para abrir un chat el cliente solo escribe su nombre.
Un nombre no verifica nada — van a llegar tres "José" —, así que la
identificación se arma por otros dos caminos: el contexto que la propia
página aporta sin preguntar (número de orden si escribe desde el estado de
su pedido, contacto recordado del último checkout) y, cuando eso no
alcanza, el admin vinculando la orden y etiquetando a mano.
"""

import logging
import os
import re
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import func
from werkzeug.utils import secure_filename

from ..models import (
    db, Order, SupportChat, SupportChatTag, SupportMessage, SupportTag,
)
from .timezone import now_ve_naive

logger = logging.getLogger(__name__)

# ─── Límites ─────────────────────────────────────────────────────────────────
#
# El formulario pasó de tres campos obligatorios a uno solo. Esa fricción
# funcionaba como filtro anti-basura sin que nadie la hubiera diseñado para
# eso, así que al quitarla hay que reponer el filtro explícitamente.
#
# Los contadores se calculan con consultas, no en memoria: con `gunicorn -w 3`
# un contador de proceso deja pasar el triple de lo que dice permitir.

MAX_NAME_LENGTH = 40
MIN_NAME_LENGTH = 2
MAX_BODY_LENGTH = 2000
MAX_NEW_CHATS_PER_IP_PER_HOUR = 3
MAX_MESSAGES_PER_CHAT_PER_MINUTE = 10
CLOSED_CHAT_RETENTION_DAYS = 90

OPEN_STATUSES = ('open', 'waiting_client')

# Caracteres de control y marcas de dirección: invisibles al leer, pero
# sirven para falsear un nombre en la bandeja del admin.
_INVISIBLE_CHARS = re.compile(r'[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]')
_WHITESPACE_RUN = re.compile(r'\s+')


class SupportError(Exception):
    """Fallo esperable que la ruta traduce a un mensaje para el cliente."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ─── Saneado de entrada ──────────────────────────────────────────────────────

def clean_name(raw):
    name = _INVISIBLE_CHARS.sub('', str(raw or ''))
    name = _WHITESPACE_RUN.sub(' ', name).strip()
    if len(name) < MIN_NAME_LENGTH:
        raise SupportError('Escribe tu nombre para iniciar el chat.')
    return name[:MAX_NAME_LENGTH]


def clean_body(raw, allow_empty=False):
    body = _INVISIBLE_CHARS.sub('', str(raw or ''))
    body = body.replace('\r\n', '\n').strip()
    if not body and not allow_empty:
        raise SupportError('Escribe un mensaje.')
    return body[:MAX_BODY_LENGTH]


def _clean_context_value(raw, limit):
    value = _INVISIBLE_CHARS.sub('', str(raw or '')).strip()
    return value[:limit] if value else None


# ─── Contexto: lo que se sabe del cliente sin preguntarle ────────────────────

def apply_context(chat, context):
    """Guarda el contexto que la página aporta sola y, si trae número de
    orden, intenta vincular la orden real.

    Nada de esto es de fiar: viene del navegador y cualquiera puede mandar
    el número de orden de otro. Por eso el número solo sirve para *sugerir*
    la orden, y los datos sensibles del pedido no se le devuelven nunca al
    cliente — solo se le muestran al admin, que decide.
    """
    context = context if isinstance(context, dict) else {}

    chat.context_order_number = _clean_context_value(context.get('order_number'), 20)
    chat.context_player_id = _clean_context_value(context.get('player_id'), 100)
    chat.context_email = _clean_context_value(context.get('email'), 255)
    chat.context_phone = _clean_context_value(context.get('phone'), 50)
    chat.context_game = _clean_context_value(context.get('game'), 120)
    chat.context_package = _clean_context_value(context.get('package'), 160)
    chat.context_page = _clean_context_value(context.get('page'), 255)

    if chat.context_order_number:
        order = Order.query.filter_by(order_number=chat.context_order_number).first()
        if order:
            chat.order_id = order.id
            if order.user_id:
                chat.user_id = order.user_id


def find_matching_orders(chat, limit=5):
    """Órdenes que probablemente sean de quien escribe. Solo para el panel
    del admin: es una sugerencia para vincular, no una identificación."""
    filters = []
    if chat.context_order_number:
        filters.append(Order.order_number == chat.context_order_number)
    if chat.context_email:
        filters.append(func.lower(Order.email) == chat.context_email.lower())
    if chat.context_phone:
        filters.append(Order.phone == chat.context_phone)
    if chat.context_player_id:
        filters.append(Order.player_id == chat.context_player_id)

    if not filters:
        return []

    from sqlalchemy import or_
    return (Order.query
            .filter(or_(*filters))
            .order_by(Order.created_at.desc())
            .limit(limit)
            .all())


def search_orders(term, limit=10):
    """Buscador manual del hilo: número de orden, ID de jugador, correo o
    teléfono. Es la vía principal para identificar a quien llegó sin
    contexto alguno."""
    term = str(term or '').strip()
    if len(term) < 3:
        return []

    from sqlalchemy import or_
    like = f'%{term}%'
    return (Order.query
            .filter(or_(
                Order.order_number.ilike(like),
                Order.player_id.ilike(like),
                Order.email.ilike(like),
                Order.phone.ilike(like),
                Order.player_nickname.ilike(like),
            ))
            .order_by(Order.created_at.desc())
            .limit(limit)
            .all())


# ─── Etiquetas ───────────────────────────────────────────────────────────────

def get_tag(slug):
    return SupportTag.query.filter_by(slug=slug).first()


def add_tag(chat, tag, admin_id=None):
    """Pega una etiqueta al chat. Idempotente: el UNIQUE de la tabla puente
    ya lo impide a nivel de BD, pero comprobarlo antes evita ensuciar la
    sesión con un IntegrityError en el camino normal."""
    if not tag:
        return False
    existing = SupportChatTag.query.filter_by(chat_id=chat.id, tag_id=tag.id).first()
    if existing:
        return False
    db.session.add(SupportChatTag(chat_id=chat.id, tag_id=tag.id, admin_id=admin_id))
    return True


def remove_tag(chat, tag):
    if not tag:
        return False
    deleted = SupportChatTag.query.filter_by(chat_id=chat.id, tag_id=tag.id).delete()
    return bool(deleted)


def sync_unidentified_tag(chat):
    """"Sin identificar" es la cola de trabajo del admin, así que se
    mantiene sola: entra cuando no se sabe de quién es el chat y sale en
    cuanto se vincula una orden o una cuenta."""
    tag = get_tag('sin-identificar')
    if not tag:
        return
    if chat.is_identified:
        remove_tag(chat, tag)
    else:
        add_tag(chat, tag)


def apply_auto_tags(chat):
    """Etiqueta de error deducida del estado de la orden vinculada.

    Solo corre al abrir el chat y nunca pisa una etiqueta de error que ya
    esté puesta: si el admin ya clasificó el caso, su criterio manda sobre
    la deducción automática.
    """
    sync_unidentified_tag(chat)

    if not chat.order_id or chat.tags_of_kind('error'):
        return

    order = chat.order or Order.query.get(chat.order_id)
    if not order:
        return

    created = order.created_at or datetime.utcnow()
    stale = (datetime.utcnow() - created) > timedelta(minutes=30)

    slug = None
    if order.status == 'rejected':
        slug = 'pago-no-verificado'
    elif order.status == 'pending' and stale:
        slug = 'pago-no-verificado'
    elif order.status == 'approved' and stale:
        slug = 'recarga-no-llego'

    if slug:
        add_tag(chat, get_tag(slug))


def link_order(chat, order, admin_id=None):
    """Vincula una orden al chat desde el admin y deja constancia en el
    propio hilo — un mensaje de sistema, para que quede el rastro de
    cuándo se supo de quién era este chat."""
    chat.order_id = order.id
    if order.user_id:
        chat.user_id = order.user_id
    sync_unidentified_tag(chat)
    add_system_message(chat, f'Orden {order.order_number} vinculada a este chat.')
    db.session.commit()
    return chat


# ─── Chats ───────────────────────────────────────────────────────────────────

def get_chat_by_token(token):
    token = str(token or '').strip()
    if not token or len(token) > 64:
        return None
    return SupportChat.query.filter_by(public_token=token).first()


def _count_recent_chats_from_ip(client_ip):
    if not client_ip:
        return 0
    since = datetime.utcnow() - timedelta(hours=1)
    return (SupportChat.query
            .filter(SupportChat.client_ip == client_ip,
                    SupportChat.created_at >= since)
            .count())


def start_chat(name, context=None, client_ip=None, user_agent=None, user_id=None):
    """Abre el chat. El nombre es lo único que se pide."""
    name = clean_name(name)

    if _count_recent_chats_from_ip(client_ip) >= MAX_NEW_CHATS_PER_IP_PER_HOUR:
        raise SupportError(
            'Abriste varios chats seguidos. Espera un momento o continúa en el que ya tienes abierto.',
            status=429,
        )

    chat = SupportChat(
        client_name=name,
        client_ip=(client_ip or '')[:45] or None,
        user_agent=(user_agent or '')[:255] or None,
        user_id=user_id,
        status='open',
        last_message_at=datetime.utcnow(),
    )
    db.session.add(chat)
    db.session.flush()  # necesita id para las etiquetas

    apply_context(chat, context)
    apply_auto_tags(chat)

    add_system_message(chat, f'{name} inició un chat de soporte.')
    db.session.commit()
    return chat


def reopen_or_start(token, name, context=None, client_ip=None, user_agent=None, user_id=None):
    """Un navegador tiene un solo chat vivo: si ya lo tiene, se reabre en
    vez de crear otro. Sin esto, cerrar y volver a abrir el modal llenaría
    la bandeja de hilos vacíos del mismo cliente."""
    chat = get_chat_by_token(token)
    if chat and not chat.is_blocked and chat.status in OPEN_STATUSES:
        return chat, False
    return start_chat(name, context, client_ip, user_agent, user_id), True


def _messages_in_last_minute(chat):
    since = datetime.utcnow() - timedelta(minutes=1)
    return (SupportMessage.query
            .filter(SupportMessage.chat_id == chat.id,
                    SupportMessage.sender == 'client',
                    SupportMessage.created_at >= since)
            .count())


def add_system_message(chat, body):
    """Nota automática del sistema. No cuenta como no leída para nadie:
    nadie tiene que responderle al sistema."""
    message = SupportMessage(chat_id=chat.id, sender='system', body=body)
    db.session.add(message)
    return message


def add_client_message(chat, body, attachment=None):
    if chat.is_blocked:
        raise SupportError('Este chat fue cerrado por el equipo de soporte.', status=403)

    body = clean_body(body, allow_empty=bool(attachment))

    if _messages_in_last_minute(chat) >= MAX_MESSAGES_PER_CHAT_PER_MINUTE:
        raise SupportError('Vas muy rápido. Espera unos segundos antes de enviar otro mensaje.',
                           status=429)

    message = SupportMessage(chat_id=chat.id, sender='client', body=body, attachment=attachment)
    db.session.add(message)

    # Un mensaje del cliente reabre el hilo: si el admin lo había cerrado y
    # el problema seguía, vuelve a la bandeja en vez de perderse.
    if chat.status == 'closed':
        chat.closed_at = None
    chat.status = 'open'
    chat.unread_admin = (chat.unread_admin or 0) + 1
    chat.last_message_at = datetime.utcnow()

    db.session.commit()
    return message


def add_admin_message(chat, body, admin_id=None, attachment=None):
    body = clean_body(body, allow_empty=bool(attachment))
    message = SupportMessage(chat_id=chat.id, sender='admin', body=body,
                             admin_id=admin_id, attachment=attachment)
    db.session.add(message)

    chat.status = 'waiting_client'
    chat.unread_client = (chat.unread_client or 0) + 1
    chat.unread_admin = 0
    chat.last_message_at = datetime.utcnow()

    db.session.commit()
    return message


def close_chat(chat, by_admin=False, admin_id=None):
    chat.status = 'closed'
    chat.closed_at = datetime.utcnow()
    chat.unread_admin = 0
    add_system_message(chat, 'El chat fue cerrado por soporte.' if by_admin
                       else 'El cliente cerró el chat.')
    db.session.commit()
    return chat


def reopen_chat(chat):
    chat.status = 'open'
    chat.closed_at = None
    add_system_message(chat, 'El chat fue reabierto.')
    db.session.commit()
    return chat


MAX_NOTE_LENGTH = 4000


def save_admin_note(chat, text, admin_id=None):
    """Guarda (o borra, si llega vacía) la nota interna del chat.

    Es lo que el admin escribe para acordarse del caso: de qué orden era y
    qué falló de verdad. Las etiquetas clasifican; la nota cuenta la
    historia que ninguna etiqueta del catálogo cubre.
    """
    text = _INVISIBLE_CHARS.sub('', str(text or ''))
    text = text.replace('\r\n', '\n').strip()[:MAX_NOTE_LENGTH]

    chat.admin_note = text or None
    chat.admin_note_at = datetime.utcnow() if text else None
    chat.admin_note_admin_id = admin_id if text else None
    db.session.commit()
    return chat


def mark_read_by_admin(chat):
    if not chat.unread_admin:
        return
    chat.unread_admin = 0
    db.session.commit()


def mark_read_by_client(chat):
    if not chat.unread_client:
        return
    chat.unread_client = 0
    db.session.commit()


def pending_chats_count():
    """Para el badge del sidebar del admin."""
    return (SupportChat.query
            .filter(SupportChat.unread_admin > 0,
                    SupportChat.is_blocked.isnot(True))
            .count())


# ─── Adjuntos ────────────────────────────────────────────────────────────────

def save_attachment(file):
    """Misma validación y carpeta que los comprobantes del checkout, pero
    en su propio subdirectorio para que una purga de soporte no roce jamás
    una captura de pago."""
    if not file or not file.filename:
        return None

    allowed = current_app.config.get('ALLOWED_IMAGE_EXTENSIONS') or set()
    name = str(file.filename)
    if '.' not in name or name.rsplit('.', 1)[1].lower() not in allowed:
        raise SupportError('Solo se aceptan imágenes.')

    filename = f"{now_ve_naive().strftime('%Y%m%d%H%M%S%f')}_{secure_filename(name)}"
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], 'support')
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, filename))
    return 'support/' + filename


# ─── Serialización ───────────────────────────────────────────────────────────

def serialize_message(message):
    return {
        'id': message.id,
        'sender': message.sender,
        'body': message.body or '',
        'attachment': message.attachment or '',
        'created_at': (message.created_at or datetime.utcnow()).isoformat() + 'Z',
    }


def serialize_chat_for_client(chat):
    """Lo que ve el cliente. Deliberadamente escueto: ni etiquetas, ni
    datos de la orden, ni notas internas. Las etiquetas son el cuaderno de
    trabajo del admin y no tienen por qué ser públicas — algunas ("Reincidente")
    serían una grosería si el cliente las viera."""
    return {
        'code': chat.display_code,
        'name': chat.client_name,
        'status': chat.status,
        'status_label': chat.status_label,
    }


# ─── Mantenimiento ───────────────────────────────────────────────────────────

def purge_old_chats(days=CLOSED_CHAT_RETENTION_DAYS):
    """Borra chats cerrados con más de `days` días. La cascada del modelo
    se lleva mensajes y etiquetas aplicadas; el catálogo no se toca."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    stale = (SupportChat.query
             .filter(SupportChat.status == 'closed',
                     SupportChat.closed_at.isnot(None),
                     SupportChat.closed_at < cutoff)
             .all())

    removed = 0
    for chat in stale:
        for message in chat.messages:
            if not message.attachment:
                continue
            path = os.path.join(current_app.config.get('UPLOAD_FOLDER', ''), message.attachment)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                logger.warning('No se pudo borrar el adjunto de soporte %s', path)
        db.session.delete(chat)
        removed += 1

    if removed:
        db.session.commit()
    return removed
