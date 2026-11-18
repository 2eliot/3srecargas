"""Autenticador de Google (TOTP) para el admin, sin dependencias de pyotp.

El algoritmo es RFC 6238 (HMAC-SHA1, 6 dígitos, pasos de 30 s). El secreto se
guarda en `admin_users.totp_secret`; los códigos de recuperación se guardan
solo como hash. El QR se genera con `qrcode` (Pillow ya está instalado).
"""
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import struct
import time
from datetime import timezone
from urllib.parse import quote

TOTP_PERIOD = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # acepta el código anterior/siguiente por desfase de reloj

# Marca en la sesión de que esta sesión pasó el código (o acaba de configurarlo).
# Sin ella, una sesión abierta antes de activar el 2FA seguiría entrando.
SESSION_STAMP_KEY = 'admin_2fa_at'
SESSION_STAMP_TOLERANCE = 5

_RECOVERY_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def totp_required():
    """Si es False, un admin que AÚN NO activó el 2FA puede entrar sin configurarlo.

    Un admin que ya lo tiene activo siempre necesita el código; esta variable
    no lo salta. Para un teléfono perdido se usa scripts/reset_admin_totp.py.
    """
    return (os.environ.get('ADMIN_TOTP_REQUIRED', '1') or '1').strip().lower() not in {'0', 'false', 'no', 'off'}


def generate_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def _secret_bytes(secret):
    raw = ''.join(str(secret or '').split()).upper()
    raw += '=' * ((8 - len(raw) % 8) % 8)
    return base64.b32decode(raw, casefold=True)


def _hotp(secret, counter):
    digest = hmac.new(_secret_bytes(secret), struct.pack('>Q', int(counter)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def current_counter(timestamp=None):
    return int((time.time() if timestamp is None else timestamp) // TOTP_PERIOD)


def generate_code(secret, timestamp=None):
    """Código actual; se usa en pruebas y para depuración del setup."""
    return _hotp(secret, current_counter(timestamp))


def verify_code(secret, code, last_counter=0):
    """Devuelve (ok, counter). No acepta un código ya usado (counter <= last)."""
    if not secret:
        return False, 0
    clean = re.sub(r'\D', '', str(code or ''))
    if len(clean) != TOTP_DIGITS:
        return False, 0
    now = current_counter()
    for delta in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        counter = now + delta
        if counter <= int(last_counter or 0):
            continue
        try:
            candidate = _hotp(secret, counter)
        except Exception:
            return False, 0
        if hmac.compare_digest(candidate, clean):
            return True, counter
    return False, 0


def stamp_session(session):
    """Anota que esta sesión ya pasó el código (o acaba de activarlo)."""
    try:
        session[SESSION_STAMP_KEY] = int(time.time())
    except Exception:
        pass


def session_stamp_valid(session, confirmed_at):
    """True si la sesión se abrió/confirmó DESPUÉS de activar el 2FA actual."""
    try:
        stamped = int(session.get(SESSION_STAMP_KEY) or 0)
    except (TypeError, ValueError):
        stamped = 0
    if not stamped:
        return False
    if confirmed_at:
        try:
            confirmed_ts = int(confirmed_at.replace(tzinfo=timezone.utc).timestamp())
        except (TypeError, ValueError, AttributeError):
            confirmed_ts = 0
        if confirmed_ts and stamped + SESSION_STAMP_TOLERANCE < confirmed_ts:
            return False
    return True


def provisioning_uri(secret, account_name='admin', issuer='3S Recargas'):
    label = quote(f'{issuer}:{account_name}', safe='')
    issuer_q = quote(issuer, safe='')
    return (
        f'otpauth://totp/{label}?secret={quote(secret, safe="")}'
        f'&issuer={issuer_q}&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}'
    )


def qr_data_uri(uri):
    """PNG en data: URI para el <img> del setup. Si falta qrcode, devuelve ''."""
    try:
        import qrcode
        image = qrcode.make(uri)
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
    except Exception:
        return ''


def generate_recovery_codes(count=10):
    codes = []
    for _ in range(int(count)):
        left = ''.join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(4))
        right = ''.join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(4))
        codes.append(f'{left}-{right}')
    return codes


def hash_recovery_code(code):
    clean = re.sub(r'[^A-Z0-9]', '', str(code or '').upper())
    return hashlib.sha256(('3s-recovery:' + clean).encode('utf-8')).hexdigest()


def verify_recovery_code(admin, code):
    """Si el código es válido, lo consume y devuelve True."""
    clean = re.sub(r'[^A-Z0-9]', '', str(code or '').upper())
    if len(clean) < 8:
        return False
    try:
        hashes = json.loads(admin.totp_recovery_hashes or '[]')
    except (TypeError, ValueError):
        hashes = []
    digest = hash_recovery_code(clean)
    if digest not in hashes:
        return False
    hashes.remove(digest)
    admin.totp_recovery_hashes = json.dumps(hashes)
    return True
