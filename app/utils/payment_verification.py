from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
from flask import current_app
from sqlalchemy import or_

from ..models import Order, PaymentMethod, Setting


AUTO_VERIFY_SETTING_KEY = 'auto_verify_payments'
PABILO_API_KEY_SETTING_KEY = 'pabilo_api_key'
PABILO_DEFAULT_MOVEMENT_TYPE = 'GENERIC'
PABILO_MIN_ACCEPTANCE_RATIO = Decimal('0.99')


def get_setting_value(key, default=''):
    setting = Setting.query.filter_by(key=key).first()
    if not setting:
        return default
    return setting.value or default


def is_auto_verify_enabled():
    return get_setting_value(AUTO_VERIFY_SETTING_KEY, 'false').strip().lower() == 'true'


def get_pabilo_api_key():
    return get_setting_value(PABILO_API_KEY_SETTING_KEY, '').strip()


def payment_method_uses_payer_identity_verification(payment_method):
    return bool(payment_method and getattr(payment_method, 'pabilo_requires_phone_dni', False))


def normalize_reference_last5(reference):
    raw = ''.join(ch for ch in str(reference or '') if ch.isdigit())
    if not raw:
        raw = str(reference or '').strip()
    return raw[-6:] if raw else ''


def _generate_reference_variants(reference):
    """Genera variantes de la referencia para probar contra Pabilo:
    referencia completa, últimos 6, últimos 5, últimos 4 dígitos.
    Elimina duplicados y solo incluye variantes >= 4 caracteres."""
    ref = str(reference or '').strip()
    if not ref:
        return []

    digits = ''.join(ch for ch in ref if ch.isdigit())
    variants = []

    # Siempre incluir la referencia original completa
    if ref not in variants:
        variants.append(ref)

    # Si hay dígitos, agregar truncamientos (últimos N dígitos)
    if digits:
        for n in (6, 5, 4):
            truncated = digits[-n:] if len(digits) >= n else None
            if truncated and truncated not in variants and len(truncated) >= 4:
                variants.append(truncated)

    return variants


def normalize_reference_key(reference):
    raw = str(reference or '').strip()
    if not raw:
        return ''

    digits_only = ''.join(ch for ch in raw if ch.isdigit())
    if digits_only:
        return digits_only

    return ''.join(raw.upper().split())


def get_order_reference_candidates(order):
    candidates = []
    seen = set()

    for source, reference in (
        ('manual', getattr(order, 'payment_reference', None)),
        ('ai', getattr(order, 'ai_extracted_reference', None)),
    ):
        raw_reference = str(reference or '').strip()
        if not raw_reference:
            continue

        normalized_key = normalize_reference_key(raw_reference)
        if not normalized_key or normalized_key in seen:
            continue

        seen.add(normalized_key)
        candidates.append({
            'reference': raw_reference,
            'source': source,
        })

    return candidates


def find_reference_conflict(reference, payment_method_code, exclude_order_id=None, statuses=None):
    reference_key = normalize_reference_key(reference)
    reference_last5 = normalize_reference_last5(reference)
    if not reference_key and not reference_last5:
        return None

    statuses = statuses or ['pending', 'approved', 'completed']
    query = Order.query.filter(
        Order.payment_method == payment_method_code,
        Order.status.in_(statuses),
    )

    filters = []
    raw_reference = str(reference or '').strip()
    if raw_reference:
        filters.append(Order.payment_reference == raw_reference)
        # También hay que buscarla como referencia del PAGO RESTANTE: un
        # pago que Pabilo confirmó pero no alcanzó a cubrir una orden queda
        # "reservado" igual, para que nadie lo reuse en una orden distinta
        # mientras la primera sigue esperando el resto.
        filters.append(Order.remainder_reference == raw_reference)
    if reference_last5:
        filters.append(Order.payment_reference_last5 == reference_last5)
    if filters:
        query = query.filter(or_(*filters))

    if exclude_order_id:
        query = query.filter(Order.id != exclude_order_id)

    candidates = query.order_by(Order.id.desc()).all()
    for candidate in candidates:
        candidate_key = normalize_reference_key(candidate.payment_reference)
        if reference_key and candidate_key and candidate_key == reference_key:
            return candidate
        candidate_remainder_key = normalize_reference_key(candidate.remainder_reference)
        if reference_key and candidate_remainder_key and candidate_remainder_key == reference_key:
            return candidate
        if not reference_key and reference_last5 and candidate.payment_reference_last5 == reference_last5:
            return candidate

    return None


