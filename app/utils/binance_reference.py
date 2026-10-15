"""Verificación de pagos de Binance Pay POR REFERENCIA (igual que Pago Móvil).

El cliente paga a los datos de Binance que el dueño pone en el método de pago (correo o Binance ID), sube el comprobante o escribe la
referencia (el ID de la operación que muestra Binance), y aquí se busca esa referencia en el historial de pagos RECIBIDOS de la cuenta
(API de Binance Pay, llave de solo lectura en el .env: BINANCE_API_KEY / BINANCE_API_SECRET). Si todo coincide, la orden se aprueba sola.

Lo que se comprueba antes de dar el pago por bueno:
- la referencia aparece en el historial como ID de la operación (transactionId / orderId); se acepta el ID completo o su parte final
  (mínimo 8 caracteres) porque el cliente a veces copia solo los números;
- es un pago RECIBIDO, en USDT (no uno que salió de la cuenta ni otra moneda);
- el monto cubre el precio de la orden con una tolerancia de 2 % (mínimo aceptado = 98 % del precio; si pagó de más, se acepta);
- ese pago no se usó antes en otra orden;
- el pago es de las horas anteriores a la orden o posterior (no se aceptan pagos viejos).
Si algo no cuadra NO se aprueba solo: queda una nota para que el administrador lo revise a mano.

El resultado tiene la misma forma que el de Pabilo (`verify_order_payment`), así que el resto del sistema (aprobar, notas, reintentos,
puntos, recarga) funciona exactamente igual que con un pago del banco.
"""
import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from flask import current_app

from . import binance_pay as BP

PROVIDER = 'Binance'
# Tolerancia del 2 %: el pago puede llegar hasta 2 % por debajo del precio (comisiones, redondeos) y se acepta igual.
BINANCE_MIN_ACCEPTANCE_RATIO = Decimal('0.98')
MIN_REFERENCE_LENGTH = 8
# El cliente suele pagar ANTES de confirmar la orden: se miran los pagos desde unas horas antes de que se creó.
LOOKBACK_BEFORE_ORDER_MINUTES = 180
MAX_WINDOW_DAYS = 7
PAGE_LIMIT = 100          # la API entrega como máximo 100 pagos por consulta
MAX_SPLIT_DEPTH = 3       # si llegan 100, la ventana se parte a la mitad hasta 3 veces (8 consultas como máximo)
ID_KEYS = ('transactionId', 'orderId', 'prepayId', 'merchantTradeNo')


def is_binance_reference_method(payment_method):
    """¿Este método de pago se verifica con la API de Binance? (casilla del método en Admin → Métodos de pago)."""
    return bool(payment_method is not None and getattr(payment_method, 'binance_verify', False))


def normalize_reference(value):
    """Solo letras y números en mayúsculas: "M_P_7150-5104" y "mp71505104" son la misma referencia."""
    return re.sub(r'[^A-Z0-9]', '', str(value or '').upper())


def _tx_ids(tx):
    ids = []
    for key in ID_KEYS:
        value = normalize_reference(tx.get(key))
        if value and value not in ids:
            ids.append(value)
    return ids


def reference_matches(reference, tx):
    """La referencia es el ID de la operación o su parte final (o el ID es la parte final de lo que escribió el cliente)."""
    ref = normalize_reference(reference)
    if len(ref) < MIN_REFERENCE_LENGTH:
        return False
    for tid in _tx_ids(tx):
        if tid == ref:
            return True
        if tid.endswith(ref):
            return True
        if len(tid) >= 12 and ref.endswith(tid):
            return True
    return False


def _history(cfg, start_ms, end_ms, depth=0):
    """Pagos entre `start_ms` y `end_ms`. Si la API entrega el tope (100) puede haber más: se parte la ventana y se juntan los resultados."""
    txs = BP._fetch_pay_transactions(cfg['api_key'], cfg['api_secret'], cfg['proxy'], cfg['timeout'], start_ms, PAGE_LIMIT, end_ms)
    if txs is None:
        return None
    if len(txs) < PAGE_LIMIT or depth >= MAX_SPLIT_DEPTH or end_ms - start_ms < 60_000:
        return txs
    mid = (start_ms + end_ms) // 2
    first = _history(cfg, start_ms, mid, depth + 1)
    second = _history(cfg, mid + 1, end_ms, depth + 1)
    if first is None or second is None:
        return None
    merged, seen = [], set()
    for tx in list(txs) + list(first) + list(second):
        key = (BP._tx_id(tx), BP._tx_time_ms(tx), str(BP._tx_amount(tx)))
        if key not in seen:
            seen.add(key)
            merged.append(tx)
    return merged


