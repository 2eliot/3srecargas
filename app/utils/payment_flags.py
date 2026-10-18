"""Etiquetas rojas de la lista de órdenes del admin. Solo AVISAN: no bloquean ni cambian nada de la orden.

Marcan las órdenes cuyo pago conviene mirar antes de aprobar (o que ya se entregaron siendo dudosas):
- "Pago ya usado": Pabilo dijo que el pago de esa referencia ya cuenta en otra orden y la orden no quedó verificada.
- "Referencia repetida": la referencia es igual a la de una orden ANTERIOR del mismo método de pago, o es el final de
  ella (el mismo pago escrito con menos dígitos).
- "Pago incompleto": lo pagado en Bs no alcanza el precio de la orden.
"""
from datetime import timedelta
from decimal import Decimal

from ..models import Order, db
from .payment_verification import PABILO_MIN_ACCEPTANCE_RATIO

LIVE_STATUSES = ('pending', 'approved', 'completed')
REPEAT_WINDOW_DAYS = 60
MIN_REFERENCE_DIGITS = 6
USED_NOTE = 'Ese pago ya fue usado'


def _digits(reference):
    return ''.join(ch for ch in str(reference or '') if ch.isdigit())


def _decimal(value):
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _used_flag(order):
    if order.payment_verified_at is not None or USED_NOTE not in (order.notes or ''):
        return None
    return {
        'code': 'used',
        'label': '⚠ Pago ya usado',
        'title': 'Pabilo dijo que el pago de esta referencia ya se usó en otra orden y esta orden no quedó verificada. Revísalo antes de aprobar.',
    }


def _incomplete_flag(order):
    if (order.payment_currency or '').lower() != 'bs':
        return None
    paid = _decimal(order.paid_amount_bs)
    price = _decimal(order.payment_amount)
    if paid is None or price is None or paid <= 0 or price <= 0:
        return None
    if paid >= price * PABILO_MIN_ACCEPTANCE_RATIO:
        return None
    return {
        'code': 'incomplete',
        'label': '⚠ Pago incompleto',
        'title': f'Pagó Bs {paid:.2f} de Bs {price:.2f}. Revisa si falta completar el pago antes de aprobar.',
    }


def _repeat_flags(orders):
    """{order.id: flag} para las órdenes cuya referencia choca con la de otra anterior (misma forma de pago)."""
    mine = {}
    for order in orders:
        if order.status not in LIVE_STATUSES or not order.payment_method or order.payment_method == 'prize' or not order.created_at:
            continue
        key = _digits(order.payment_reference)
        if len(key) >= MIN_REFERENCE_DIGITS:
            mine[order.id] = (order, key)
    if not mine:
        return {}

    window = timedelta(days=REPEAT_WINDOW_DAYS)
    oldest = min(order.created_at for order, _ in mine.values()) - window
    tails = {key[-MIN_REFERENCE_DIGITS:] for _, key in mine.values()}
    candidates = (
        db.session.query(Order.id, Order.order_number, Order.payment_method, Order.payment_reference, Order.created_at)
        .filter(
            Order.payment_reference_last5.in_(tails),
            Order.status.in_(LIVE_STATUSES),
            Order.payment_method != 'prize',
            Order.created_at >= oldest,
        )
        .all()
    )
    prepared = [(row, _digits(row.payment_reference)) for row in candidates]

    flags = {}
    for order_id, (order, key) in mine.items():
        best = None
        for row, other_key in prepared:
            if row.id >= order_id or row.payment_method != order.payment_method or len(other_key) < MIN_REFERENCE_DIGITS:
                continue
            if abs(order.created_at - row.created_at) > window:
                continue
            if key == other_key or key.endswith(other_key) or other_key.endswith(key):
                if best is None or row.id > best.id:
                    best = row
        if best is not None:
            flags[order_id] = {
                'code': 'repeat',
                'label': '⚠ Referencia repetida',
                'title': (f'La referencia de esta orden es igual a la de la orden #{best.order_number} (creada antes) o es el final de ella: '
                          'puede ser el mismo pago escrito con menos dígitos. Haz clic para abrir esa orden.'),
                'order_id': best.id,
                'order_number': best.order_number,
            }
    return flags


def order_payment_flags(orders):
    """{order.id: [etiqueta, ...]} para una lista de órdenes (solo se incluyen las que tienen alguna)."""
    orders = [o for o in orders if o is not None]
    if not orders:
        return {}
    repeats = _repeat_flags(orders)
    result = {}
    for order in orders:
        if order.status not in LIVE_STATUSES or order.payment_method == 'prize':
            continue
        found = [flag for flag in (_used_flag(order), repeats.get(order.id), _incomplete_flag(order)) if flag]
        if found:
            result[order.id] = found
    return result