def _get_bs_amount(order):
    """Devuelve el monto en Bs para enviar a Pabilo.

    Prioridad:
    1. Si la orden guardó el monto directamente en Bs (payment_currency == 'bs'
       y payment_amount > 0), se usa ese valor.
    2. Caso contrario se calcula: amount_usd × tasa_usd_bs actual.
    """
    if (order.payment_currency or '').lower() == 'bs':
        amt = float(order.payment_amount or 0)
        if amt > 0:
            return amt

    method_code = (getattr(order, 'payment_method', '') or '').strip().lower()
    method = PaymentMethod.query.filter_by(code=method_code).first() if method_code else None
    if method and not bool(method.uses_rate) and order.amount is not None:
        return round(float(order.amount), 2)

    # Calcular desde el precio base en USD usando la tasa del paquete o la global
    try:
        rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
        usd_rate = float(rate_setting.value) if rate_setting and rate_setting.value else 0.0
    except Exception:
        usd_rate = 0.0

    game = getattr(order, 'game', None)
    if game is not None:
        usd_rate = game.get_bs_rate(usd_rate)

    if usd_rate > 0 and order.amount:
        return round(float(order.amount) * usd_rate, 2)

    return None


def recompute_order_payment_amount(order):
    """Recalcula order.payment_amount para que quede en línea con
    order.amount (p.ej. justo después de que el admin corrige el paquete
    de una orden pendiente).

    order.payment_amount se calcula UNA vez en el checkout (ver
    routes/checkout.py) y de ahí en adelante _get_bs_amount lo usa como
    fuente de verdad con prioridad sobre order.amount — así el monto
    esperado no se mueve si la tasa global cambia después. El problema es
    que si el paquete de la orden se corrige más tarde, ese monto queda
    calculado para el paquete viejo y la verificación de Pabilo sigue
    comparando contra un monto que ya no corresponde. Esta función repite
    el mismo cálculo del checkout con el monto ya actualizado."""
    currency = (order.payment_currency or '').lower()
    if not currency or order.amount is None:
        return

    if currency == 'usd':
        order.payment_amount = round(float(order.amount), 2)
        return

    if currency != 'bs':
        return

    method_code = (getattr(order, 'payment_method', '') or '').strip().lower()
    method = PaymentMethod.query.filter_by(code=method_code).first() if method_code else None

    if method and not bool(method.uses_rate):
        order.payment_amount = normalize_bs_integer_amount(order.amount)
        return

    try:
        rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
        usd_rate = float(rate_setting.value) if rate_setting and rate_setting.value else 0.0
    except Exception:
        usd_rate = 0.0

    game = getattr(order, 'game', None)
    if game is not None:
        usd_rate = game.get_bs_rate(usd_rate)

    if usd_rate > 0:
        order.payment_amount = normalize_bs_integer_amount(float(order.amount) * usd_rate)


def _coerce_decimal_amount(value):
    if value is None:
        return None

    if isinstance(value, Decimal):
        return value

    if isinstance(value, int):
        return Decimal(value)

    if isinstance(value, float):
        return Decimal(str(value))

    raw = str(value or '').strip()
    if not raw:
        return None

    cleaned = raw.upper()
    for token in ('BSD', 'BS.D', 'BS', '$'):
        cleaned = cleaned.replace(token, '')
    cleaned = cleaned.replace(' ', '')

    filtered = ''.join(ch for ch in cleaned if ch.isdigit() or ch in ',.-')
    if not filtered:
        return None

    if ',' in filtered and '.' in filtered:
        filtered = filtered.replace(',', '')
    elif ',' in filtered:
        filtered = filtered.replace(',', '.')

    try:
        return Decimal(filtered)
    except (InvalidOperation, ValueError):
        return None


