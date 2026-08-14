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
from app.utils.push_notifications import send_push_to_order_subscribers_async

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
