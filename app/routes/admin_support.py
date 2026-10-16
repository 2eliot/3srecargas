"""Bandeja de soporte del admin.

Va en su propio módulo y no dentro de `admin.py`, que ya pasa de las 3800
líneas. Cuelga de `/admin/soporte` con su propio guardián de acceso: el
`before_request` de `admin_bp` solo protege a su blueprint, no a este.
"""

import hashlib
import json
import re
from datetime import datetime

from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request, url_for
)
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from ..models import db, Order, Setting, SupportChat, SupportChatTag, SupportMessage, SupportQuickReply, SupportTag
from ..utils import support as support_service
from ..utils import support_bot
from ..utils.support import SupportError
from ..utils.support_ai import (
    ai_replies_last_24h, gemini_key_configured, get_ai_settings, save_ai_settings, test_connection as test_support_ai
)
from ..utils.notifications import notify_support_admin_reply
from ..utils.timezone import format_ve, now_ve, to_ve
from .admin import delete_uploaded_file

admin_support_bp = Blueprint('admin_support_bp', __name__, url_prefix='/admin/soporte')

INBOX_PAGE_SIZE = 40
INBOX_MAX_LIMIT = 200


@admin_support_bp.before_request
def guard():
    if not current_user.is_authenticated:
        return None  # lo resuelve @login_required + el unauthorized_handler
    if current_user.__class__.__name__ != 'AdminUser':
        flash('Esta sección es solo para administradores.', 'warning')
        return redirect(url_for('main_bp.index'))
    return None


def _admin_id():
    return current_user.id if current_user.is_authenticated else None


def _clean_color(value, fallback):
    """Color de etiqueta: solo #rgb / #rrggbb / #rrggbbaa (va dentro de un style=\"...\"); cualquier otra cosa -> el anterior."""
    value = (value or '').strip()
    return value if re.fullmatch(r'#[0-9a-fA-F]{3,8}', value) else (fallback or '#6c5ce7')


def _slugify(value):
    text = ''.join(c.lower() if c.isalnum() else '-' for c in str(value or '').strip())
    while '--' in text:
        text = text.replace('--', '-')
    return text.strip('-')[:60] or 'etiqueta'


# ─── Bandeja ─────────────────────────────────────────────────────────────────

def _is_embed():
    """El chat se muestra dentro de la bandeja (iframe) sin el menú ni la
    barra del admin. Los navegadores avisan `Sec-Fetch-Dest: iframe` en cada
    paso, incluidas las redirecciones, así que no se pierde al guardar algo."""
    return request.args.get('embed') == '1' or request.headers.get('Sec-Fetch-Dest', '').lower() == 'iframe'


# Pestañas de la bandeja: "En atención" es donde te toca contestar, "En espera"
# donde ya contestaste y falta el cliente, "Resueltos" los cerrados.
INBOX_TABS = ('open', 'waiting_client', 'closed')


def _filtered_chats(status, tag_slug, term, only_unread=False):
    query = SupportChat.query.options(
        joinedload(SupportChat.tag_links).joinedload(SupportChatTag.tag)
    )

    if status == 'active':
        query = query.filter(SupportChat.status.in_(support_service.OPEN_STATUSES))
    elif status == 'unread':
        query = query.filter(SupportChat.unread_admin > 0)
    elif status in INBOX_TABS:
        query = query.filter(SupportChat.status == status)
    if only_unread:
        query = query.filter(SupportChat.unread_admin > 0)

    if tag_slug:
        tag = support_service.get_tag(tag_slug)
        if tag:
            query = query.filter(SupportChat.tag_links.any(SupportChatTag.tag_id == tag.id))

    if term:
        like = f'%{term}%'
        query = query.filter(or_(
            SupportChat.client_name.ilike(like),
            SupportChat.short_code.ilike(like),
            SupportChat.context_order_number.ilike(like),
            SupportChat.context_email.ilike(like),
            SupportChat.context_player_id.ilike(like),
            SupportChat.admin_note.ilike(like),
        ))
    return query