def normalize_bs_integer_amount(value):
    amount = _coerce_decimal_amount(value)
    if amount is None:
        return None

    normalized = amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return int(normalized)


def _get_expected_order_amount(order):
    amount = _coerce_decimal_amount(_get_bs_amount(order))
    if amount is None:
        return None, 'La orden no tiene un monto exacto válido para validar en Pabilo.'

    if amount <= 0:
        return None, 'La orden no tiene un monto válido mayor a cero para validar en Pabilo.'

    return amount, None


def _normalize_pabilo_amount(order):
    amount, amount_error = _get_expected_order_amount(order)
    if amount_error:
        return None, amount_error

    normalized_amount = normalize_bs_integer_amount(amount)
    if normalized_amount is None or normalized_amount <= 0:
        return None, 'El monto de la orden no puede normalizarse correctamente para Pabilo.'

    return normalized_amount, None


def build_pabilo_reference_payload(reference):
    reference = str(reference or '').strip()
    if not reference:
        return None, 'La orden no tiene una referencia bancaria válida para consultar en Pabilo.'

    payload = {
        'bank_reference': reference,
    }

    return payload, None


def build_pabilo_phone_dni_payload(order):
    payer_phone = ''.join(ch for ch in str(order.payer_phone or '') if ch.isdigit())[:20]
    if len(payer_phone) < 10:
        return None, 'La orden no tiene un telefono del pagador valido para consultar en Pabilo.'

    payer_dni_number = ''.join(ch for ch in str(order.payer_dni_number or '') if ch.isdigit())[:20]
    if not payer_dni_number:
        return None, 'La orden no tiene una cedula del pagador valida para consultar en Pabilo.'

    payer_dni_type = (str(order.payer_dni_type or 'V').strip().upper() or 'V')[:2]
    if payer_dni_type not in {'V', 'E', 'J', 'G', 'P'}:
        payer_dni_type = 'V'

    normalized_amount, amount_error = _normalize_pabilo_amount(order)
    if amount_error:
        return None, amount_error

    payload = {
        'phone': payer_phone,
        'dni': f'{payer_dni_type}{payer_dni_number}',
        'amount': normalized_amount,
    }

    return payload, None


def _iter_amount_candidates(payload):
    if isinstance(payload, dict):
        for key in (
            'amount', 'monto', 'payment_amount', 'paid_amount', 'amount_paid',
            'bank_amount', 'credited_amount', 'transfer_amount', 'amount_bs',
            'bs_amount', 'montobs', 'monto_bs',
        ):
            if key in payload:
                yield payload.get(key)

        for value in payload.values():
            if isinstance(value, dict):
                yield from _iter_amount_candidates(value)
            elif isinstance(value, list):
                for item in value:
                    yield from _iter_amount_candidates(item)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_amount_candidates(item)


def _extract_pabilo_reported_amount(payload_data, full_data):
    # `user_bank_payment` es el mismo objeto del que ya sacamos el `id` y el
    # `status` del pago en verify_order_payment — es el registro autoritativo
    # de ESTE movimiento específico. Se revisa PRIMERO a propósito: antes se
    # buscaba en todo `payload_data` de forma amplia (cualquier campo con
    # nombre de "monto" en cualquier nivel) y esa búsqueda genérica podía
    # toparse con un campo de otro lugar de la respuesta antes de llegar al
    # monto real — se vio en producción con una "verificación repetida" de
    # Pabilo (el mismo pago consultado dos veces) donde así se leyó un
    # número equivocado en vez de los Bs reales del pago.
    payment_data = payload_data.get('user_bank_payment') if isinstance(payload_data, dict) else None
    if payment_data:
        for candidate in _iter_amount_candidates(payment_data):
            amount = _coerce_decimal_amount(candidate)
            if amount is not None and amount > 0:
                return amount

    for candidate in _iter_amount_candidates(payload_data):
        amount = _coerce_decimal_amount(candidate)
        if amount is not None and amount > 0:
            return amount

    for candidate in _iter_amount_candidates(full_data):
        amount = _coerce_decimal_amount(candidate)
        if amount is not None and amount > 0:
            return amount

    return None


