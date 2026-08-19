"""Disponibilidad de un paquete para la venta: stock y horario.

Antes esta regla vivía suelta dentro del endpoint que arma la vitrina
(`main.py`), así que solo pintaba la tarjeta en gris: el checkout no la
consultaba nunca y entrando directo a `/checkout/<id>` se compraba igual un
paquete agotado. Ahora la regla vive en un solo sitio y la usan las dos
puntas.
"""

from ..models import RevendedoresItemMapping, Setting
from .timezone import now_ve

# Los paquetes que se recargan a mano solo se venden dentro del horario de
# atención: fuera de él nadie puede procesarlos y el cliente se queda
# reclamando de madrugada. Por defecto, de 10:00 a. m. a 10:00 p. m. (hora
# de Venezuela), que es el horario que anuncia la propia web.
DEFAULT_MANUAL_OPEN_HOUR = 10
DEFAULT_MANUAL_CLOSE_HOUR = 22


def package_has_auto_mapping(package_id):
    """True si el paquete tiene un mapeo activo de Revendedores que sirva de
    respaldo cuando el stock de PINs llega a 0."""
    if not package_id:
        return False
    try:
        return bool(
            RevendedoresItemMapping.query.filter_by(
                store_package_id=int(package_id),
                active=True,
                auto_enabled=True,
            ).first()
        )
    except Exception:
        return False


def package_ids_with_auto_mapping(package_ids):
    """Versión en lote de `package_has_auto_mapping`, para armar la vitrina
    sin una consulta por paquete."""
    ids = [int(pid) for pid in (package_ids or []) if pid]
    if not ids:
        return set()
    try:
        return {
            m.store_package_id
            for m in RevendedoresItemMapping.query.filter(
                RevendedoresItemMapping.store_package_id.in_(ids),
                RevendedoresItemMapping.active == True,  # noqa: E712
                RevendedoresItemMapping.auto_enabled == True,  # noqa: E712
            ).all()
        }
    except Exception:
        return set()


def package_needs_pin_stock(package, is_tarjetas=None):
    """True si la entrega de este paquete consume un PIN del stock.

    Es la misma regla que aplica `_approve_order_locked` al momento de
    entregar (`package.is_automated or categoría == 'tarjetas'`): las
    tarjetas y gift cards se entregan por PIN aunque no tengan encendido el
    interruptor de automatizado, y son justamente las de iTunes y Roblox que
    pide el documento.
    """
    if not package:
        return False
    if package.is_automated:
        return True

    if is_tarjetas is None:
        try:
            is_tarjetas = (
                package.game.category.slug or ''
            ).lower() == 'tarjetas' if package.game and package.game.category else False
        except Exception:
            is_tarjetas = False
    return bool(is_tarjetas)


def package_is_out_of_stock(package, auto_mapped_ids=None, is_tarjetas=None):
    """True cuando el paquete se entrega por stock de PINs, no le quedan PINs
    libres y tampoco hay un mapeo de Revendedores que lo cubra.

    `auto_mapped_ids` permite reutilizar la consulta en lote al armar la
    vitrina; si no se pasa, se consulta el mapeo de ese paquete.
    """
    if not package_needs_pin_stock(package, is_tarjetas=is_tarjetas):
        return False

    if int(package.pin_count or 0) > 0:
        return False

    if auto_mapped_ids is not None:
        return package.id not in auto_mapped_ids

    return not package_has_auto_mapping(package.id)


# ─── Horario de los paquetes manuales ───────────────────────────────────────

def _get_hour_setting(key, fallback):
    try:
        setting = Setting.query.filter_by(key=key).first()
        value = int((setting.value if setting else '').strip())
    except (AttributeError, TypeError, ValueError):
        return fallback
    return value if 0 <= value <= 23 else fallback


def get_manual_schedule():
    """Horario de atención para las recargas manuales, en hora de Venezuela."""
    return {
        'open_hour': _get_hour_setting('manual_open_hour', DEFAULT_MANUAL_OPEN_HOUR),
        'close_hour': _get_hour_setting('manual_close_hour', DEFAULT_MANUAL_CLOSE_HOUR),
    }


def format_hour(hour):
    """12 -> '12:00 p. m.', 10 -> '10:00 a. m.'"""
    hour = int(hour) % 24
    suffix = 'a. m.' if hour < 12 else 'p. m.'
    display = hour % 12
    if display == 0:
        display = 12
    return f'{display}:00 {suffix}'


def manual_service_is_open(schedule=None, now=None):
    """True si estamos dentro del horario de atención.

    Soporta rangos que cruzan la medianoche (abrir a las 22 y cerrar a las 5),
    por si alguna vez se configura al revés.
    """
    schedule = schedule or get_manual_schedule()
    open_hour = schedule['open_hour']
    close_hour = schedule['close_hour']

    if open_hour == close_hour:
        return True  # sin ventana definida: se atiende siempre

    hour = (now or now_ve()).hour
    if open_hour < close_hour:
        return open_hour <= hour < close_hour
    return hour >= open_hour or hour < close_hour


def package_is_manual(package, auto_mapped_ids=None, is_tarjetas=None):
    """True si este paquete lo tiene que recargar un admin a mano.

    Es el complemento de las dos vías automáticas: stock de PINs propio o
    mapeo activo de Revendedores. Si no tiene ninguna, la recarga la hace una
    persona y por eso depende del horario de atención.
    """
    if not package:
        return False
    if package_needs_pin_stock(package, is_tarjetas=is_tarjetas):
        return False
    if auto_mapped_ids is not None:
        return package.id not in auto_mapped_ids
    return not package_has_auto_mapping(package.id)


def package_is_closed_now(package, auto_mapped_ids=None, is_tarjetas=None, schedule=None):
    """True si el paquete es manual y ahora mismo estamos fuera de horario."""
    if not package_is_manual(
        package, auto_mapped_ids=auto_mapped_ids, is_tarjetas=is_tarjetas
    ):
        return False
    return not manual_service_is_open(schedule=schedule)


def get_purchase_block_reason(package, auto_mapped_ids=None, is_tarjetas=None, schedule=None):
    """Mensaje para el cliente cuando no se puede comprar, o '' si sí se puede."""
    if not package or not package.is_active:
        return 'Este paquete ya no está disponible.'
    if package_is_out_of_stock(
        package, auto_mapped_ids=auto_mapped_ids, is_tarjetas=is_tarjetas
    ):
        return 'Este paquete está agotado por ahora. Elige otro o vuelve a intentarlo más tarde.'
    if package_is_closed_now(
        package, auto_mapped_ids=auto_mapped_ids, is_tarjetas=is_tarjetas, schedule=schedule
    ):
        schedule = schedule or get_manual_schedule()
        return (
            'Este paquete se recarga a mano y ahora estamos cerrados. '
            f'Atendemos de {format_hour(schedule["open_hour"])} a '
            f'{format_hour(schedule["close_hour"])}, hora de Venezuela.'
        )
    return ''