def _last_messages(chat_ids):
    """Último mensaje de cada chat con dos consultas (no una por chat)."""
    if not chat_ids:
        return {}
    latest_ids = [row[0] for row in (
        db.session.query(func.max(SupportMessage.id))
        .filter(SupportMessage.chat_id.in_(chat_ids))
        .group_by(SupportMessage.chat_id).all())]
    messages = SupportMessage.query.filter(SupportMessage.id.in_(latest_ids)).all() if latest_ids else []
    return {m.chat_id: m for m in messages}


def _preview(message):
    """Texto de una línea para la lista, con quién habló al principio."""
    if message is None:
        return ''
    if message.is_deleted:
        text = '🚫 Mensaje eliminado'
    elif message.body:
        text = ' '.join(message.body.split())
    elif message.attachment:
        text = '🎥 Video' if support_service.is_video_attachment(message.attachment) else '📷 Foto'
    else:
        text = ''
    text = text[:100]
    if message.sender == 'admin' and message.is_ai:
        return '🤖 ' + text
    if message.sender == 'admin':
        return 'Tú: ' + text
    return text


def _row_time(moment):
    """Hora si fue hoy, "Ayer" o día/mes (horario de Venezuela)."""
    local = to_ve(moment)
    if not local:
        return ''
    days = (now_ve().date() - local.date()).days
    if days <= 0:
        return local.strftime('%H:%M')
    if days == 1:
        return 'Ayer'
    return local.strftime('%d/%m')


def _chat_row(chat, last_message):
    return {
        'id': chat.id,
        'name': chat.client_name,
        'code': chat.display_code,
        'preview': _preview(last_message),
        'unread': int(chat.unread_admin or 0),
        'status': chat.status,
        'time': _row_time(chat.last_message_at),
        'at': (chat.last_message_at.isoformat() + 'Z') if chat.last_message_at else '',
        'tags': [{'name': t.name, 'color': t.color} for t in chat.tags],
        'order': chat.order.order_number if chat.order else '',
        'has_note': bool(chat.admin_note),
        'blocked': bool(chat.is_blocked),
        'ai': bool(chat.ai_active) and chat.status in support_service.OPEN_STATUSES,
        'url': url_for('admin_support_bp.detail', chat_id=chat.id),
    }


def _tab_counts():
    rows = dict(db.session.query(SupportChat.status, func.count(SupportChat.id)).group_by(SupportChat.status).all())
    return {
        'open': int(rows.get('open', 0)),
        'waiting_client': int(rows.get('waiting_client', 0)),
        'closed': int(rows.get('closed', 0)),
    }


def _inbox_payload(status, tag_slug, term, only_unread, limit):
    query = _filtered_chats(status, tag_slug, term, only_unread)
    total = query.count()
    # Más recientes arriba, por la hora del último mensaje: un chat que acabas
    # de responder no se hunde debajo de otros solo por tener menos sin leer.
    chats = query.order_by(SupportChat.last_message_at.desc(), SupportChat.id.desc()).limit(limit).all()
    last = _last_messages([c.id for c in chats])
    return {
        'ok': True,
        'chats': [_chat_row(c, last.get(c.id)) for c in chats],
        'total': total,
        'has_more': total > len(chats),
        'counts': _tab_counts(),
        'pending': support_service.pending_chats_count(),
    }


def _inbox_filters():
    status = (request.args.get('status') or 'open').strip()
    if status not in INBOX_TABS + ('active', 'unread', 'all'):
        status = 'open'
    limit = min(max(request.args.get('limit', type=int) or INBOX_PAGE_SIZE, 10), INBOX_MAX_LIMIT)
    return (
        status,
        (request.args.get('tag') or '').strip(),
        (request.args.get('q') or '').strip(),
        request.args.get('unread') == '1',
        limit,
    )


@admin_support_bp.route('/')
@login_required
def inbox():
    """Bandeja a la izquierda y el chat a la derecha. La lista se vuelve a
    pedir sola (bandeja.json) para que los chats lleguen en vivo."""
    status, tag_slug, term, only_unread, limit = _inbox_filters()
    selected = request.args.get('chat', type=int)
    if selected and not SupportChat.query.get(selected):
        selected = None

    return render_template(
        'admin/support.html',
        status=status,
        tag_slug=tag_slug,
        term=term,
        only_unread=only_unread,
        selected_chat=selected,
        initial=_inbox_payload(status, tag_slug, term, only_unread, limit),
        tags=SupportTag.query.filter_by(is_active=True)
                      .order_by(SupportTag.kind, SupportTag.sort_order).all(),
    )


