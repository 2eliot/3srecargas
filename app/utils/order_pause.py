"""Pausa de emergencia de una orden (botón "Pausar" del admin).

Cuando un cliente escribe mal el ID y el sistema no lo detecta, la entrega automática sigue mandando peticiones al proveedor con ese ID: el primer
intento al verificar el pago, los reintentos del scheduler cada pocos minutos, el botón "Aprobar", el webhook de Revendedores... Pausar una orden pone
`orders.is_paused = True` y TODO lo automático que manda una recarga deja de tocarla hasta que el admin la reanude.

Lo que la pausa frena:
- `approve_order` y `process_order_delivery` (el único camino hacia Revendedores, Central One y el bot de PINs);
- el recorrido del scheduler y la verificación automática del pago (no se toca la orden);
- cada paso de una orden con varios pasos: se revisa justo antes de CADA envío, así que una pausa a mitad de camino detiene los pasos que faltan
  (lo ya entregado queda guardado: al reanudar no se repite).

Lo que NO frena, a propósito: rechazar la orden, marcarla como entregada a mano ("Forzar aprobación"), editar sus datos, las notas y los mensajes
al cliente. Son decisiones del admin y no mandan nada al proveedor.
"""
from datetime import datetime

from ..models import Order, db
from .timezone import format_ve

PAUSED_MESSAGE = ('Esta orden está en PAUSA: el sistema no la procesa ni manda peticiones al proveedor. '
                  'Corrige lo que haga falta y toca "Reanudar" en el detalle de la orden.')
# Lo que ve el CLIENTE en su página mientras la orden está pausada (no se le habla de "pausa": para él es una revisión).
CUSTOMER_MESSAGE = 'Tu orden está siendo revisada por nuestro equipo. Te avisamos en cuanto esté lista.'


def order_paused_now(order):
    """¿La orden está en pausa AHORA MISMO?

    Lee el valor de la base de datos, no el que quedó en memoria: el admin puede pausar mientras otra solicitud (el scheduler, el polling del cliente)
    ya tiene cargada la orden. Sin autoflush para no mandar a la base cambios a medias de quien llama."""
    if order is None or getattr(order, 'id', None) is None:
        return False
    try:
        with db.session.no_autoflush:
            return bool(db.session.query(Order.is_paused).filter(Order.id == order.id).scalar())
    except Exception:
        return bool(getattr(order, 'is_paused', False))


def paused_result(order=None):
    """Resultado que se devuelve en lugar de procesar una orden pausada (misma forma que `process_order_delivery`)."""
    return {
        'ok': False,
        'changed': False,
        'pending_verification': False,
        'paused': True,
        'message': PAUSED_MESSAGE,
        'category': 'warning',
    }


def _stamp():
    return format_ve(datetime.utcnow(), '%d/%m/%Y %H:%M')


def _add_note(order, text):
    order.notes = ((order.notes or '') + '\n' + text).strip()


def pause_order(order):
    """Pausa la orden. Devuelve {'ok', 'changed', 'message', 'category'} para mostrarle al admin."""
    if order.status != 'pending':
        return {'ok': False, 'changed': False, 'message': 'Solo se pueden pausar órdenes pendientes.', 'category': 'warning'}
    if order.is_paused:
        return {'ok': True, 'changed': False, 'message': f'La orden #{order.order_number} ya estaba en pausa.', 'category': 'info'}
    order.is_paused = True
    _add_note(order, f'[PAUSA {_stamp()}] Orden pausada por el admin: el sistema no manda más peticiones al proveedor con esta orden hasta que se reanude.')
    order.updated_at = datetime.utcnow()
    db.session.commit()
    return {
        'ok': True,
        'changed': True,
        'message': f'Orden #{order.order_number} en PAUSA: el sistema ya no manda peticiones al proveedor con ella.',
        'category': 'warning',
    }


def resume_order(order):
    """Reanuda la orden: vuelve a poder procesarse sola. No dispara nada en el momento; sigue su camino normal."""
    if not order.is_paused:
        return {'ok': True, 'changed': False, 'message': f'La orden #{order.order_number} no estaba en pausa.', 'category': 'info'}
    order.is_paused = False
    _add_note(order, f'[REANUDADA {_stamp()}] El admin reanudó la orden: el sistema vuelve a procesarla.')
    order.updated_at = datetime.utcnow()
    db.session.commit()
    return {
        'ok': True,
        'changed': True,
        'message': f'Orden #{order.order_number} reanudada: el sistema vuelve a procesarla.',
        'category': 'success',
    }
