"""Validación de correos de clientes por dominio.

El dueño pidió aceptar solo correos de proveedores reconocidos (Gmail, Outlook,
Hotmail, Yahoo, iCloud, etc.) y bloquear los de ejemplo/temporales. La lista de
proveedores vive aquí; el admin puede AGREGAR dominios propios desde
Admin → Seguridad (setting `allowed_email_domains`). Los dominios bloqueados
nunca se pueden habilitar.
"""
import re
from urllib.parse import urlsplit

DEFAULT_ALLOWED_DOMAINS = {
    # Google
    'gmail.com', 'googlemail.com',
    # Microsoft
    'hotmail.com', 'hotmail.es', 'hotmail.co.uk', 'hotmail.com.br', 'hotmail.com.ar',
    'hotmail.com.co', 'hotmail.com.pe', 'hotmail.com.ve', 'hotmail.com.mx', 'hotmail.fr', 'hotmail.it',
    'hotmail.de', 'outlook.com', 'outlook.es', 'outlook.com.br', 'outlook.com.ar',
    'outlook.com.co', 'outlook.com.pe', 'outlook.com.ve', 'outlook.com.mx', 'outlook.fr', 'outlook.it',
    'outlook.de', 'live.com', 'live.com.ve', 'live.com.mx', 'live.com.ar', 'live.com.co',
    'live.com.pe', 'live.com.br', 'msn.com',
    # Yahoo
    'yahoo.com', 'yahoo.es', 'yahoo.com.mx', 'yahoo.com.ar', 'yahoo.com.co',
    'yahoo.com.pe', 'yahoo.com.ve', 'yahoo.com.br', 'yahoo.co.uk', 'yahoo.fr', 'yahoo.it',
    'yahoo.de', 'ymail.com', 'rocketmail.com',
    # Apple
    'icloud.com', 'me.com', 'mac.com',
    # Otros proveedores reconocidos
    'aol.com', 'aol.es', 'protonmail.com', 'proton.me', 'pm.me',
    'zoho.com', 'zohomail.com', 'gmx.com', 'gmx.net', 'gmx.es', 'gmx.de',
    'mail.com', 'yandex.com', 'yandex.ru', 'tutanota.com', 'tutamail.com',
    'tuta.io', 'fastmail.com', 'fastmail.fm',
    # Proveedores de internet habituales en Latinoamérica y Estados Unidos
    'cantv.net', 'uol.com.br', 'bol.com.br', 'terra.com.br', 'ig.com.br',
    'comcast.net', 'verizon.net', 'att.net', 'cox.net', 'sbcglobal.net',
    'web.de', 'mail.ru', 'bk.ru', 'inbox.ru', 'list.ru',
    'naver.com', 'hanmail.net', 'daum.net', 'qq.com', '163.com', '126.com',
}

BLOCKED_DOMAINS = {
    # Reservados para ejemplos/documentación: nunca son un cliente real.
    'example.com', 'example.org', 'example.net', 'example.edu',
    'test.com', 'test.org', 'test.net', 'localhost', 'local', 'invalid',
    # Correos temporales/desechables conocidos.
    'mailinator.com', 'yopmail.com', 'guerrillamail.com', '10minutemail.com',
    'tempmail.com', 'temp-mail.org', 'throwawaymail.com', 'sharklasers.com',
    'getnada.com', 'dispostable.com', 'trashmail.com', 'maildrop.cc',
    'mintemail.com', 'mytemp.email', 'fakeinbox.com',
}

_DOMAIN_RE = re.compile(r'^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$')
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_SETTING_KEY = 'allowed_email_domains'


def normalize_domain(value):
    domain = str(value or '').strip().lower().lstrip('@').rstrip('.')
    if '://' in domain:
        domain = (urlsplit(domain).hostname or '').lower()
    return domain


def email_domain(email):
    raw = str(email or '').strip().lower()
    if '@' not in raw:
        return ''
    return normalize_domain(raw.rsplit('@', 1)[1])


def configured_domains():
    """Dominios extra que el admin agregó (no reemplazan a los de fábrica)."""
    try:
        from flask import g, has_app_context
        if has_app_context():
            cached = getattr(g, '_allowed_email_domains', None)
            if cached is not None:
                return cached
        from ..models import Setting
        row = Setting.query.filter_by(key=_SETTING_KEY).first()
        raw = row.value if row and row.value else ''
        domains = set()
        for part in re.split(r'[\s,;]+', raw):
            domain = normalize_domain(part)
            if domain and _DOMAIN_RE.match(domain) and domain not in BLOCKED_DOMAINS:
                domains.add(domain)
        if has_app_context():
            g._allowed_email_domains = domains
        return domains
    except Exception:
        return set()


def allowed_domains():
    return set(DEFAULT_ALLOWED_DOMAINS) | configured_domains()


def domain_allowed(domain):
    domain = normalize_domain(domain)
    return bool(domain) and domain not in BLOCKED_DOMAINS and domain in allowed_domains()


def email_domain_problem(email):
    """Devuelve un mensaje para el cliente, o None si el correo es aceptable.

    Un correo vacío devuelve None: cada pantalla decide si es obligatorio.
    """
    raw = str(email or '').strip()
    if not raw:
        return None
    if not _EMAIL_RE.match(raw):
        return 'Escribe un correo electrónico válido.'
    domain = email_domain(raw)
    if not domain:
        return 'Escribe un correo electrónico válido.'
    if domain in BLOCKED_DOMAINS:
        return 'No se aceptan correos de ejemplo ni temporales. Usa tu Gmail, Outlook, Hotmail, Yahoo o iCloud.'
    if domain not in allowed_domains():
        return (
            'Usa un correo de un proveedor reconocido (Gmail, Outlook, Hotmail, Yahoo, iCloud…). '
            'Si usas el correo de tu empresa, escríbenos por soporte para habilitarlo.'
        )
    return None


def parse_domains_text(raw):
    """Lista limpia de dominios válidos para guardar en el admin."""
    result = set()
    for part in re.split(r'[\s,;]+', str(raw or '')):
        domain = normalize_domain(part)
        if domain and _DOMAIN_RE.match(domain) and domain not in BLOCKED_DOMAINS:
            result.add(domain)
    return sorted(result)


def domains_text():
    return ', '.join(sorted(configured_domains()))