@admin_support_bp.route('/bandeja.json')
@login_required
def inbox_json():
    """Sondeo de la bandeja. Si nada cambió desde la última vez (`v` es la
    huella que devolvió) responde casi vacío, así pedirla cada pocos segundos
    cuesta poco."""
    payload = _inbox_payload(*_inbox_filters())
    version = hashlib.md5(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()
    if request.args.get('v') == version:
        return jsonify({'ok': True, 'unchanged': True, 'version': version})
    payload['version'] = version
    return jsonify(payload)


@admin_support_bp.route('/panel-vacio')
@login_required
def empty_pane():
    """Lo que se ve a la derecha mientras no hay un chat elegido."""
    return render_template('admin/support_empty.html', embed=True)


# ─── Hilo ────────────────────────────────────────────────────────────────────

@admin_support_bp.route('/<int:chat_id>')
@login_required
def detail(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    support_service.mark_read_by_admin(chat)

    # La foto del equipo en las burbujas: la de Configuración, o el logo si no hay.
    avatar_row = Setting.query.filter_by(key='support_avatar').first()
    support_photo = (url_for('static', filename='uploads/' + avatar_row.value)
                     if avatar_row and avatar_row.value else url_for('static', filename='pwa/icon-192.png'))

    return render_template(
        'admin/support_detail.html',
        support_photo=support_photo,
        chat=chat,
        client_expired=support_service.client_view_expired(chat),
        client_visible_hours=support_service.CLIENT_VISIBLE_HOURS,
        embed=_is_embed(),
        messages=[support_service.serialize_message(m) for m in chat.messages],
        suggested_orders=support_service.find_matching_orders(chat),
        recent_orders=(Order.query
                       .filter(Order.player_id == chat.order.player_id)
                       .order_by(Order.created_at.desc())
                       .limit(5).all()) if chat.order and chat.order.player_id else [],
        user_tags=SupportTag.query.filter_by(kind='user', is_active=True)
                            .order_by(SupportTag.sort_order).all(),
        error_tags=SupportTag.query.filter_by(kind='error', is_active=True)
                             .order_by(SupportTag.sort_order).all(),
        applied_tag_ids={t.id for t in chat.tags},
        support_signature=support_service.get_support_signature(),
        quick_replies=SupportQuickReply.query
                             .order_by(SupportQuickReply.sort_order, SupportQuickReply.id).all(),
    )


@admin_support_bp.route('/<int:chat_id>/hilo.json')
@login_required
def thread_json(chat_id):
    """Sondeo de la vista del hilo. Solo mensajes nuevos, más los IDs
    borrados hace poco (para actualizar burbujas que ya estaban pintadas
    antes de que alguien las eliminara)."""
    chat = SupportChat.query.get_or_404(chat_id)
    after_id = request.args.get('after_id', type=int) or 0

    query = SupportMessage.query.filter(SupportMessage.chat_id == chat.id)
    if after_id:
        query = query.filter(SupportMessage.id > after_id)
    messages = query.order_by(SupportMessage.id).all()

    if messages:
        support_service.mark_read_by_admin(chat)

    return jsonify({
        'ok': True,
        'status': chat.status,
        'status_label': chat.status_label,
        'messages': [support_service.serialize_message(m) for m in messages],
        'deleted_ids': support_service.recently_deleted_ids(chat),
    })


@admin_support_bp.route('/pendientes.json')
@login_required
def pending_json():
    """Badge del menú lateral, y de paso el barrido de chats inactivos.

    Aprovecha este sondeo en vez de un cron: si nadie está mirando el
    panel tampoco urge cerrar hilos, y cuando abres la bandeja ya llega
    barrida.
    """
    support_service.close_idle_chats()
    return jsonify({'ok': True, 'pending': support_service.pending_chats_count()})


@admin_support_bp.route('/<int:chat_id>/responder', methods=['POST'])
@login_required
def reply(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    data = request.get_json(silent=True) or request.form

    try:
        attachment = support_service.save_attachment(request.files.get('file'))
        message = support_service.add_admin_message(
            chat, data.get('body'), admin_id=_admin_id(), attachment=attachment
        )
    except SupportError as exc:
        return jsonify({'ok': False, 'error': exc.message}), exc.status

    notify_support_admin_reply(chat, message)

    return jsonify({
        'ok': True,
        'status': chat.status,
        'status_label': chat.status_label,
        'message': support_service.serialize_message(message),
    })


@admin_support_bp.route('/<int:chat_id>/mensajes/<int:message_id>/borrar', methods=['POST'])
@login_required
def delete_message(chat_id, message_id):
    """Eliminar para todos: por si el admin se equivocó en algo que mandó y
    quiere borrarlo antes de que el cliente lo lea."""
    chat = SupportChat.query.get_or_404(chat_id)
    message = SupportMessage.query.filter_by(id=message_id, chat_id=chat.id).first_or_404()

    try:
        support_service.delete_message(message, admin_id=_admin_id())
    except SupportError as exc:
        return jsonify({'ok': False, 'error': exc.message}), exc.status

    return jsonify({'ok': True, 'message': support_service.serialize_message(message)})


# ─── Etiquetas aplicadas ─────────────────────────────────────────────────────

@admin_support_bp.route('/<int:chat_id>/etiqueta', methods=['POST'])
@login_required
def toggle_tag(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    data = request.get_json(silent=True) or request.form

    try:
        tag_id = int(data.get('tag_id') or 0)
    except (TypeError, ValueError):
        tag_id = 0

    tag = SupportTag.query.get(tag_id) if tag_id else None
    if not tag:
        return jsonify({'ok': False, 'error': 'Etiqueta no encontrada.'}), 404

    if str(data.get('action') or 'add') == 'remove':
        support_service.remove_tag(chat, tag)
        applied = False
    else:
        support_service.add_tag(chat, tag, admin_id=_admin_id())
        applied = True

    db.session.commit()
    return jsonify({'ok': True, 'tag_id': tag.id, 'applied': applied})


# ─── Nota interna ────────────────────────────────────────────────────────────

@admin_support_bp.route('/<int:chat_id>/nota', methods=['POST'])
@login_required
def save_note(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    data = request.get_json(silent=True) or request.form

    support_service.save_admin_note(chat, data.get('note'), admin_id=_admin_id())

    return jsonify({
        'ok': True,
        'has_note': bool(chat.admin_note),
        'saved_at': format_ve(chat.admin_note_at) if chat.admin_note_at else '',
        'author': chat.admin_note_author.username if chat.admin_note_author else '',
    })


# ─── Vinculación con una orden ───────────────────────────────────────────────

@admin_support_bp.route('/<int:chat_id>/buscar-ordenes')
@login_required
def search_orders(chat_id):
    SupportChat.query.get_or_404(chat_id)
    orders = support_service.search_orders(request.args.get('q'))
    return jsonify({'ok': True, 'orders': [{
        'id': o.id,
        'order_number': o.order_number,
        'player_id': o.player_id or '',
        'nickname': o.player_nickname or '',
        'game': o.game.name if o.game else '',
        'package': o.package.name if o.package else '',
        'status': o.status,
        'status_label': o.status_label,
        'email': o.email or '',
        'created_at': (o.created_at or datetime.utcnow()).isoformat() + 'Z',
    } for o in orders]})


@admin_support_bp.route('/<int:chat_id>/vincular', methods=['POST'])
@login_required
def link_order(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    data = request.get_json(silent=True) or request.form

    order = Order.query.get(data.get('order_id'))
    if not order:
        return jsonify({'ok': False, 'error': 'Orden no encontrada.'}), 404

    support_service.link_order(chat, order, admin_id=_admin_id())
    return jsonify({'ok': True, 'order_number': order.order_number})


@admin_support_bp.route('/<int:chat_id>/desvincular', methods=['POST'])
@login_required
def unlink_order(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    chat.order_id = None
    chat.user_id = None
    support_service.sync_unidentified_tag(chat)
    support_service.add_system_message(chat, 'Se desvinculó la orden de este chat.')
    db.session.commit()
    return jsonify({'ok': True})


# ─── Estado del chat ─────────────────────────────────────────────────────────

@admin_support_bp.route('/<int:chat_id>/estado', methods=['POST'])
@login_required
def set_status(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    action = (request.form.get('action') or (request.get_json(silent=True) or {}).get('action') or '').strip()

    if action == 'cerrar':
        support_service.close_chat(chat, by_admin=True, admin_id=_admin_id())
    elif action == 'reabrir':
        support_service.reopen_chat(chat)
    elif action == 'bloquear':
        # Corta a un insistente sin tocar la IP: bloquear por IP en
        # Venezuela suele llevarse por delante a media zona detrás del
        # mismo CGNAT del operador.
        chat.is_blocked = True
        chat.status = 'closed'
        chat.closed_at = datetime.utcnow()
        chat.unread_admin = 0
        db.session.commit()
    elif action == 'desbloquear':
        chat.is_blocked = False
        db.session.commit()
    elif action == 'ia_encender':
        # Devolverle el chat al asistente: vuelve a contar de cero los
        # intentos fallidos.
        chat.ai_active = True
        chat.ai_failed = 0
        support_service.add_system_message(chat, 'El equipo devolvió este chat al asistente virtual.')
        db.session.commit()
    elif action == 'ia_apagar':
        chat.ai_active = False
        support_service.add_system_message(chat, 'Un agente del equipo tomó este chat.')
        db.session.commit()
    else:
        return jsonify({'ok': False, 'error': 'Acción desconocida.'}), 400

    if request.is_json:
        return jsonify({'ok': True, 'status': chat.status, 'blocked': bool(chat.is_blocked)})
    # Cerrar o bloquear termina la conversación: vuelve a la bandeja en vez
    # de dejar al admin mirando un chat que ya no tiene nada más que hacer.
    # Reabrir/desbloquear sí se quedan en el hilo, porque lo lógico es
    # seguir trabajando ahí mismo.
    if action in ('cerrar', 'bloquear'):
        # Dentro de la bandeja, el panel del chat queda vacío (la lista ya
        # está al lado); abierto solo, vuelve a la bandeja.
        return redirect(url_for('admin_support_bp.empty_pane') if _is_embed() else url_for('admin_support_bp.inbox'))
    return redirect(url_for('admin_support_bp.detail', chat_id=chat.id))


# ─── Catálogo de etiquetas ───────────────────────────────────────────────────

@admin_support_bp.route('/asistente', methods=['GET'])
@login_required
def assistant():
    """Botones y saludo del chat + asistente de IA (con sus instrucciones)."""
    return render_template(
        'admin/support_assistant.html',
        support_ai=get_ai_settings(),
        ai_replies_24h=ai_replies_last_24h(),
        gemini_key_ok=gemini_key_configured(),
        bot=support_bot.get_bot_settings(),
        bot_buttons=support_bot.BOT_BUTTONS,
        support_tags=SupportTag.query.filter_by(is_active=True).order_by(SupportTag.sort_order, SupportTag.name).all(),
    )


@admin_support_bp.route('/asistente/guardar', methods=['POST'])
@login_required
def assistant_save():
    tag_id = request.form.get('handoff_tag_id', type=int)
    if tag_id and not SupportTag.query.get(tag_id):
        tag_id = None
    provider = request.form.get('provider')
    api_key = request.form.get('api_key') or ''
    custom_key = request.form.get('custom_api_key') or ''
    try:
        save_ai_settings(
            enabled=request.form.get('enabled') == '1',
            api_key=api_key,
            model=request.form.get('model'),
            handoff_tag_id=tag_id,
            max_failed=request.form.get('max_failed', type=int),
            daily_limit=request.form.get('daily_limit', type=int),
            instructions=request.form.get('instructions'),
            base_url=request.form.get('base_url'),
            provider=provider,
            custom_api_key=custom_key,
        )
    except ValueError as exc:
        # URL de la IA inválida: no se guarda nada, ni siquiera los botones.
        flash(str(exc), 'danger')
        return redirect(url_for('admin_support_bp.assistant'))
    support_bot.save_bot_settings(
        enabled=request.form.get('bot_enabled') == '1',
        name=request.form.get('bot_name'),
        greeting=request.form.get('bot_greeting'),
        buttons=request.form.getlist('bot_buttons'),
    )
    if request.form.get('test'):
        ok, message = test_support_ai(api_key=custom_key if provider == 'custom' else api_key)
        flash(('✅ ' if ok else '❌ ') + message, 'success' if ok else 'danger')
    else:
        flash('Asistente de soporte guardado.', 'success')
    return redirect(url_for('admin_support_bp.assistant'))


@admin_support_bp.route('/etiquetas', methods=['GET'])
@login_required
def tags():
    return render_template(
        'admin/support_tags.html',
        user_tags=SupportTag.query.filter_by(kind='user').order_by(SupportTag.sort_order).all(),
        error_tags=SupportTag.query.filter_by(kind='error').order_by(SupportTag.sort_order).all(),
        idle_minutes=support_service.get_idle_close_minutes(),
    )


@admin_support_bp.route('/etiquetas/inactividad', methods=['POST'])
@login_required
def set_idle_minutes():
    from ..models import Setting

    raw = (request.form.get('minutes') or '').strip()
    try:
        minutes = max(0, min(int(raw), 1440))
    except (TypeError, ValueError):
        flash('Pon un número de minutos.', 'danger')
        return redirect(url_for('admin_support_bp.tags'))

    row = Setting.query.filter_by(key=support_service.IDLE_CLOSE_SETTING_KEY).first()
    if not row:
        row = Setting(key=support_service.IDLE_CLOSE_SETTING_KEY,
                      description='Minutos sin respuesta del cliente antes de cerrar su chat')
        db.session.add(row)
    row.value = str(minutes)
    db.session.commit()

    flash('Cierre automático desactivado.' if minutes == 0
          else f'Los chats se cerrarán tras {minutes} minutos sin respuesta.', 'success')
    return redirect(url_for('admin_support_bp.tags'))


@admin_support_bp.route('/etiquetas/crear', methods=['POST'])
@login_required
def tag_create():
    name = (request.form.get('name') or '').strip()
    kind = (request.form.get('kind') or 'error').strip()

    if not name:
        flash('Ponle un nombre a la etiqueta.', 'danger')
        return redirect(url_for('admin_support_bp.tags'))
    if kind not in ('user', 'error'):
        kind = 'error'

    slug = _slugify(name)
    if SupportTag.query.filter_by(slug=slug).first():
        flash('Ya existe una etiqueta con ese nombre.', 'warning')
        return redirect(url_for('admin_support_bp.tags'))

    last = (SupportTag.query.filter_by(kind=kind)
            .order_by(SupportTag.sort_order.desc()).first())

    db.session.add(SupportTag(
        name=name[:60], slug=slug, kind=kind,
        color=_clean_color(request.form.get('color'), '#6c5ce7'),
        sort_order=(last.sort_order or 0) + 1 if last else 1,
    ))
    db.session.commit()
    flash('Etiqueta creada.', 'success')
    return redirect(url_for('admin_support_bp.tags'))


@admin_support_bp.route('/etiquetas/<int:tag_id>/editar', methods=['POST'])
@login_required
def tag_edit(tag_id):
    tag = SupportTag.query.get_or_404(tag_id)
    name = (request.form.get('name') or '').strip()
    if name:
        tag.name = name[:60]
    tag.color = _clean_color(request.form.get('color'), tag.color)
    tag.is_active = request.form.get('is_active') == 'on'
    db.session.commit()
    flash('Etiqueta actualizada.', 'success')
    return redirect(url_for('admin_support_bp.tags'))


@admin_support_bp.route('/etiquetas/<int:tag_id>/borrar', methods=['POST'])
@login_required
def tag_delete(tag_id):
    tag = SupportTag.query.get_or_404(tag_id)

    # "Sin identificar" la mantiene el sistema sola; borrarla dejaría al
    # admin sin la cola de chats por identificar y sin forma de recuperarla
    # salvo recreándola con el mismo slug exacto.
    if tag.slug == 'sin-identificar':
        flash('Esa etiqueta la usa el sistema y no se puede borrar. Puedes desactivarla.', 'warning')
        return redirect(url_for('admin_support_bp.tags'))

    SupportChatTag.query.filter_by(tag_id=tag.id).delete()
    db.session.delete(tag)
    db.session.commit()
    flash('Etiqueta borrada.', 'success')
    return redirect(url_for('admin_support_bp.tags'))


# ─── Firma de los mensajes ────────────────────────────────────────────────────

@admin_support_bp.route('/firma', methods=['POST'])
@login_required
def save_signature():
    text = support_service.save_support_signature(request.form.get('signature'))
    flash('Firma actualizada.' if text else 'Firma quitada.', 'success')
    return redirect(request.referrer or url_for('admin_support_bp.inbox'))


# ─── Respuestas rápidas ──────────────────────────────────────────────────────

@admin_support_bp.route('/respuestas-rapidas/crear', methods=['POST'])
@login_required
def quick_reply_create():
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    redirect_target = request.referrer or url_for('admin_support_bp.inbox')

    if not title or not (body or request.files.get('file')):
        flash('La respuesta rápida necesita un título y un texto o un adjunto.', 'danger')
        return redirect(redirect_target)

    try:
        attachment = support_service.save_attachment(request.files.get('file'))
    except SupportError as exc:
        flash(exc.message, 'danger')
        return redirect(redirect_target)

    last = SupportQuickReply.query.order_by(SupportQuickReply.sort_order.desc()).first()
    db.session.add(SupportQuickReply(
        title=title[:60], body=body[:2000], attachment=attachment,
        sort_order=(last.sort_order or 0) + 1 if last else 1,
    ))
    db.session.commit()
    flash('Respuesta rápida guardada.', 'success')
    return redirect(redirect_target)


@admin_support_bp.route('/respuestas-rapidas/<int:reply_id>/editar', methods=['POST'])
@login_required
def quick_reply_edit(reply_id):
    reply = SupportQuickReply.query.get_or_404(reply_id)
    title = (request.form.get('title') or '').strip()
    body = (request.form.get('body') or '').strip()
    remove_file = bool(request.form.get('remove_file'))
    redirect_target = request.referrer or url_for('admin_support_bp.inbox')

    keeps_attachment = reply.attachment and not remove_file
    if not title or not (body or request.files.get('file') or keeps_attachment):
        flash('La respuesta rápida necesita un título y un texto o un adjunto.', 'danger')
        return redirect(redirect_target)

    try:
        new_attachment = support_service.save_attachment(request.files.get('file'))
    except SupportError as exc:
        flash(exc.message, 'danger')
        return redirect(redirect_target)

    if new_attachment:
        delete_uploaded_file(reply.attachment)
        reply.attachment = new_attachment
    elif remove_file:
        delete_uploaded_file(reply.attachment)
        reply.attachment = None

    reply.title = title[:60]
    reply.body = body[:2000]
    db.session.commit()
    flash('Respuesta rápida actualizada.', 'success')
    return redirect(redirect_target)


@admin_support_bp.route('/<int:chat_id>/respuestas-rapidas/<int:reply_id>/enviar', methods=['POST'])
@login_required
def quick_reply_send(chat_id, reply_id):
    """Manda la respuesta rápida (texto + adjunto, si tiene) directo al
    chat con un clic, en vez de solo rellenar el cuadro de texto — hace
    falta para poder mandar el adjunto, que no se puede "escribir" en el
    textarea como el texto."""
    chat = SupportChat.query.get_or_404(chat_id)
    qr = SupportQuickReply.query.get_or_404(reply_id)

    try:
        message = support_service.send_quick_reply(chat, qr, admin_id=_admin_id())
    except SupportError as exc:
        return jsonify({'ok': False, 'error': exc.message}), exc.status

    notify_support_admin_reply(chat, message)

    return jsonify({
        'ok': True,
        'status': chat.status,
        'status_label': chat.status_label,
        'message': support_service.serialize_message(message),
    })


@admin_support_bp.route('/respuestas-rapidas/<int:reply_id>/borrar', methods=['POST'])
@login_required
def quick_reply_delete(reply_id):
    reply = SupportQuickReply.query.get_or_404(reply_id)
    db.session.delete(reply)
    db.session.commit()
    flash('Respuesta rápida eliminada.', 'success')
    return redirect(request.referrer or url_for('admin_support_bp.inbox'))


@admin_support_bp.app_context_processor
def _inject_support_pending():
    """Badge de chats sin responder, junto al link de Soporte. Solo dentro del panel (/admin): no se calcula en las páginas de la tienda."""
    if not request.path.startswith('/admin'):
        return {}
    try:
        return {'support_pending_count': support_service.pending_chats_count()}
    except Exception:
        return {'support_pending_count': 0}
