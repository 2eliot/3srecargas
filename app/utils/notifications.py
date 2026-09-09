"""
Dispatcher de notificaciones de alto nivel.
Llamar estas funciones después de eventos del ciclo de vida de la orden.
"""

import logging
import os

from flask import current_app, url_for

from app.utils.email import send_email_async, get_setting
from app.utils.email_templates import (
    build_order_created_email,
    build_order_approved_email,
    build_order_completed_pin_email,
    build_order_rejected_email,
    build_admin_new_order_email,
)
from app.utils.push_notifications import (
    send_push_to_order_subscribers_async,
    send_push_to_chat_subscribers_async,
)

logger = logging.getLogger(__name__)


def _app():
    """Obtiene el objeto app real de Flask para hilos asíncronos."""
    return current_app._get_current_object()


def _resolve_upload_attachment(relative_path):
    relative_path = str(relative_path or '').strip()
    if not relative_path:
        return None

    upload_root = current_app.config.get('UPLOAD_FOLDER', '')
    absolute_path = os.path.join(upload_root, relative_path)
    if not os.path.isfile(absolute_path):
        logger.warning('Adjunto de orden no encontrado: %s', absolute_path)
        return None

    return {
        'path': absolute_path,
        'filename': os.path.basename(relative_path),
    }


def notify_order_created(order, package, game):
    """Envía correo al cliente + admin cuando se crea una nueva orden."""
    app = _app()

    # Correo al cliente
    if order.email:
        subject, html, text = build_order_created_email(order, package, game)
        send_email_async(app, order.email, subject, html, text)

    # Correo al admin
    admin_email = get_setting('admin_notify_email', '') or app.config.get('ADMIN_NOTIFY_EMAIL', '')
    if admin_email:
        subject, html, text = build_admin_new_order_email(order, package, game)
        send_email_async(app, admin_email, subject, html, text)


def _order_status_url(order):
    try:
        return url_for('checkout_bp.order_status', order_number=order.order_number)
    except Exception:
        return '/'


def notify_order_approved(order, package, game, delivery_proof_path=None):
    """Avisa al cliente (correo + push) cuando la orden es aprobada (sin PIN)."""
    app = _app()
    if order.email:
        attachment = _resolve_upload_attachment(delivery_proof_path or getattr(order, 'delivery_proof', ''))
        subject, html, text = build_order_approved_email(order, package, game, has_delivery_proof=bool(attachment))
        send_email_async(app, order.email, subject, html, text, attachments=[attachment] if attachment else None)

    send_push_to_order_subscribers_async(
        app, order.id,
        '¡Tu pago fue confirmado!',
        f'Tu orden #{order.order_number} de {game.name} está aprobada y en proceso.',
        url=_order_status_url(order),
    )


def notify_order_completed(order, package, game, pin_code=None):
    """Avisa al cliente (correo + push) cuando la orden se completa."""
    app = _app()
    if order.email:
        subject, html, text = build_order_completed_pin_email(order, package, game, pin_code)
        send_email_async(app, order.email, subject, html, text)

    send_push_to_order_subscribers_async(
        app, order.id,
        '¡Tu recarga está lista! 🎉',
        f'Tu orden #{order.order_number} de {game.name} ya se completó.',
        url=_order_status_url(order),
    )


def notify_order_rejected(order, package, game, reason=''):
    """Avisa al cliente (correo + push) cuando la orden es rechazada."""
    app = _app()
    if order.email:
        subject, html, text = build_order_rejected_email(order, package, game, reason)
        send_email_async(app, order.email, subject, html, text)

    send_push_to_order_subscribers_async(
        app, order.id,
        'Hubo un problema con tu orden',
        f'Tu orden #{order.order_number} de {game.name} fue rechazada. Contáctanos si crees que es un error.',
        url=_order_status_url(order),
    )


# ─── Soporte ─────────────────────────────────────────────────────────────────
#
# Al admin se le avisa solo por correo. `send_push_broadcast` iría a TODOS
# los navegadores suscritos —clientes incluidos—, así que usarlo para un
# aviso interno le mandaría "chat nuevo de soporte" a media Venezuela.

def _support_admin_email():
    app = _app()
    return get_setting('admin_notify_email', '') or app.config.get('ADMIN_NOTIFY_EMAIL', '')


def _support_admin_url(chat):
    try:
        return url_for('admin_support_bp.detail', chat_id=chat.id, _external=True)
    except Exception:
        return f'/admin/soporte/{chat.id}'


def _support_context_lines(chat):
    pairs = [
        ('Orden', chat.context_order_number),
        ('ID de jugador', chat.context_player_id),
        ('Correo', chat.context_email),
        ('Teléfono', chat.context_phone),
        ('Juego', chat.context_game),
        ('Paquete', chat.context_package),
    ]
    return [f'{label}: {value}' for label, value in pairs if value]


def notify_support_chat_opened(chat):
    """Correo al admin cuando se abre un chat nuevo."""
    admin_email = _support_admin_email()
    if not admin_email:
        return

    link = _support_admin_url(chat)
    context = _support_context_lines(chat)
    context_html = ''.join(f'<li>{line}</li>' for line in context) or '<li>Sin contexto: llegó desde la portada.</li>'

    subject = f'Nuevo chat de soporte {chat.display_code} — {chat.client_name}'
    html = (
        f'<p><strong>{chat.client_name}</strong> abrió el chat {chat.display_code}.</p>'
        f'<ul>{context_html}</ul>'
        f'<p><a href="{link}">Abrir el chat en el panel</a></p>'
    )
    text = '\n'.join([f'{chat.client_name} abrió el chat {chat.display_code}.', *context, link])

    send_email_async(_app(), admin_email, subject, html, text)


def notify_support_client_message(chat, message):
    """Correo al admin cuando el cliente escribe.

    Solo en el primer mensaje sin responder: `unread_admin` vale 1 justo
    ahí. Si el cliente manda cinco mensajes seguidos —lo normal cuando
    alguien está molesto— sale un correo, no cinco.
    """
    if (chat.unread_admin or 0) != 1:
        return

    admin_email = _support_admin_email()
    if not admin_email:
        return

    body = (message.body or '(imagen adjunta)')[:400]
    link = _support_admin_url(chat)
    subject = f'Mensaje de soporte {chat.display_code} — {chat.client_name}'
    html = f'<p><strong>{chat.client_name}</strong> escribió:</p><blockquote>{body}</blockquote><p><a href="{link}">Responder</a></p>'
    text = f'{chat.client_name} escribió:\n{body}\n\n{link}'

    send_email_async(_app(), admin_email, subject, html, text)


def notify_support_admin_reply(chat, message):
    """Avisa al cliente de que le respondieron: push si aceptó
    notificaciones, y correo si en algún momento dejó uno."""
    app = _app()
    body = (message.body or 'Tienes una respuesta de soporte.')[:120]

    send_push_to_chat_subscribers_async(
        app, chat.id,
        'Soporte te respondió 💬',
        body,
        url='/',
    )

    email = chat.context_email or (chat.order.email if chat.order else '')
    if not email:
        return

    subject = f'Soporte 3S Recargas respondió tu chat {chat.display_code}'
    html = (
        f'<p>Hola {chat.client_name}, respondimos tu consulta:</p>'
        f'<blockquote>{body}</blockquote>'
        '<p>Entra a la tienda y abre el chat de soporte para continuar.</p>'
    )
    text = f'Hola {chat.client_name}, respondimos tu consulta:\n{body}'
    send_email_async(app, email, subject, html, text)
