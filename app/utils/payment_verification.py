from datetime import datetime

import requests
from flask import current_app

from ..models import Order, PaymentMethod, Setting


AUTO_VERIFY_SETTING_KEY = 'auto_verify_payments'
PABILO_API_KEY_SETTING_KEY = 'pabilo_api_key'


def get_setting_value(key, default=''):
    setting = Setting.query.filter_by(key=key).first()
    if not setting:
        return default
    return setting.value or default


def is_auto_verify_enabled():
    return get_setting_value(AUTO_VERIFY_SETTING_KEY, 'false').strip().lower() == 'true'


def get_pabilo_api_key():
    return get_setting_value(PABILO_API_KEY_SETTING_KEY, '').strip()


def normalize_reference_last5(reference):
    raw = ''.join(ch for ch in str(reference or '') if ch.isdigit())
    if not raw:
        raw = str(reference or '').strip()
    return raw[-6:] if raw else ''


def has_possible_duplicate_reference(reference_last5, amount, payment_method_code, exclude_order_id=None):
    if not reference_last5:
        return None

    query = Order.query.filter(
        Order.payment_reference_last5 == reference_last5,
        Order.payment_method == payment_method_code,
        Order.status.in_(['pending', 'approved', 'completed']),
    )

    if amount is not None:
        query = query.filter(Order.payment_amount == amount)

    if exclude_order_id:
        query = query.filter(Order.id != exclude_order_id)

    return query.order_by(Order.id.desc()).first()


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

    # Calcular desde el precio base en USD usando la tasa configurada
    try:
        rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
        usd_rate = float(rate_setting.value) if rate_setting and rate_setting.value else 0.0
    except Exception:
        usd_rate = 0.0

    if usd_rate > 0 and order.amount:
        return round(float(order.amount) * usd_rate, 2)

    return None


def build_pabilo_payload(order, include_amount=True):
    payload = {'bank_reference': str(order.payment_reference or '').strip()}

    if include_amount:
        bs_amount = _get_bs_amount(order)
        if bs_amount is not None and bs_amount > 0:
            payload['amount'] = bs_amount

    if order.payer_dni_number:
        payload['dni_pagador'] = {
            'dniType': (order.payer_dni_type or 'V').strip().upper(),
            'dniNumber': str(order.payer_dni_number).strip(),
        }
    if order.payer_phone:
        payload['phone_pagador'] = str(order.payer_phone).strip()
    if order.payer_bank_origin:
        payload['bank_origin'] = str(order.payer_bank_origin).strip()
    if order.payer_payment_date:
        payload['fecha_pago'] = order.payer_payment_date.strftime('%Y-%m-%d')
    if order.payer_movement_type:
        payload['movement_type'] = str(order.payer_movement_type).strip().upper()

    return payload


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


def _extract_pabilo_payload(data):
    if not isinstance(data, dict):
        return {}, {}

    inner = data.get('data')
    if isinstance(inner, dict):
        return inner, data

    return data, data


def verify_order_payment(order):
    if not order:
        return {'ok': False, 'verified': False, 'message': 'Orden inválida.'}

    payment_method = PaymentMethod.query.filter_by(code=(order.payment_method or '').strip().lower()).first()
    if not payment_method:
        return {'ok': False, 'verified': False, 'message': 'Método de pago no encontrado.'}

    api_key = get_pabilo_api_key()
    if not api_key:
        return {'ok': False, 'verified': False, 'message': 'Falta configurar la API key de Pabilo.'}

    user_bank_id = (payment_method.pabilo_user_bank_id or '').strip()
    if not user_bank_id:
        return {'ok': False, 'verified': False, 'message': 'Este método de pago no tiene userBankId de Pabilo.'}

    duplicate = has_possible_duplicate_reference(
        reference_last5=order.payment_reference_last5,
        amount=order.payment_amount,
        payment_method_code=order.payment_method,
        exclude_order_id=order.id,
    )
    if duplicate:
        return {
            'ok': False,
            'verified': False,
            'message': (
                'Se detectó otra orden con los mismos últimos 5 dígitos de referencia '
                'y monto. La aprobación automática fue bloqueada.'
            ),
            'duplicate_order_id': duplicate.id,
        }

    url = f"{current_app.config.get('PABILO_BASE_URL', 'https://api.pabilo.app').rstrip('/')}/userbankpayment/{user_bank_id}/betaserio"
    payload = build_pabilo_payload(order, include_amount=True)
    timeout = current_app.config.get('PABILO_TIMEOUT', 30)

    response, data = _request_pabilo_verify(url, api_key, payload, timeout)
    if response is None:
        return data

    payload_data, full_data = _extract_pabilo_payload(data)

    if response.status_code == 404:
        return {'ok': True, 'verified': False, 'message': 'El pago todavía no aparece verificado en Pabilo.', 'response': full_data}
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

    accepted_statuses = {
        'verified', 'approve', 'approved', 'aprobado',
        'success', 'successful', 'completed', 'completada',
        'paid', 'pagado',
    }
    root_status = str(data.get('status') or '').strip().lower()
    is_verified_flag = bool(payload_data.get('verified') or full_data.get('verified'))
    status_is_verified = payment_status in accepted_statuses or root_status in accepted_statuses or is_verified_flag

    if not status_is_verified:
        return {
            'ok': True,
            'verified': False,
            'message': 'La transacción aún no está marcada como verificada en Pabilo.',
            'response': full_data,
        }

    if not verification_id:
        # Fallback cuando Pabilo no devuelve ID único de verificación.
        # El sistema sigue protegido por referencia única en órdenes.
        verification_id = f"fallback:{payment_method.id}:{str(order.payment_reference or '').strip()}"

    return {
        'ok': True,
        'verified': True,
        'message': 'Pago verificado correctamente en Pabilo.',
        'verification_id': verification_id,
        'is_new': is_new,
        'response': full_data,
    }


def stamp_verified_payment(order, verification_result):
    order.payment_verified_at = datetime.utcnow()
    order.payment_verification_id = verification_result.get('verification_id') or order.payment_verification_id