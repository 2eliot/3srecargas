"""Verificación de pagos de Binance Pay POR REFERENCIA (igual que Pago Móvil).

El cliente paga a los datos de Binance que el dueño pone en el método de pago (correo o Binance ID), sube el comprobante o escribe la
referencia (el ID de la operación que muestra Binance), y aquí se busca esa referencia en el historial de pagos RECIBIDOS de la cuenta
(API de Binance Pay, llave de solo lectura en el .env: BINANCE_API_KEY / BINANCE_API_SECRET). Si todo coincide, la orden se aprueba sola.

Lo que se comprueba antes de dar el pago por bueno:
- la referencia es el ID COMPLETO de la operación (transactionId / orderId), como pide el dueño: NO se acepta una referencia más corta (como solo la parte
  final, que en Pago Móvil dejaba pasar referencias cualquiera). Solo se perdonan las letras del inicio ("M_P_71505104267788288" y "71505104267788288" son
  el mismo ID) y los guiones, espacios y mayúsculas;
- es un pago RECIBIDO, en USDT (no uno que salió de la cuenta ni otra moneda);
- el monto cubre el precio de la orden con una tolerancia de 2 % (mínimo aceptado = 98 % del precio; si pagó de más, se acepta);
- ese pago no se usó antes en otra orden;
- el pago es de hasta 48 horas antes de la orden (el cliente suele pagar y enviar la orden horas después) o posterior; no se aceptan pagos más viejos.
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
# El cliente suele pagar ANTES de confirmar la orden (a veces paga en la mañana y la envía en la noche): se miran los pagos desde 48 horas antes de que se creó
# (pedido del dueño, 2026-10-17; antes eran 3 horas). Sigue acotado por MAX_WINDOW_DAYS.
LOOKBACK_BEFORE_ORDER_MINUTES = 48 * 60
MAX_WINDOW_DAYS = 7
PAGE_LIMIT = 100          # la API entrega como máximo 100 pagos por consulta
MAX_SPLIT_DEPTH = 6       # si llegan 100, la ventana se parte a la mitad hasta 6 veces (con la ventana de 48 horas, tramos de unos 50 minutos; solo se parte si hay 100 o más pagos)
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


def _id_core(tid):
    """El ID sin las letras del inicio ("MP71505104267788288" -> "71505104267788288"): el cliente a veces copia solo la parte de números COMPLETA."""
    return re.sub(r'^[A-Z]+', '', tid)


def reference_matches(reference, tx):
    """La referencia tiene que ser el ID COMPLETO de la operación (o ese ID sin las letras del inicio). Una referencia MÁS CORTA (la parte final, por ejemplo)
    NO coincide: así nadie acierta un pago poniendo unos pocos números. Si el cliente escribió de MÁS (un prefijo, "Order ID: ..."), el ID completo está dentro y sí."""
    ref = normalize_reference(reference)
    if len(ref) < MIN_REFERENCE_LENGTH:
        return False
    for tid in _tx_ids(tx):
        core = _id_core(tid)
        if tid == ref or (len(core) >= MIN_REFERENCE_LENGTH and core == ref):
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
        shown = ', '.join(sorted(tx_ids)[:5])
        return _result(f'ATENCIÓN, REFERENCIA REPETIDA: esa referencia coincide con más de un pago de Binance ({len(tx_ids)} pagos: {shown}). '
                       'No se aprueba sola: un operador la revisa a mano.')
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
    other = find_order_using_payment_id(tx_id, order.id)
    if other is None and tx_id in claimed:
        from ..models import Order
        other = Order.query.filter(Order.binance_tx_id == tx_id, Order.id != order.id).first()
    if tx_id in claimed or other is not None:
        # Se avisa en las notas de la orden (lo hace quien llama con este mensaje): qué pago es y en qué orden ya se usó.
        who = f' (orden #{other.order_number})' if other is not None and getattr(other, 'order_number', None) else ''
        return _result(f'ATENCIÓN, PAGO YA USADO: ese pago de Binance (ID {tx_id}) ya fue usado en otra orden{who}. '
                       'No se aprueba sola: un operador debe revisar si el cliente repitió la referencia de un pago anterior.', ok=False)

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
