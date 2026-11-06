"""Devolución del dinero de una orden (botón "Devolución" del detalle de la orden, en el admin).

Qué hace una devolución:
- La orden pasa a 'refunded' ("Devuelta"). Ese estado SIGUE reservando el pago (ver utils/order_states.py): la referencia y el id de pago de
  Pabilo quedan quemados y nadie puede volver a usarlos en otra orden. Rechazar, en cambio, libera el pago.
- Se guarda un registro (OrderRefund) con el total devuelto, la referencia de la transferencia de vuelta, el motivo y el comprobante opcional.
- El cliente lo ve en su orden y en la campana, y le llega el correo (con el comprobante adjunto si lo hay).

Reglas del dueño (2026-11-06):
- NUNCA se devuelve una orden ya completada: la recarga ya se entregó.
- Siempre se devuelve el TOTAL que pagó el cliente, nunca una parte.
- Los puntos solo se entregan al completarse la orden: una orden devuelta nunca los tuvo, no hay nada que quitar.
- El comprobante es opcional, igual que en los otros botones.

Seguridad: antes de devolver el dinero se confirma que el proveedor NO entregó la recarga (si no, el cliente se quedaría con las dos cosas) y que
ese mismo pago no lo está usando otra orden. Todo corre bajo el mismo candado por orden que "Aprobar" y "Forzar", así que no puede cruzarse con una
entrega en curso ni con otro clic.
"""
import logging
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from ..models import OrderRefund, Pin, db
from .locks import acquire_lock, release_lock
from .order_states import REFUNDABLE_STATUSES
from .timezone import format_ve

logger = logging.getLogger(__name__)

# Motivo de la devolución (solo para el registro y las estadísticas; al cliente se le dice lo mismo en todos los casos).
REFUND_REASONS = {
    'proveedor': 'El proveedor no pudo entregar la recarga',
    'id_incorrecto': 'ID o dato del jugador incorrecto',
    'cliente': 'El cliente la pidió',
    'duplicado': 'Pago duplicado o de más',
    'otro': 'Otro motivo',
}
NOTE_MAX = 500
REFERENCE_MAX = 120
_CENT = Decimal('0.01')


def clean_reason(value):
    value = str(value or '').strip().lower()
    return value if value in REFUND_REASONS else 'otro'


def reason_label(code):
    return REFUND_REASONS.get(code or 'otro', REFUND_REASONS['otro'])