def expected_usdt(order):
    """Lo que debe pagar el cliente en USDT (el precio de la orden en dólares, ya con descuento)."""
    try:
        if (order.payment_currency or '').lower() == 'usd' and order.payment_amount and float(order.payment_amount) > 0:
            value = Decimal(str(order.payment_amount))
        else:
            value = Decimal(str(order.amount or 0))
    except Exception:
        return None
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) if value > 0 else None


def _result(message, verified=False, ok=True, **extra):
    data = {'ok': ok, 'verified': verified, 'provider': PROVIDER, 'message': message}
    data.update(extra)
    return data


def _money(value):
    return f"{Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def verify_order_by_reference(order, app=None):
    """Busca en Binance el pago de esta orden por su referencia. Devuelve el mismo tipo de resultado que `verify_order_payment` (Pabilo)."""
    from .payment_verification import find_order_using_payment_id, get_order_reference_candidates

    app = app or current_app._get_current_object()
    cfg = BP._binance_settings(app)
    if not cfg['api_key'] or not cfg['api_secret']:
        return _result('Falta cargar la llave de la API de Binance en el servidor (BINANCE_API_KEY y BINANCE_API_SECRET en el archivo .env): '
                       'no se puede verificar el pago solo. Un operador lo revisa a mano.', ok=False, requestable=False)

    candidates = []
    for candidate in get_order_reference_candidates(order):
        normalized = normalize_reference(candidate['reference'])
        if len(normalized) >= MIN_REFERENCE_LENGTH and all(normalized != c['normalized'] for c in candidates):
            candidates.append({'normalized': normalized, 'reference': candidate['reference'], 'source': candidate['source']})
    if not candidates:
        return _result(f'La referencia de Binance es muy corta o no se leyó (mínimo {MIN_REFERENCE_LENGTH} caracteres): '
                       'no se consulta. Un operador la revisa a mano.', ok=False, requestable=False)

    expected = expected_usdt(order)
    if expected is None:
        return _result('La orden no tiene un monto válido en dólares para comparar con el pago de Binance.', ok=False, requestable=False)

    created = order.created_at or datetime.utcnow()
    now = datetime.utcnow()
    start = max(created - timedelta(minutes=LOOKBACK_BEFORE_ORDER_MINUTES), now - timedelta(days=MAX_WINDOW_DAYS))
    start_ms, end_ms = BP._to_epoch_ms(start), BP._to_epoch_ms(now + timedelta(minutes=5))
    history = _history(cfg, start_ms, end_ms)
    if history is None:
        return _result('No se pudo consultar Binance en este momento. Se vuelve a intentar solo.')

    matched = [tx for tx in history if any(reference_matches(c['reference'], tx) for c in candidates)]
    if not matched:
        return _result('El pago todavía no aparece en Binance.')
    tx_ids = {BP._tx_id(tx) for tx in matched}
    if len(tx_ids) > 1:
        return _result('Esa referencia coincide con más de un pago de Binance: un operador la revisa a mano.')
    tx = matched[0]
    tx_id = BP._tx_id(tx)
    used_by = candidates[0]
    for c in candidates:
        if reference_matches(c['reference'], tx):
            used_by = c
            break

    if not BP._is_incoming_usdt(tx):
        currency = BP._tx_currency(tx) or 'otra moneda'
        direction = 'saliente' if BP._tx_amount(tx) < 0 else 'recibido'
        return _result(f'Ese pago de Binance no es un cobro recibido en USDT (es {direction}, moneda {currency}). Un operador lo revisa a mano.')

    claimed = BP._claimed_tx_ids()
    if tx_id in claimed or find_order_using_payment_id(tx_id, order.id):
        return _result('Ese pago de Binance ya fue usado en otra orden.', ok=False)

    reported = Decimal(str(abs(BP._tx_amount(tx))))
    minimum = (expected * BINANCE_MIN_ACCEPTANCE_RATIO).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    safe_response = {'transactionId': tx_id, 'amount': str(reported), 'currency': BP._tx_currency(tx) or 'USDT', 'transactionTime': BP._tx_time_ms(tx)}
    if reported < minimum:
        return _result(
            f'Binance recibió ${_money(reported)} pero la orden es de ${_money(expected)} (mínimo aceptado ${_money(minimum)}, 2 % de tolerancia). '
            'No se aprueba sola: un operador la revisa a mano.',
            expected_amount=_money(expected), minimum_amount=_money(minimum), reported_amount=_money(reported), response=safe_response)

    source_message = ' Se usó la referencia extraída del comprobante.' if used_by['source'] == 'ai' else ''
    return _result(
        f'Pago verificado correctamente en Binance. Monto recibido: ${_money(reported)}.{source_message}'.strip(),
        verified=True,
        verification_id=tx_id, binance_tx_id=tx_id, is_new=True,
        expected_amount=_money(expected), minimum_amount=_money(minimum), reported_amount=_money(reported),
        resolved_reference=used_by['reference'], resolved_reference_source=used_by['source'],
        response=safe_response)
