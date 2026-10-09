"""Quién puede ver el PIN de una orden de tarjetas (gift cards).

La página de la orden es pública para quien tenga su número, y el login de
clientes no pide contraseña (basta el ID o el correo de la compra), así que
desde "Mis órdenes" cualquiera que supiera el correo de otro llegaba a la
página de su gift card y veía el PIN. Ahora el PIN completo se muestra solo
en el navegador donde se hizo la compra (o el canje), o a quien escriba los
últimos dígitos de la referencia con la que pagó.
"""
import re

from flask import session

OWN_ORDERS_SESSION_KEY = 'own_orders'
OWN_ORDERS_LIMIT = 30
PIN_UNLOCK_MIN_DIGITS = 4


def remember_own_order(order_number):
    """Marca la orden como creada en este navegador."""
    number = str(order_number or '').strip()
    if not number:
        return
    owned = [n for n in (session.get(OWN_ORDERS_SESSION_KEY) or []) if n != number]
    owned.append(number)
    session[OWN_ORDERS_SESSION_KEY] = owned[-OWN_ORDERS_LIMIT:]


def is_own_order(order_number):
    return str(order_number or '').strip() in (session.get(OWN_ORDERS_SESSION_KEY) or [])


def _digits(value):
    return re.sub(r'\D', '', str(value or ''))


def reference_tail_matches(order, tail):
    """True si `tail` (mínimo 4 dígitos) es el final de alguna referencia con
    la que se pagó la orden, o del teléfono del pagador."""
    tail = _digits(tail)
    if len(tail) < PIN_UNLOCK_MIN_DIGITS:
        return False
    candidates = [
        order.payment_reference, order.ai_extracted_reference,
        order.remainder_reference, order.payer_phone,
    ] + [ref for ref in (order.remainder_references or '').split(',')]
    for candidate in candidates:
        digits = _digits(candidate)
        if len(digits) >= PIN_UNLOCK_MIN_DIGITS and digits.endswith(tail):
            return True
    return False
