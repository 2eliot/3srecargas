"""
Dispatcher de notificaciones de alto nivel.
Llamar estas funciones después de eventos del ciclo de vida de la orden.
"""

import logging

from flask import current_app

from app.utils.email import send_email_async, get_setting
from app.utils.email_templates import (
    build_order_created_email,
    build_order_approved_email,
    build_order_completed_pin_email,
    build_order_rejected_email,
    build_admin_new_order_email,
)

logger = logging.getLogger(__name__)


def _app():
    """Obtiene el objeto app real de Flask para hilos asíncronos."""
    return current_app._get_current_object()


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


def notify_order_approved(order, package, game):
    """Envía correo al cliente cuando la orden es aprobada (sin PIN)."""
    if not order.email:
        return
    app = _app()
    subject, html, text = build_order_approved_email(order, package, game)
    send_email_async(app, order.email, subject, html, text)


def notify_order_completed(order, package, game, pin_code=None):
    """Envía correo al cliente cuando la orden se completa (con PIN/código opcional)."""
    if not order.email:
        return
    app = _app()
    subject, html, text = build_order_completed_pin_email(order, package, game, pin_code)
    send_email_async(app, order.email, subject, html, text)


def notify_order_rejected(order, package, game, reason=''):
    """Envía correo al cliente cuando la orden es rechazada."""
    if not order.email:
        return
    app = _app()
    subject, html, text = build_order_rejected_email(order, package, game, reason)
    send_email_async(app, order.email, subject, html, text)