def _money(value):
    try:
        return Decimal(str(value if value is not None else 0)).quantize(_CENT, rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal('0.00')


def order_refund_total(order):
    """(monto, moneda) = el TOTAL que pagó el cliente.

    Bs: lo que el banco ya confirmó (puede sumar varios pagos si pagó en partes); si todavía no hay nada confirmado, el monto que se le pidió. USD:
    el monto de la orden. Moneda 'bs' o 'usd'."""
    currency = (order.payment_currency or '').strip().lower()
    if currency == 'bs':
        paid = _money(order.paid_amount_bs)
        if paid > 0:
            return paid, 'bs'
        asked = _money(order.payment_amount)
        if asked > 0:
            return asked, 'bs'
    elif currency == 'usd':
        asked = _money(order.payment_amount)
        if asked > 0:
            return asked, 'usd'
    return _money(order.amount), 'usd'


def format_refund_amount(amount, currency):
    amount = _money(amount)
    return f'${amount}' if (currency or '').lower() == 'usd' else f'Bs {amount}'


def refund_payment_warning(order):
    """Aviso para el admin ANTES de devolver (no bloquea): el pago no está verificado, así que puede que el dinero nunca haya llegado."""
    if order.payment_verified_at or (order.paid_amount_bs or 0) > 0:
        return ''
    return ('El pago de esta orden NO está verificado por el banco. Confirma que el dinero de verdad llegó a tu cuenta '
            'antes de devolverlo.')


# ─── ¿El proveedor ya entregó algo? ────────────────────────────────────────────

def _check_revendedores_step(order, step, base_url, api_key):
    """'' si Revendedores confirmó que ese intento NO entregó; si no, el motivo."""
    from .order_processing import _check_revendedores_attempt_status

    attempts = int(step.get('rev_attempt') or 0)
    external_id = str(step.get('external_order_id') or '').strip()
    if not external_id and attempts:
        external_id = f"{order.order_number}-s{int(step.get('slot') or 1)}-{attempts}"
    if not external_id:
        return ''
    if not base_url or not api_key:
        return 'esta orden tiene un intento con Revendedores y su llave no está configurada para confirmar cómo terminó'
    data, answered = _check_revendedores_attempt_status(base_url, api_key, external_id)
    if not answered:
        return f'no se pudo confirmar con Revendedores cómo terminó el intento {external_id} (inténtalo de nuevo en un momento)'
    if not data:
        return ''  # Revendedores no lo conoce: nunca se creó allá
    status = str(data.get('status') or '').strip().lower()
    if status == 'completada':
        return f'Revendedores ya entregó la recarga (intento {external_id})'
    if status == 'procesando':
        return f'Revendedores todavía está procesando la recarga (intento {external_id})'
    if status == 'fallida':
        return ''
    return f'Revendedores respondió un estado que no se reconoce ({status or "vacío"}) para el intento {external_id}'


def _check_centralone_step(step):
    from .central_one import CentralOneError, CentralOneUnknown, _remote_outcome, fetch_order

    remote_id = str(step.get('remote_order_id') or '').strip()
    if not remote_id:
        if str(step.get('idempotency_key') or '').strip():
            return 'hay una recarga sin confirmar con Central One (se mandó pero no se supo cómo terminó)'
        return ''
    try:
        remote = fetch_order(remote_id)
    except CentralOneError as exc:
        if exc.code == 'not_found':
            return ''
        return f'no se pudo confirmar con Central One cómo terminó la recarga ({exc.message or exc.code})'
    except CentralOneUnknown:
        return 'no se pudo confirmar con Central One cómo terminó la recarga (inténtalo de nuevo en un momento)'
    outcome = _remote_outcome(remote or {})
    if outcome == 'delivered':
        return 'Central One ya entregó la recarga'
    if outcome == 'pending':
        return 'Central One todavía está procesando la recarga'
    return ''  # 'refunded' o 'failed': el proveedor confirmó que no entregó


def provider_refund_blocker(order):
    """Motivo por el que NO se puede devolver el dinero porque el proveedor entregó (o pudo entregar) la recarga; '' si no hay rastro.

    Mira lo que la orden tiene guardado y, si hay un intento sin cerrar, le pregunta al proveedor cómo terminó. Si no se puede saber, bloquea: es peor
    devolver el dinero de una recarga que sí llegó que esperar un minuto."""
    from .order_processing import _load_delivery_state, get_revendedores_env

    state = _load_delivery_state(order)
    if state.get('manual_override'):
        return 'esta orden se marcó como entregada a mano'
    if state.get('success') is True:
        return 'el sistema ya registró esta orden como entregada'
    if (order.pin_delivered or '').strip() or Pin.query.filter_by(order_id=order.id, is_used=True).first():
        return 'a esta orden ya se le entregó un código'

    steps = [s for s in (state.get('steps') or []) if isinstance(s, dict)]
    if not steps and state.get('source') == 'revendedores_api' and (state.get('external_order_id') or state.get('rev_attempt')):
        steps = [state]  # órdenes viejas de un solo paso, sin lista de pasos
    delivered = sum(1 for s in steps if s.get('success'))
    if delivered:
        return f'el proveedor ya entregó {delivered} recarga(s) de esta orden'

    base_url, api_key, _, _ = get_revendedores_env()
    for step in steps:
        if step.get('remote_order_id') or step.get('idempotency_key'):
            blocker = _check_centralone_step(step)
        else:
            blocker = _check_revendedores_step(order, step, base_url, api_key)
        if blocker:
            return blocker
    return ''


def payment_reuse_blocker(order):
    """Motivo si otra orden VIVA ya usa este mismo pago (referencia o id de Pabilo); '' si no.

    Una orden rechazada libera su pago y otra pudo usarlo después: devolver ese dinero sería devolver un pago que ya sirvió para otra compra."""
    from .payment_verification import find_order_using_payment_id, find_reference_conflict

    other = None
    if (order.payment_reference or '').strip():
        other = find_reference_conflict(order.payment_reference, order.payment_method, exclude_order_id=order.id)
    if other is None and order.payment_verification_id:
        other = find_order_using_payment_id(order.payment_verification_id, order.id)
    if other is not None:
        return (f'este mismo pago ya lo usa la orden #{other.order_number} ({other.status_label}): no se devuelve dinero de un pago '
                'que sirvió para otra orden. Si esta es la orden repetida, recházala.')
    return ''


# ─── La devolución ─────────────────────────────────────────────────────────────

def _fail(message, category='warning'):
    return {'ok': False, 'changed': False, 'message': message, 'category': category, 'refund': None}


def refund_blocker(order):
    """Todo lo que impide la devolución, en orden, sin cambiar nada ('' si se puede). Es lo que se revisa dentro del candado."""
    if order.status == 'refunded':
        return f'La orden #{order.order_number} ya fue devuelta.'
    if order.status == 'completed':
        return 'Esta orden ya fue completada (entregada): no se puede devolver.'
    if order.status not in REFUNDABLE_STATUSES:
        return 'Solo se pueden devolver órdenes pendientes o rechazadas.'
    amount, _currency = order_refund_total(order)
    if amount <= 0:
        return 'No se pudo calcular cuánto pagó el cliente en esta orden.'
    reason = payment_reuse_blocker(order)
    if reason:
        return f'No se puede devolver: {reason}'
    reason = provider_refund_blocker(order)
    if reason:
        return f'No se puede devolver todavía: {reason}.'
    return ''


def refund_order(order, reason='otro', note='', reference='', proof_path=None):
    """Devuelve el dinero de la orden (siempre el total). Devuelve {'ok','changed','message','category','refund'}.

    El llamador guarda `proof_path` (foto opcional del comprobante) y la borra si el resultado no es ok."""
    if order.status not in REFUNDABLE_STATUSES:
        return _fail(refund_blocker(order))

    from .order_processing import ORDER_APPROVE_LOCK_TTL_SECONDS

    lock_key = f'order_approve:{order.id}'
    lock_holder = uuid4().hex
    if not acquire_lock(lock_key, ORDER_APPROVE_LOCK_TTL_SECONDS, lock_holder):
        return _fail('Esta orden ya se está procesando en este momento (otra solicitud en curso). Intenta de nuevo en unos segundos.')

    try:
        db.session.refresh(order)
        blocker = refund_blocker(order)
        if blocker:
            return _fail(blocker)

        amount, currency = order_refund_total(order)
        reason = clean_reason(reason)
        note = ' '.join(str(note or '').split())[:NOTE_MAX]
        reference = ' '.join(str(reference or '').split())[:REFERENCE_MAX]
        was_paused = bool(order.is_paused)

        # Un PIN apartado para esta orden vuelve al stock: la orden ya no lo va a usar.
        reserved_pin = Pin.query.filter_by(order_id=order.id, is_used=False).first()
        if reserved_pin:
            reserved_pin.order_id = None

        refund = OrderRefund(
            order_id=order.id, amount=amount, currency=currency, reference=reference or None,
            reason=reason, note=note or None, proof=proof_path or None,
        )
        db.session.add(refund)
        try:
            db.session.flush()  # la orden solo puede tener UNA devolución (order_id único): si ya hay una, aquí se ve
        except IntegrityError:
            db.session.rollback()
            return _fail(f'La orden #{order.order_number} ya tenía una devolución registrada.')

        lines = [f'[DEVOLUCIÓN {format_ve(datetime.utcnow(), "%d/%m/%Y %H:%M")}] Se devolvió {format_refund_amount(amount, currency)} al cliente '
                 f'(motivo: {reason_label(reason)}).']
        if reference:
            lines.append(f'Referencia de la devolución: {reference}.')
        if note:
            lines.append(f'Nota: {note}')
        lines.append('El pago original queda quemado: no se puede usar en otra orden.')
        if was_paused:
            lines.append('(La orden estaba en pausa.)')
        opportunity = getattr(order, 'minigame_opportunity', None)
        if opportunity is not None and (opportunity.status or 'available') != 'available':
            lines.append('Ojo: esta orden ya había jugado su minijuego (el premio ya se entregó).')
        order.notes = ((order.notes or '') + '\n' + ' '.join(lines)).strip()
        order.status = 'refunded'
        order.is_paused = False
        order.updated_at = datetime.utcnow()
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return _fail(f'La orden #{order.order_number} ya tenía una devolución registrada.')
        return {
            'ok': True,
            'changed': True,
            'message': f'Devolución registrada: {format_refund_amount(amount, currency)} de la orden #{order.order_number}. '
                       'El pago quedó quemado y el cliente ya puede verlo.',
            'category': 'success',
            'refund': refund,
        }
    except Exception:
        db.session.rollback()
        logger.exception('Error al registrar la devolución de la orden %s', getattr(order, 'id', None))
        raise
    finally:
        release_lock(lock_key, lock_holder)


def refund_for_order(order):
    """La devolución de la orden (o None). Para pintarla en la página del cliente y en el admin."""
    if order is None or order.status != 'refunded':
        return None
    return getattr(order, 'refund', None)
