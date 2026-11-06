"""Etiquetas rojas de la lista de órdenes del admin. Solo AVISAN: no bloquean ni cambian nada de la orden.

Marcan las órdenes cuyo pago conviene mirar antes de aprobar (o que ya se entregaron siendo dudosas):
- "Pago ya usado": Pabilo dijo que el pago de esa referencia ya cuenta en otra orden y la orden no quedó verificada.
- "Referencia repetida": la referencia es igual a la de una orden ANTERIOR del mismo método de pago, o es el final de
  ella (el mismo pago escrito con menos dígitos).
- "Pago incompleto": lo pagado en Bs no alcanza el precio de la orden.
"""
import json
import re
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import or_

from ..models import Order, db
from .order_states import LIVE_PAYMENT_STATUSES
from .payment_verification import (
    PABILO_MIN_ACCEPTANCE_RATIO,
    counted_remainder_references,
    find_reference_conflict,
    normalize_reference_key,
    order_payment_ids,
)

LIVE_STATUSES = ('pending', 'approved', 'completed')
REPEAT_WINDOW_DAYS = 60
MIN_REFERENCE_DIGITS = 6
USED_NOTE = 'Ese pago ya fue usado'
# Búsquedas por referencia (una consulta cada una) que se permiten por llamada para ubicar la orden de "Pago ya usado".
USED_REFERENCE_LOOKUP_LIMIT = 5


def _digits(reference):
    return ''.join(ch for ch in str(reference or '') if ch.isdigit())


def _decimal(value):
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _pabilo_payments(order):
    """Pagos de Pabilo (lo que trae `user_bank_payment`: id, monto, referencia del banco, fecha) en la última respuesta guardada de la orden:
    el pago que Pabilo encontró para su referencia."""
    raw = order.payment_verification_raw_response
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    payments = []

    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            payment = node.get('user_bank_payment')
            if isinstance(payment, dict) and payment.get('id'):
                payment_id = str(payment['id']).strip()
                if payment_id and all(p['id'] != payment_id for p in payments):
                    payments.append({**payment, 'id': payment_id})
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(data)
    return payments


def _pabilo_payment_ids(order):
    """Ids de pago de Pabilo que trae la última respuesta guardada de la orden."""
    return [payment['id'] for payment in _pabilo_payments(order)]


def _is_used_candidate(order):
    return order.payment_verified_at is None and USED_NOTE in (order.notes or '')


def _lookup_used_payment_orders(orders):
    """{order.id: la OTRA orden viva donde ya cuenta el pago de esa orden} (solo las que se encuentran).

    Primero por el id de pago que Pabilo devolvió (es lo que "Ese pago ya fue usado" compara: sirve aunque la referencia esté escrita
    distinto o el pago esté allá como "pago restante"): UNA sola consulta para todas las órdenes. Las que no se resuelven así se buscan por
    su referencia (una consulta cada una, con tope), para que una página con muchas órdenes marcadas no multiplique las consultas."""
    orders = [o for o in orders if o is not None]
    if not orders:
        return {}
    ids_by_order = {o.id: _pabilo_payment_ids(o) for o in orders}
    all_ids = sorted({pid for ids in ids_by_order.values() for pid in ids})
    candidates = []
    if all_ids:
        conditions = [Order.payment_verification_id.in_(all_ids)] + [Order.remainder_verification_ids.contains(pid) for pid in all_ids]
        candidates = Order.query.filter(Order.status.in_(LIVE_PAYMENT_STATUSES), or_(*conditions)).order_by(Order.id.desc()).all()

    found = {}
    for order in orders:
        for payment_id in ids_by_order[order.id]:
            match = next((c for c in candidates if c.id != order.id and payment_id in order_payment_ids(c)), None)
            if match is not None:
                found[order.id] = match
                break

    lookups = 0
    for order in orders:
        if order.id in found or lookups >= USED_REFERENCE_LOOKUP_LIMIT:
            continue
        reference = (order.payment_reference or '').strip()
        if not reference or not order.payment_method:
            continue
        lookups += 1
        other = find_reference_conflict(reference, order.payment_method, exclude_order_id=order.id)
        if other is not None:
            found[order.id] = other
    return found


def find_used_payment_order(order):
    """La OTRA orden viva donde ya cuenta el pago de esta (None si no se encuentra)."""
    return _lookup_used_payment_orders([order]).get(order.id)


def _bs(value):
    amount = _decimal(value)
    return f'Bs {amount:.2f}' if amount is not None else ''


def _date_es(value):
    """'2026-11-04T04:00:00Z' -> '04/11/2026' ('' si no se entiende)."""
    match = re.match(r'^(\d{4})-(\d{2})-(\d{2})', str(value or ''))
    return f'{match.group(3)}/{match.group(2)}/{match.group(1)}' if match else ''


