"""Qué dato le pide la tienda al cliente en cada producto (ID, correo, usuario, teléfono o texto libre) y cómo se limpia y se valida.

Un solo lugar para toda la web: la tienda, el chat de soporte, el canje de código, las cuentas de clientes y el panel usan estas mismas reglas.

El TIPO decide dónde se guarda el dato (el nombre que el dueño le pone al campo solo es lo que ve el cliente):
  * correo de un producto de Wallet -> campo "correo" de la orden (así funciona Zinli desde siempre);
  * todo lo demás (ID, usuario, teléfono, texto, y el correo de un juego) -> "dato de la cuenta" (`player_id`).
Un usuario o un teléfono nunca caen en el campo del correo, que es el que se usa para mandar los correos de confirmación.
"""
import re

PLAYER_INPUT_TYPES = ('numeric', 'email', 'username', 'phone', 'text')

# Nombre que ve el cliente cuando el dueño no escribió uno propio.
DEFAULT_LABELS = {
    'numeric': 'Player ID',
    'email': 'Correo electrónico',
    'username': 'Nombre de usuario',
    'phone': 'Número de teléfono',
    'text': 'Dato del jugador',
}
# El nombre que trae un producto nuevo. En un tipo que no es ID se cambia por el nombre normal del tipo.
DEFAULT_ID_LABEL = 'Player ID'

PHONE_MIN_DIGITS = 7
PHONE_MAX_DIGITS = 15
MAX_VALUE_LENGTH = 100          # `orders.player_id` es VARCHAR(100)
EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]{2,}$')
PHONE_RE = re.compile(r'^\+?\d{%d,%d}$' % (PHONE_MIN_DIGITS, PHONE_MAX_DIGITS))


def normalize_type(value):
    """Un tipo desconocido cae en "solo números" (como siempre)."""
    value = str(value or '').strip().lower()
    return value if value in PLAYER_INPUT_TYPES else 'numeric'


def category_slug(game):
    category = getattr(game, 'category', None) if game is not None else None
    return str(getattr(category, 'slug', '') or '').strip().lower() if category is not None else ''


def field_kind(game):
    """'none' (tarjetas: no hay cuadro de ID, solo el correo de entrega) o el tipo del producto."""
    slug = category_slug(game)
    if slug == 'tarjetas':
        return 'none'
    raw = str(getattr(game, 'player_id_input_type', '') or '').strip().lower()
    if slug == 'wallet' and raw not in PLAYER_INPUT_TYPES:
        return 'email'      # un Wallet sin tipo válido sigue pidiendo el correo, como siempre
    return normalize_type(raw)


def stores_in_email(game):
    """True si el dato del cliente se guarda en el campo "correo" de la orden (solo Wallet con tipo correo)."""
    return category_slug(game) == 'wallet' and field_kind(game) == 'email'


def display_label(game):
    """El nombre que ve el cliente: el que escribió el dueño, o el nombre normal del tipo si lo dejó vacío o con el "Player ID" de fábrica."""
    kind = field_kind(game)
    label = str(getattr(game, 'player_id_label', '') or '').strip()
    if not label:
        return DEFAULT_LABELS.get(kind, DEFAULT_ID_LABEL)
    if label == DEFAULT_ID_LABEL and kind not in ('numeric', 'none'):
        return DEFAULT_LABELS[kind]
    return label


def clean_value(kind, raw):
    """El dato como se guarda: solo números / sin espacios / teléfono con + opcional y dígitos."""
    value = str(raw or '')
    if kind == 'numeric':
        return ''.join(ch for ch in value if ch.isdigit())
    if kind in ('email', 'username'):
        return ''.join(value.split())
    if kind == 'phone':
        compact = ''.join(value.split())
        digits = ''.join(ch for ch in compact if ch in '0123456789')
        return ('+' if compact.startswith('+') else '') + digits
    return value.strip()


def validate_value(kind, value):
    """Mensaje de error (en español) o '' si el dato está bien. Un dato vacío no se valida aquí: cada pantalla ya exige que no esté vacío."""
    if not value:
        return ''
    if len(value) > MAX_VALUE_LENGTH:
        return 'Ese dato es demasiado largo. Revísalo.'
    if kind == 'email' and not EMAIL_RE.match(value):
        return 'Revisa tu correo: parece que no está bien escrito.'
    if kind == 'phone' and not PHONE_RE.match(value):
        return 'Revisa el número de teléfono: debe tener entre %d y %d números (puede empezar con +).' % (PHONE_MIN_DIGITS, PHONE_MAX_DIGITS)
    return ''
