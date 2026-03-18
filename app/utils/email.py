"""
Utilidades de envío de correo para 3S Recargas.
Soporta STARTTLS, fallback SSL, envío síncrono y asíncrono.
"""

import smtplib
import threading
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import current_app

from app.models import Setting

logger = logging.getLogger(__name__)


def _get_mail_config():
    """Lee la configuración SMTP desde la config de Flask."""
    app = current_app._get_current_object()
    return {
        'server': app.config.get('MAIL_SERVER', 'smtp.gmail.com'),
        'port': app.config.get('MAIL_PORT', 587),
        'username': app.config.get('MAIL_USERNAME', ''),
        'password': app.config.get('MAIL_PASSWORD', ''),
        'use_tls': app.config.get('MAIL_USE_TLS', True),
        'use_ssl': app.config.get('MAIL_USE_SSL', False),
        'default_sender': app.config.get('MAIL_DEFAULT_SENDER', ''),
    }


def get_setting(key, default=''):
    """Obtiene un valor de Setting desde la BD, con fallback."""
    try:
        s = Setting.query.filter_by(key=key).first()
        return (s.value or '').strip() if s else default
    except Exception:
        return default


def _smtp_send_starttls(cfg, msg, to_email):
    """Envía vía STARTTLS."""
    with smtplib.SMTP(cfg['server'], cfg['port'], timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(cfg['username'], cfg['password'])
        smtp.sendmail(cfg['username'], to_email, msg.as_string())


def _smtp_send_ssl(cfg, msg, to_email):
    """Envía vía SSL (fallback)."""
    port = 465 if cfg['port'] in (587, 25) else cfg['port']
    with smtplib.SMTP_SSL(cfg['server'], port, timeout=30) as smtp:
        smtp.login(cfg['username'], cfg['password'])
        smtp.sendmail(cfg['username'], to_email, msg.as_string())


def send_email_html(to_email, subject, html_body, text_body=''):
    """
    Envía un correo HTML con fallback a texto plano.
    Retorna True si se envió, False si falló.
    """
    cfg = _get_mail_config()
    if not cfg['username'] or not cfg['password'] or not to_email:
        logger.warning('Correo no enviado: faltan credenciales o destinatario.')
        return False

    try:
        msg = MIMEMultipart('alternative')
        sender_name = get_setting('email_brand_name', '') or current_app.config.get('MAIL_BRAND_NAME', '3S Recargas')
        sender_addr = cfg['default_sender'] or cfg['username']
        msg['From'] = f'{sender_name} <{sender_addr}>'
        msg['To'] = to_email
        msg['Subject'] = subject

        if text_body:
            msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
        msg.attach(MIMEText(html_body or '', 'html', 'utf-8'))

        try:
            _smtp_send_starttls(cfg, msg, to_email)
            logger.info(f'Correo enviado (STARTTLS) a {to_email}: {subject}')
            return True
        except Exception:
            _smtp_send_ssl(cfg, msg, to_email)
            logger.info(f'Correo enviado (SSL fallback) a {to_email}: {subject}')
            return True
    except Exception as e:
        logger.error(f'Error al enviar correo a {to_email}: {e}')
        return False


def send_email_async(app, to_email, subject, html_body, text_body=''):
    """
    Envía el correo en un hilo de fondo para no bloquear la petición.
    `app` debe ser el objeto Flask real (no el proxy).
    """
    def _send():
        with app.app_context():
            send_email_html(to_email, subject, html_body, text_body)

    t = threading.Thread(target=_send, daemon=True)
    t.start()
