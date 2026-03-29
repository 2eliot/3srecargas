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
    return raw[-5:] if raw else ''


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


def build_pabilo_payload(order, include_amount=True):
    payload = {'bank_reference': str(order.payment_reference or '').strip()}

    if include_amount and order.payment_amount is not None:
        payload['amount'] = float(order.payment_amount or 0)

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

    # Para BDV Personas/Provincial, Pabilo permite verificar solo con referencia.
    # Si el monto es rechazado, reintenta automáticamente sin amount.
    if response.status_code == 400:
        msg_400 = (data.get('message') or data.get('error') or '').strip().lower()
        if 'amount' in msg_400 and ('invalid' in msg_400 or 'not valid' in msg_400):
            payload_without_amount = build_pabilo_payload(order, include_amount=False)
            response_retry, data_retry = _request_pabilo_verify(url, api_key, payload_without_amount, timeout)
            if response_retry is None:
                return data_retry
            response = response_retry
            data = data_retry

    if response.status_code == 404:
        return {'ok': True, 'verified': False, 'message': 'El pago todavía no aparece verificado en Pabilo.', 'response': data}
    if response.status_code == 401:
        return {'ok': False, 'verified': False, 'message': 'La API key de Pabilo es inválida o está inactiva.', 'response': data}
    if response.status_code == 402:
        return {'ok': False, 'verified': False, 'message': 'La cuenta de Pabilo no tiene créditos suficientes.', 'response': data}
    if _is_rate_limited_response(response.status_code, data):
        return {
            'ok': True,
            'verified': False,
            'message': 'Pabilo está recibiendo demasiadas solicitudes (429). Reintentaremos en unos segundos.',
            'rate_limited': True,
            'response': data,
        }
    if response.status_code >= 400:
        message = data.get('message') or data.get('error') or f'Pabilo devolvió HTTP {response.status_code}.'
        return {'ok': False, 'verified': False, 'message': message, 'response': data}

    payment_data = data.get('user_bank_payment') or {}
    verification_id = str(payment_data.get('id') or '').strip()
    payment_status = str(payment_data.get('status') or '').strip().lower()
    is_new = bool(data.get('is_new'))

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
                'response': data,
            }

    accepted_statuses = {
        'verified', 'approve', 'approved', 'aprobado',
        'success', 'successful', 'completed', 'completada',
    }
    root_status = str(data.get('status') or '').strip().lower()
    is_verified_flag = bool(data.get('verified'))
    status_is_verified = payment_status in accepted_statuses or root_status in accepted_statuses or is_verified_flag

    if not status_is_verified:
        return {
            'ok': True,
            'verified': False,
            'message': 'La transacción aún no está marcada como verificada en Pabilo.',
            'response': data,
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
        'response': data,
    }


def stamp_verified_payment(order, verification_result):
    order.payment_verified_at = datetime.utcnow()
    order.payment_verification_id = verification_result.get('verification_id') or order.payment_verification_id