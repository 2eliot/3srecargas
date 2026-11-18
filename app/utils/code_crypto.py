"""Cifrado de los códigos (stock y códigos entregados).

Los códigos se guardan cifrados en la base con Fernet. Al cliente solo se le
muestra el código real si el juego y el paquete están activos; si el juego está
apagado se le muestra un aviso de código cifrado. El admin puede verlos
descifrados después de escribir su código de Google Authenticator.

La clave sale de `CODE_ENCRYPTION_KEY` si está en el entorno; si no, se deriva
de `SECRET_KEY` (estable). Cambiar esa clave deja los códigos viejos sin poder
descifrarse.
"""
import base64
import hashlib
import os
import time

from cryptography.fernet import Fernet, InvalidToken

PREFIX = 'enc:v1:'
CODES_UNLOCK_SESSION_KEY = 'admin_codes_unlocked_at'
CODES_UNLOCK_SECONDS = 15 * 60

_FERNET_CACHE = {}


def _fernet():
    key_env = (os.environ.get('CODE_ENCRYPTION_KEY') or '').strip()
    if key_env:
        raw_key = key_env.encode('ascii', 'ignore')
    else:
        from flask import current_app, has_app_context
        secret = ''
        if has_app_context():
            secret = str(current_app.config.get('SECRET_KEY') or '')
        secret = secret or os.environ.get('SECRET_KEY') or '3s-recargas'
        digest = hashlib.sha256(('3s-code-encryption-v1:' + secret).encode('utf-8')).digest()
        raw_key = base64.urlsafe_b64encode(digest)
    cached = _FERNET_CACHE.get(raw_key)
    if cached is not None:
        return cached
    try:
        fernet = Fernet(raw_key)
    except Exception:
        # Una clave de entorno mal escrita no debe dejar la tienda sin códigos:
        # se cae a la derivada de SECRET_KEY.
        digest = hashlib.sha256(
            ('3s-code-encryption-v1:' + str(os.environ.get('SECRET_KEY') or '3s-recargas')).encode('utf-8')
        ).digest()
        fernet = Fernet(base64.urlsafe_b64encode(digest))
    _FERNET_CACHE[raw_key] = fernet
    return fernet


def is_encrypted(value):
    return str(value or '').startswith(PREFIX)


def encrypt_code(value):
    text = str(value or '')
    if not text or is_encrypted(text):
        return text
    token = _fernet().encrypt(text.encode('utf-8')).decode('ascii')
    return PREFIX + token


def decrypt_code(value):
    text = str(value or '')
    if not text or not is_encrypted(text):
        return text
    try:
        return _fernet().decrypt(text[len(PREFIX):].encode('ascii')).decode('utf-8')
    except (InvalidToken, ValueError, TypeError):
        return ''


def mask_code():
    return '••••••••'


def mark_codes_unlocked():
    from flask import session
    session[CODES_UNLOCK_SESSION_KEY] = int(time.time())


def lock_codes():
    from flask import session
    session.pop(CODES_UNLOCK_SESSION_KEY, None)


def codes_unlocked():
    from flask import has_request_context, session
    if not has_request_context():
        return False
    try:
        stamped = int(session.get(CODES_UNLOCK_SESSION_KEY) or 0)
    except (TypeError, ValueError):
        return False
    return bool(stamped) and (time.time() - stamped) < CODES_UNLOCK_SECONDS


def customer_code_for_order(order):
    """(código, disponible, mensaje) para la página/correo/campanita del cliente.

    - `codes_hidden`: bloqueado por seguridad, nunca se muestra.
    - juego o paquete inactivo: no se descifra; se avisa que está cifrado.
    - juego y paquete activos: se devuelve el código real.
    """
    if not order or not getattr(order, 'pin_delivered', ''):
        return '', False, ''
    if getattr(order, 'codes_hidden', False):
        return '', False, (
            'El código de esta orden fue bloqueado por seguridad. '
            'Escríbenos por soporte para revisarlo.'
        )
    game = getattr(order, 'game', None)
    package = getattr(order, 'package', None)
    if not game or not package or not game.is_active or not package.is_active:
        return '', False, (
            'El código está cifrado porque el juego está desactivado. '
            'Se mostrará cuando el juego vuelva a estar activo; mientras tanto no se puede canjear.'
        )
    code = decrypt_code(order.pin_delivered)
    if not code:
        return '', False, 'No se pudo leer el código. Escríbenos por soporte.'
    return code, True, ''


def admin_code_for_order(order):
    """Código real para el admin, solo con la sesión de códigos desbloqueada."""
    if not order or not getattr(order, 'pin_delivered', ''):
        return ''
    if not codes_unlocked():
        return ''
    return decrypt_code(order.pin_delivered)