def _validate_verified_payment_amount(order, payload_data, full_data):
    expected_amount, amount_error = _get_expected_order_amount(order)
    if amount_error:
        return {
            'ok': False,
            'verified': False,
            'message': amount_error,
        }

    reported_amount = _extract_pabilo_reported_amount(payload_data, full_data)
    if reported_amount is None:
        return {
            'ok': False,
            'verified': False,
            'message': 'Pabilo confirmó la referencia, pero no devolvió un monto válido para compararlo con la orden.',
        }

    minimum_amount = (expected_amount * PABILO_MIN_ACCEPTANCE_RATIO).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if reported_amount < minimum_amount:
        missing_amount = (expected_amount - reported_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return {
            'ok': False,
            'verified': False,
            # El pago SÍ se encontró en Pabilo (no es "aún no aparece"), solo
            # que el monto no alcanza: es un caso distinto de "no encontrado"
            # que dispara el flujo de "completar el pago restante".
            'underpaid': True,
            'expected_amount': str(expected_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
            'reported_amount': str(reported_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
            'missing_amount': str(missing_amount),
            'message': (
                f'Pabilo devolvió un monto menor al permitido para la orden. '
                f'Esperado: Bs {expected_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)}. '
                f'Mínimo aceptado: Bs {minimum_amount}. '
                f'Reportado: Bs {reported_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)}.'
            ),
        }

    return {
        'ok': True,
        'verified': True,
        'expected_amount': str(expected_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
        'minimum_amount': str(minimum_amount),
        'reported_amount': str(reported_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),
    }


def _request_pabilo_verify(url, api_key, payload, timeout):
    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                'Content-Type': 'application/json',
                'appKey': api_key,
            },
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        return None, {'ok': False, 'verified': False, 'message': 'Pabilo no respondió a tiempo.'}
    except requests.exceptions.ConnectionError:
        return None, {'ok': False, 'verified': False, 'message': 'No se pudo conectar con Pabilo.'}
    except Exception as exc:
        return None, {'ok': False, 'verified': False, 'message': f'Error consultando Pabilo: {exc}'}

    try:
        data = response.json()
    except Exception:
        data = {}
    return response, data


def _is_rate_limited_response(status_code, data):
    if status_code == 429:
        return True

    msg = f"{data.get('message') or ''} {data.get('error') or ''}".strip().lower()
    if not msg:
        return False

    if 'too many requests' in msg:
        return True
    if '[429]' in msg:
        return True
    if 'servicio no disponible' in msg and 'intente más tarde' in msg:
        return True
    if 'cannot unmarshal object into go value of type mooc.accountmovements' in msg:
        return True
    return False


def _is_not_found_response(status_code, data):
    if status_code == 404:
        return True

    msg = f"{data.get('message') or ''} {data.get('error') or ''}".strip().lower()
    if not msg:
        return False

    not_found_markers = (
        'payment not found',
        'movement not found',
        'movimiento no encontrado',
        'referencia no encontrada',
        'not found with bank reference',
    )
    return any(marker in msg for marker in not_found_markers)


def _extract_pabilo_payload(data):
    if not isinstance(data, dict):
        return {}, {}

    inner = data.get('data')
    if isinstance(inner, dict):
        return inner, data

    return data, data


def verify_order_payment(order, force_reference=False):
    if not order:
        return {'ok': False, 'verified': False, 'message': 'Orden inválida.'}

    payment_method = PaymentMethod.query.filter_by(code=(order.payment_method or '').strip().lower()).first()
    if not payment_method:
        return {'ok': False, 'verified': False, 'message': 'Método de pago no encontrado.'}

    uses_payer_identity = payment_method_uses_payer_identity_verification(payment_method) and not force_reference

    reference_candidates = []
    identity_payload = None
    if uses_payer_identity:
        identity_payload, payload_error = build_pabilo_phone_dni_payload(order)
        if payload_error:
            return {
                'ok': False,
                'verified': False,
                'requestable': False,
                'message': payload_error,
            }
    else:
        reference_candidates = get_order_reference_candidates(order)
        if not reference_candidates:
            return {
                'ok': False,
                'verified': False,
                'requestable': False,
                'message': 'La orden no tiene una referencia bancaria válida para consultar en Pabilo.',
            }

    api_key = get_pabilo_api_key()
    if not api_key:
        return {'ok': False, 'verified': False, 'message': 'Falta configurar la API key de Pabilo.'}

    user_bank_id = (payment_method.pabilo_user_bank_id or '').strip()
    if not user_bank_id:
        return {'ok': False, 'verified': False, 'message': 'Este método de pago no tiene userBankId de Pabilo.'}

    url = f"{current_app.config.get('PABILO_BASE_URL', 'https://api.pabilo.app').rstrip('/')}/userbankpayment/{user_bank_id}/betaserio"
    timeout = current_app.config.get('PABILO_TIMEOUT', 30)

    accepted_statuses = {
        'verified', 'approve', 'approved', 'aprobado',
        'success', 'successful', 'completed', 'completada',
        'paid', 'pagado',
    }
    last_soft_result = None
    duplicate_hit = None

    verification_candidates = []
    if uses_payer_identity:
        verification_candidates.append({
            'payload': identity_payload,
            'reference': str(order.payment_reference or '').strip(),
            'source': 'payer_identity',
        })
    else:
        for candidate in reference_candidates:
            reference = candidate['reference']
            payload, payload_error = build_pabilo_reference_payload(reference)
            if payload_error:
                continue
            verification_candidates.append({
                'payload': payload,
                'reference': reference,
                'source': candidate['source'],
            })

    for candidate in verification_candidates:
        original_reference = candidate['reference']
        source = candidate['source']

        # El chequeo de duplicados SIEMPRE se hace con la referencia completa
        # (nunca con una variante truncada): dos referencias que solo
        # coinciden en sus últimos 4-5 dígitos no son la misma referencia,
        # y bloquear por eso genera falsos "duplicado" entre órdenes
        # legítimas que nada tienen que ver entre sí.
        if not uses_payer_identity:
            duplicate = find_reference_conflict(
                reference=original_reference,
                payment_method_code=order.payment_method,
                exclude_order_id=order.id,
            )
            if duplicate:
                duplicate_hit = duplicate
                last_soft_result = {
                    'ok': False,
                    'verified': False,
                    'message': (
                        'Se detectó otra orden con la misma referencia bancaria. '
                        'La aprobación automática fue bloqueada.'
                    ),
                    'duplicate_order_id': duplicate.id,
                }
                continue

        # Generar variantes: referencia completa, últimos 6, 5, 4 dígitos.
        # Estas variantes truncadas son solo para intentar encontrar el pago
        # en Pabilo (algunos bancos solo devuelven la referencia truncada),
        # NUNCA se usan para decidir si la orden es un duplicado.
        reference_variants = _generate_reference_variants(original_reference)

        for variant_ref in reference_variants:
            # Construir payload para esta variante
            if uses_payer_identity:
                variant_payload = candidate['payload']
            else:
                variant_payload, payload_error = build_pabilo_reference_payload(variant_ref)
                if payload_error:
                    continue

            response, data = _request_pabilo_verify(url, api_key, variant_payload, timeout)
            if response is None:
                return data

            payload_data, full_data = _extract_pabilo_payload(data)

            if _is_not_found_response(response.status_code, data):
                last_soft_result = {
                    'ok': True,
                    'verified': False,
                    'message': 'El pago todavía no aparece verificado en Pabilo.',
                    'response': full_data,
                }
                continue  # probar siguiente variante
            if response.status_code == 401:
                return {'ok': False, 'verified': False, 'message': 'La API key de Pabilo es inválida o está inactiva.', 'response': full_data}
            if response.status_code == 402:
                return {'ok': False, 'verified': False, 'message': 'La cuenta de Pabilo no tiene créditos suficientes.', 'response': full_data}
            if _is_rate_limited_response(response.status_code, data):
                return {
                    'ok': True,
                    'verified': False,
                    'message': 'Pabilo está recibiendo demasiadas solicitudes (429). Reintentaremos en unos segundos.',
                    'rate_limited': True,
                    'response': full_data,
                }
            if response.status_code >= 400:
                message = full_data.get('message') or full_data.get('error') or f'Pabilo devolvió HTTP {response.status_code}.'
                return {'ok': False, 'verified': False, 'message': message, 'response': full_data}

            payment_data = payload_data.get('user_bank_payment') or {}
            verification_id = str(payment_data.get('id') or '').strip()
            payment_status = str(payment_data.get('status') or '').strip().lower()
            is_new = bool(payload_data.get('is_new'))

            if verification_id:
                existing_by_verification = Order.query.filter(
                    Order.payment_verification_id == verification_id,
                    Order.id != order.id,
                    Order.status.in_(['approved', 'completed'])
                ).first()
                if existing_by_verification:
                    return {
                        'ok': False,
                        'verified': False,
                        'message': 'Ese pago ya fue usado para aprobar otra orden.',
                        'response': full_data,
                    }

            root_status = str(data.get('status') or '').strip().lower()
            is_verified_flag = bool(payload_data.get('verified') or full_data.get('verified'))
            status_is_verified = payment_status in accepted_statuses or root_status in accepted_statuses or is_verified_flag

            if not status_is_verified:
                last_soft_result = {
                    'ok': True,
                    'verified': False,
                    'message': 'La transacción aún no está marcada como verificada en Pabilo.',
                    'response': full_data,
                }
                continue  # probar siguiente variante

            amount_validation = _validate_verified_payment_amount(order, payload_data, full_data)
            if not amount_validation.get('verified'):
                amount_validation['response'] = full_data
                if amount_validation.get('underpaid'):
                    # Pabilo SÍ encontró y confirmó esta referencia (solo que el
                    # monto no alcanza): se informa cuál fue para que el caller
                    # la "reserve" en la orden y nadie pueda reusar ese mismo
                    # pago real en una orden distinta mientras se completa.
                    amount_validation['resolved_reference'] = variant_ref
                    amount_validation['resolved_reference_source'] = source
                return amount_validation

            date_verdict = _check_payment_min_date(payment_data, is_new)
            if date_verdict == 'old':
                return {
                    'ok': False,
                    'verified': False,
                    'message': 'Ese pago es anterior a la fecha mínima aceptada por la tienda.',
                    'response': full_data,
                }
            if date_verdict == 'unknown':
                # Sin fecha no se puede descartar que sea un pago viejo: queda para revisión manual.
                return {
                    'ok': True,
                    'verified': False,
                    'message': 'Pabilo no informó la fecha del pago; queda para revisión manual.',
                    'response': full_data,
                }

            if not verification_id:
                verification_id = f"fallback:{payment_method.id}:{variant_ref or source}"

            source_message = ''
            if source == 'ai':
                source_message = ' Se usó la referencia extraída del comprobante.'
            elif source == 'payer_identity':
                source_message = ' Se validó con el telefono y la cedula del pagador.'

            # Indicar si se usó una variante truncada
            variant_message = ''
            if variant_ref != original_reference:
                variant_message = f' (verificada con últimos {len(variant_ref)} dígitos)'

            return {
                'ok': True,
                'verified': True,
                'message': (
                    'Pago verificado correctamente en Pabilo. '
                    f"Monto reportado: Bs {amount_validation['reported_amount']}.{source_message}{variant_message}"
                ).strip(),
                'verification_id': verification_id,
                'is_new': is_new,
                'expected_amount': amount_validation.get('expected_amount'),
                'minimum_amount': amount_validation.get('minimum_amount'),
                'reported_amount': amount_validation.get('reported_amount'),
                'resolved_reference': variant_ref,
                'resolved_reference_source': source,
                'response': full_data,
            }

    if duplicate_hit and last_soft_result:
        return last_soft_result

    if last_soft_result:
        return last_soft_result

    return {
        'ok': False,
        'verified': False,
        'message': 'No se pudo consultar una referencia bancaria válida para esta orden.',
    }


def verify_remainder_reference(order, reference):
    """Verifica en Pabilo una referencia de un PAGO RESTANTE (el cliente
    completó lo que le faltaba con un segundo pago/referencia distinta).

    A diferencia de verify_order_payment, no compara el monto contra el
    total de la orden: solo confirma que el pago existe en Pabilo, que no es
    un duplicado de otra orden, y devuelve cuánto reportó Pabilo para ÉL. El
    caller (complete-payment) es quien suma esto a lo ya confirmado antes y
    decide si con eso ya se cubre la orden.
    """
    reference = str(reference or '').strip()
    if not order or not reference:
        return {'ok': False, 'verified': False, 'message': 'Falta la referencia del pago restante.'}

    payment_method = PaymentMethod.query.filter_by(code=(order.payment_method or '').strip().lower()).first()
    if not payment_method:
        return {'ok': False, 'verified': False, 'message': 'Método de pago no encontrado.'}

    api_key = get_pabilo_api_key()
    if not api_key:
        return {'ok': False, 'verified': False, 'message': 'Falta configurar la API key de Pabilo.'}

    user_bank_id = (payment_method.pabilo_user_bank_id or '').strip()
    if not user_bank_id:
        return {'ok': False, 'verified': False, 'message': 'Este método de pago no tiene userBankId de Pabilo.'}

    duplicate = find_reference_conflict(
        reference=reference,
        payment_method_code=order.payment_method,
        exclude_order_id=order.id,
    )
    if duplicate:
        return {
            'ok': False,
            'verified': False,
            'message': 'Se detectó otra orden con la misma referencia bancaria. La verificación fue bloqueada.',
            'duplicate_order_id': duplicate.id,
        }

    url = f"{current_app.config.get('PABILO_BASE_URL', 'https://api.pabilo.app').rstrip('/')}/userbankpayment/{user_bank_id}/betaserio"
    timeout = current_app.config.get('PABILO_TIMEOUT', 30)

    accepted_statuses = {
        'verified', 'approve', 'approved', 'aprobado',
        'success', 'successful', 'completed', 'completada',
        'paid', 'pagado',
    }

    last_soft_result = {
        'ok': True,
        'verified': False,
        'message': 'El pago restante todavía no aparece verificado en Pabilo.',
    }

    for variant_ref in _generate_reference_variants(reference):
        payload, payload_error = build_pabilo_reference_payload(variant_ref)
        if payload_error:
            continue

        response, data = _request_pabilo_verify(url, api_key, payload, timeout)
        if response is None:
            return data

        payload_data, full_data = _extract_pabilo_payload(data)

        if _is_not_found_response(response.status_code, data):
            continue
        if response.status_code == 401:
            return {'ok': False, 'verified': False, 'message': 'La API key de Pabilo es inválida o está inactiva.', 'response': full_data}
        if response.status_code == 402:
            return {'ok': False, 'verified': False, 'message': 'La cuenta de Pabilo no tiene créditos suficientes.', 'response': full_data}
        if _is_rate_limited_response(response.status_code, data):
            return {
                'ok': True,
                'verified': False,
                'message': 'Pabilo está recibiendo demasiadas solicitudes (429). Intenta de nuevo en unos segundos.',
                'rate_limited': True,
                'response': full_data,
            }
        if response.status_code >= 400:
            message = full_data.get('message') or full_data.get('error') or f'Pabilo devolvió HTTP {response.status_code}.'
            return {'ok': False, 'verified': False, 'message': message, 'response': full_data}

        payment_data = payload_data.get('user_bank_payment') or {}
        verification_id = str(payment_data.get('id') or '').strip()
        payment_status = str(payment_data.get('status') or '').strip().lower()
        is_new = bool(payload_data.get('is_new'))

        if verification_id:
            existing_by_verification = Order.query.filter(
                Order.payment_verification_id == verification_id,
                Order.id != order.id,
                Order.status.in_(['approved', 'completed']),
            ).first()
            if existing_by_verification:
                return {
                    'ok': False,
                    'verified': False,
                    'message': 'Ese pago ya fue usado para aprobar otra orden.',
                    'response': full_data,
                }

        root_status = str(data.get('status') or '').strip().lower()
        is_verified_flag = bool(payload_data.get('verified') or full_data.get('verified'))
        status_is_verified = payment_status in accepted_statuses or root_status in accepted_statuses or is_verified_flag

        if not status_is_verified:
            last_soft_result = {
                'ok': True,
                'verified': False,
                'message': 'El pago restante todavía no está marcado como verificado en Pabilo.',
                'response': full_data,
            }
            continue

        reported_amount = _extract_pabilo_reported_amount(payload_data, full_data)
        if reported_amount is None or reported_amount <= 0:
            last_soft_result = {
                'ok': False,
                'verified': False,
                'message': 'Pabilo confirmó el pago restante, pero no devolvió un monto válido.',
                'response': full_data,
            }
            continue

        date_verdict = _check_payment_min_date(payment_data, is_new)
        if date_verdict == 'old':
            return {
                'ok': False,
                'verified': False,
                'message': 'Ese pago es anterior a la fecha mínima aceptada por la tienda.',
                'response': full_data,
            }
        if date_verdict == 'unknown':
            return {
                'ok': True,
                'verified': False,
                'message': 'Pabilo no informó la fecha del pago restante; queda para revisión manual.',
                'response': full_data,
            }

        if not verification_id:
            verification_id = f"fallback:{payment_method.id}:{variant_ref}"

        quantized_amount = reported_amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return {
            'ok': True,
            'verified': True,
            'verification_id': verification_id,
            'reported_amount': str(quantized_amount),
            'resolved_reference': variant_ref,
            'message': f'Pago restante verificado. Monto reportado: Bs {quantized_amount}.',
            'response': full_data,
        }

    return last_soft_result


def _parse_pabilo_date(value):
    """Pabilo manda '0001-01-01T00:00:00Z' cuando no conoce una fecha."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.year < 2000:
        return None
    return parsed


def _check_payment_min_date(payment_data, is_new):
    """Piso de fecha (PAYMENT_MIN_DATE, día de Venezuela) para los pagos de Pabilo.

    La tienda reabrió con la base vacía y se perdió el registro de referencias ya usadas:
    sin este piso un cliente podría reclamar otra vez un pago móvil viejo. Pabilo informa
    `movement_date` (día del movimiento en el banco) y `created_at` (cuándo registró el pago).
    Devuelve 'ok', 'old' o 'unknown'.
    """
    day = (current_app.config.get('PAYMENT_MIN_DATE') or '').strip()
    if not day:
        return 'ok'
    try:
        # Medianoche de Venezuela (UTC-4) del día configurado.
        floor = datetime.fromisoformat(f'{day}T04:00:00+00:00')
    except ValueError:
        return 'ok'

    movement = _parse_pabilo_date((payment_data or {}).get('movement_date'))
    created = _parse_pabilo_date((payment_data or {}).get('created_at'))

    if movement is not None and movement < floor:
        return 'old'
    # Ya estaba registrado en Pabilo antes del piso: lo cobró la tienda anterior.
    if not is_new and created is not None and created < floor:
        return 'old'
    if movement is None and created is None:
        return 'unknown'
    return 'ok'


def clear_pabilo_verification_state(order):
    order.payment_verified_at = None
    order.payment_verification_id = None
    order.payment_verification_attempts = 0
    order.payment_last_verification_at = None


def stamp_verified_payment(order, verification_result):
    resolved_reference = str(verification_result.get('resolved_reference') or '').strip()
    if resolved_reference:
        order.payment_reference = resolved_reference
        order.payment_reference_last5 = normalize_reference_last5(resolved_reference)
    order.payment_verified_at = datetime.utcnow()
    order.payment_verification_id = verification_result.get('verification_id') or order.payment_verification_id