ROLE_LABELS = {
    'remainder': 'PAGO RESTANTE',
    'first_full': 'PAGO COMPLETO',
    'first_partial': 'PRIMER PAGO (PARCIAL)',
}


def describe_used_payment(order, other):
    """(rol, frase) para el admin: CÓMO cuenta ese pago en la OTRA orden (pago completo, primer pago parcial o pago restante) y de cuánto, para no
    adivinar si sirvió para pagar la orden entera o solo un resto. El monto, la referencia del banco y la fecha salen de lo que Pabilo devolvió
    para esta orden; lo demás, de lo guardado en la otra. Rol '' si no se puede saber."""
    label = f'#{other.order_number} ({other.status_label})'
    payments = {p['id']: p for p in _pabilo_payments(order)}
    other_ids = order_payment_ids(other)
    payment_id = next((pid for pid in payments if pid in other_ids), '')
    payment = payments.get(payment_id) or {}
    amount = _decimal(payment.get('amount')) if payment.get('amount') is not None else None
    if amount is not None and amount <= 0:
        amount = None

    role = ''
    if payment_id:
        role = 'first' if (other.payment_verification_id or '').strip() == payment_id else 'remainder'
    else:
        mine = normalize_reference_key(order.payment_reference)
        if mine and mine == normalize_reference_key(other.payment_reference):
            role = 'first'
        elif mine and mine in {normalize_reference_key(ref) for ref in [other.remainder_reference] + counted_remainder_references(other)}:
            role = 'remainder'

    details = []
    if payment.get('bank_reference_id'):
        details.append(f"ref. del banco {payment['bank_reference_id']}")
    when = _date_es(payment.get('movement_date') or payment.get('created_at'))
    if when:
        details.append(f'del {when}')
    who = f"Ese pago ({', '.join(details)})" if details else 'Ese pago'
    amount_txt = f' de {_bs(amount)}' if amount is not None else ''
    expected = _decimal(other.payment_amount) if (other.payment_currency or '').lower() == 'bs' else None
    total_txt = f' La orden es de {_bs(expected)} en total.' if expected is not None and expected > 0 else ''

    if role == 'remainder':
        partial = re.search(r'llegó Bs ([\d.]+) de Bs ([\d.]+)', other.notes or '')
        first_txt = f' Esa orden pagó primero {_bs(partial.group(1))} y este pago fue el resto.' if partial else ''
        return 'remainder', f'{who} se usó como PAGO RESTANTE{amount_txt} para completar la orden {label}.{first_txt}{total_txt}'
    if role == 'first':
        if other.awaiting_payment_completion:
            return 'first_partial', f'{who} se usó como el PRIMER PAGO (parcial){amount_txt} de la orden {label}, que todavía espera el resto.{total_txt}'
        if len(other_ids) > 1 or counted_remainder_references(other):
            return 'first_partial', f'{who} se usó como el PRIMER PAGO (parcial){amount_txt} de la orden {label}, que luego se completó con pagos restantes.{total_txt}'
        return 'first_full', f'{who} se usó como el PAGO COMPLETO{amount_txt} de la orden {label}.{total_txt}'
    return '', f'{who} ya cuenta en la orden {label}.{total_txt}'


def _used_flag(order, other=None):
    if not _is_used_candidate(order):
        return None
    flag = {
        'code': 'used',
        'label': '⚠ Pago ya usado',
        'title': 'Pabilo dijo que el pago de esta referencia ya se usó en otra orden y esta orden no quedó verificada. Revísalo antes de aprobar.',
    }
    if other is not None:
        # Con enlace a esa orden (la lista y el detalle lo dibujan igual que el de "Referencia repetida").
        role, detail = describe_used_payment(order, other)
        flag.update({
            'order_id': other.id,
            'order_number': other.order_number,
            'order_status': other.status_label,
            'role': role,
            'role_label': ROLE_LABELS.get(role, ''),
            'detail': detail,
            'title': (f'Pabilo dijo que el pago de esta referencia ya cuenta en la orden #{other.order_number} ({other.status_label}) y esta orden '
                      f'no quedó verificada. {detail} Haz clic para abrir esa orden y comparar antes de aprobar.'),
        })
    return flag


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
            # Las devueltas también cuentan como "orden anterior con esa referencia": ese pago quedó quemado.
            Order.status.in_(LIVE_PAYMENT_STATUSES),
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
    # La orden donde ya cuenta el pago, de todas las "Pago ya usado" a la vez (sin consultas de más si no hay ninguna).
    used_orders = _lookup_used_payment_orders([o for o in orders if o.status in LIVE_STATUSES and o.payment_method != 'prize' and _is_used_candidate(o)])
    result = {}
    for order in orders:
        if order.status not in LIVE_STATUSES or order.payment_method == 'prize':
            continue
        found = [flag for flag in (_used_flag(order, used_orders.get(order.id)), repeats.get(order.id), _incomplete_flag(order)) if flag]
        if found:
            result[order.id] = found
    return result
