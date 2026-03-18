"""
Email sending utilities for 3S Recargas.
Supports STARTTLS, SSL fallback, sync and async sending.
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
    """Read SMTP config from Flask app config."""
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
    """Get a Setting value from DB, with fallback."""
    try:
        s = Setting.query.filter_by(key=key).first()
        return (s.value or '').strip() if s else default
    except Exception:
        return default


def _smtp_send_starttls(cfg, msg, to_email):
    """Send via STARTTLS."""
    with smtplib.SMTP(cfg['server'], cfg['port'], timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(cfg['username'], cfg['password'])
        smtp.sendmail(cfg['username'], to_email, msg.as_string())


def _smtp_send_ssl(cfg, msg, to_email):
    """Send via SSL (fallback)."""
    port = 465 if cfg['port'] in (587, 25) else cfg['port']
    with smtplib.SMTP_SSL(cfg['server'], port, timeout=30) as smtp:
        smtp.login(cfg['username'], cfg['password'])
        smtp.sendmail(cfg['username'], to_email, msg.as_string())


def send_email_html(to_email, subject, html_body, text_body=''):
    """
    Send an HTML email with plain-text fallback.
    Returns True on success, False on failure.
    """
    cfg = _get_mail_config()
    if not cfg['username'] or not cfg['password'] or not to_email:
        logger.warning('Email not sent: missing credentials or recipient.')
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
            logger.info(f'Email sent (STARTTLS) to {to_email}: {subject}')
            return True
        except Exception:
            _smtp_send_ssl(cfg, msg, to_email)
            logger.info(f'Email sent (SSL fallback) to {to_email}: {subject}')
            return True
    except Exception as e:
        logger.error(f'Failed to send email to {to_email}: {e}')
        return False


def send_email_async(app, to_email, subject, html_body, text_body=''):
    """
    Send email in a background thread so the request isn't blocked.
    `app` must be the real Flask app object (not the proxy).
    """
    def _send():
        with app.app_context():
            send_email_html(to_email, subject, html_body, text_body)

    t = threading.Thread(target=_send, daemon=True)
    t.start()
