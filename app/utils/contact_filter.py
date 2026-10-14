"""Regla de la tienda: lo que se muestra en un perfil PÚBLICO (nombre, "acerca de mí", nombre de usuario) no puede llevar
redes sociales, enlaces, correos, teléfonos ni números de ID. Motivo: la competencia podría entrar a los perfiles, sacar
información y llevarse a los clientes que más compran.

No es una barrera perfecta (siempre habrá quien escriba "i n s t a" con huecos): por eso además el administrador puede
ocultar o restablecer cualquier perfil. Lo que se detecta se TACHA CON ASTERISCOS (el resto del texto sí se guarda y se ve):
ver `mask_contact_info`. El nombre de usuario, que es un identificador, sí se rechaza (`username_looks_like_contact`).
"""
import re

PLATFORMS = (
    'instagram', 'facebook', 'whatsapp', 'telegram', 'discord', 'youtube', 'twitter', 'snapchat', 'tiktok', 'twitch',
)
_TLDS = r'(?:com|net|org|gg|me|io|tv|ly|co|ve|app|link|bio|xyz|info|site|online|store|shop|page|dev|cc|us|es|mx|ar|cl|pe)'

URL_RE = re.compile(r'(?:https?://|www\.|\b[a-z0-9-]{2,}\.' + _TLDS + r'\b|\bt\.me\b|\bwa\.me\b)', re.IGNORECASE)
EMAIL_RE = re.compile(r'[^\s@]+@[^\s@]+\.[^\s@]+')
HANDLE_RE = re.compile(r'(?<![\w])@[\w.]{2,}')
# 6 o más dígitos seguidos (con espacios, puntos o guiones entre ellos): teléfonos e IDs de jugador
DIGITS_RE = re.compile(r'(?:\d[\s.\-_]*){6,}')

MESSAGE = ('No pongas enlaces, redes sociales, correos, teléfonos ni números de ID: por seguridad de la tienda el perfil '
           'público no los muestra.')


def contains_contact_info(text):
    """True si el texto trae algo que la regla prohíbe."""
    value = str(text or '')
    if not value.strip():
        return False
    lowered = value.lower()
    if URL_RE.search(lowered) or EMAIL_RE.search(value) or HANDLE_RE.search(value) or DIGITS_RE.search(value):
        return True
    squashed = re.sub(r'[\W_]+', '', lowered)  # "ins.ta-gram" -> "instagram"
    return any(name in squashed for name in PLATFORMS)


def username_looks_like_contact(value):
    """Para nombres de usuario (letras, números, . - _): sin números largos (podría ser un ID) ni nombres de redes."""
    value = str(value or '')
    if re.search(r'\d{6,}', value):
        return True
    squashed = re.sub(r'[\W_]+', '', value.lower())
    return any(name in squashed for name in PLATFORMS)


PLATFORM_RES = [
    re.compile(r'[\W_]*'.join(re.escape(ch) for ch in name), re.IGNORECASE) for name in PLATFORMS
]
MASK_MESSAGE = ('Tachamos con asteriscos lo que parecía un enlace, red social, correo, teléfono o número de ID: '
                'por seguridad de la tienda el perfil público no los muestra.')


def _mask_all(match):
    """Cada letra, número o signo pasa a "*"; los espacios se quedan (así se ve cuántas palabras eran)."""
    return re.sub(r'\S', '*', match.group(0))


def _mask_digits(match):
    text = match.group(0)
    core = re.sub(r'[\s.\-_]+$', '', text)
    return re.sub(r'\S', '*', core) + text[len(core):]


def mask_contact_info(text):
    """Tacha con asteriscos todo lo que parezca correo, enlace, @usuario, red social o número de 6+ dígitos (ID o teléfono).
    Devuelve (texto, cambió). Es idempotente: un texto ya tachado no vuelve a cambiar.

    Debe hacer lo mismo que `maskContact` de static/js/profile-editor.js (la vista previa del editor)."""
    original = str(text or '')
    if not original.strip():
        return original, False
    value = EMAIL_RE.sub(_mask_all, original)
    # un enlace se tacha ENTERO (la palabra completa), no solo el dominio
    value = re.sub(r'\S+', lambda m: _mask_all(m) if URL_RE.search(m.group(0).lower()) else m.group(0), value)
    value = HANDLE_RE.sub(_mask_all, value)
    value = DIGITS_RE.sub(_mask_digits, value)
    for pattern in PLATFORM_RES:
        value = pattern.sub(_mask_all, value)
    return value, value != original


def public_name(display_name, username):
    """Nombre para mostrar ya tachado. Si de él no queda ni una letra o número (era todo un ID, un teléfono...), se usa el usuario."""
    masked, _changed = mask_contact_info(display_name)
    masked = ' '.join(masked.split())
    return masked if re.search(r'[^\W_]', masked) else username
