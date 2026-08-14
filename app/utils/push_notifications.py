"""Notificaciones push web (VAPID).

Guarda el par de llaves VAPID en la tabla Setting (se generan solas la
primera vez que se necesitan, no requiere tocar variables de entorno en
el VPS). Cada suscripción es un navegador/dispositivo (`endpoint` único);
si además queda ligada a una orden, recibe el aviso de "tu recarga está
lista" para esa orden puntual, sin dejar de recibir los avisos generales.
"""

import base64
import logging
import threading
from datetime import datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import current_app

from ..models import PushSubscription, Setting, db

logger = logging.getLogger(__name__)

VAPID_PRIVATE_KEY_SETTING = 'vapid_private_key_pem'
VAPID_PUBLIC_KEY_SETTING = 'vapid_public_key_b64'
VAPID_CLAIMS_SUB_SETTING = 'vapid_claims_sub'
DEFAULT_VAPID_SUB = 'mailto:soporte@3srecargas.com'


def _b64url(raw_bytes):
    return base64.urlsafe_b64encode(raw_bytes).rstrip(b'=').decode('ascii')


def _get_setting_value(key):
    setting = Setting.query.filter_by(key=key).first()
    return setting.value if setting and setting.value else ''


def _set_setting_value(key, value, description=''):
    setting = Setting.query.filter_by(key=key).first()
    if not setting:
        setting = Setting(key=key, value=value, description=description)
        db.session.add(setting)
    else:
        setting.value = value


def _generate_vapid_keys():
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode('ascii')

    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_b64 = _b64url(public_raw)
    return private_pem, public_b64


def get_or_create_vapid_keys():
    """Devuelve (private_key_pem, public_key_b64url), generándolas y
    guardándolas en Settings la primera vez que se llama."""
    private_pem = _get_setting_value(VAPID_PRIVATE_KEY_SETTING)
    public_b64 = _get_setting_value(VAPID_PUBLIC_KEY_SETTING)
    if private_pem and public_b64:
        return private_pem, public_b64

    private_pem, public_b64 = _generate_vapid_keys()
    _set_setting_value(VAPID_PRIVATE_KEY_SETTING, private_pem, 'Llave privada VAPID para notificaciones push (no compartir).')
    _set_setting_value(VAPID_PUBLIC_KEY_SETTING, public_b64, 'Llave pública VAPID que usa el navegador del cliente.')
    db.session.commit()
    return private_pem, public_b64


def get_vapid_public_key():
    _, public_b64 = get_or_create_vapid_keys()
    return public_b64


def _vapid_claims():
    sub = _get_setting_value(VAPID_CLAIMS_SUB_SETTING) or DEFAULT_VAPID_SUB
    return {'sub': sub}


def is_push_configured():
    try:
        return bool(get_or_create_vapid_keys()[0])
    except Exception:
        return False


def subscribe(endpoint, p256dh_key, auth_key, order_id=None):
    endpoint = str(endpoint or '').strip()
    p256dh_key = str(p256dh_key or '').strip()
    auth_key = str(auth_key or '').strip()
    if not endpoint or not p256dh_key or not auth_key:
        raise ValueError('Datos de suscripción incompletos.')

    record = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if not record:
        record = PushSubscription(endpoint=endpoint)
        db.session.add(record)

    record.p256dh_key = p256dh_key
    record.auth_key = auth_key
    if order_id:
        record.order_id = int(order_id)
    db.session.commit()
    return record


def unsubscribe(endpoint):
    endpoint = str(endpoint or '').strip()
    if not endpoint:
        return
    PushSubscription.query.filter_by(endpoint=endpoint).delete()
    db.session.commit()


def _send_to_record(record, payload_json, app):
    from pywebpush import WebPushException, webpush

    private_pem, _ = get_or_create_vapid_keys()
    subscription_info = {
        'endpoint': record.endpoint,
        'keys': {'p256dh': record.p256dh_key, 'auth': record.auth_key},
    }
    try:
        webpush(
            subscription_info=subscription_info,
            data=payload_json,
            vapid_private_key=private_pem,
            vapid_claims=dict(_vapid_claims()),
            timeout=10,
        )
        return True
    except WebPushException as exc:
        status_code = getattr(exc.response, 'status_code', None)
        if status_code in (404, 410):
            # El navegador canceló la suscripción (desinstaló, borró
            # datos, etc.): ya no sirve, se limpia para no reintentar.
            try:
                db.session.delete(record)
                db.session.commit()
            except Exception:
                db.session.rollback()
        else:
            logger.warning('Push falló (%s) para endpoint %s: %s', status_code, record.endpoint[:60], exc)
        return False
    except Exception as exc:
        logger.warning('Push falló para endpoint %s: %s', record.endpoint[:60], exc)
        return False


def _build_payload(title, body, url=None, tag=None):
    import json
    payload = {'title': title, 'body': body}
    if url:
        payload['url'] = url
    if tag:
        payload['tag'] = tag
    return json.dumps(payload, ensure_ascii=False)


def send_push_to_subscriptions(subscriptions, title, body, url=None, tag=None):
    if not subscriptions:
        return {'sent': 0, 'failed': 0}
    if not is_push_configured():
        return {'sent': 0, 'failed': 0}

    app = current_app._get_current_object()
    payload_json = _build_payload(title, body, url=url, tag=tag)
    sent = 0
    failed = 0
    for record in subscriptions:
        if _send_to_record(record, payload_json, app):
            record.last_sent_at = datetime.utcnow()
            sent += 1
        else:
            failed += 1
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return {'sent': sent, 'failed': failed}


def send_push_broadcast(title, body, url=None):
    subscriptions = PushSubscription.query.all()
    return send_push_to_subscriptions(subscriptions, title, body, url=url, tag='promo')


def send_push_to_order_subscribers(order, title, body, url=None):
    if not order:
        return {'sent': 0, 'failed': 0}
    subscriptions = PushSubscription.query.filter_by(order_id=order.id).all()
    return send_push_to_subscriptions(subscriptions, title, body, url=url, tag=f'order-{order.id}')


def send_push_to_order_subscribers_async(app, order_id, title, body, url=None):
    """Igual que send_push_to_order_subscribers pero en un hilo de fondo,
    para no sumarle latencia a la petición que aprueba/completa la orden."""
    def _send():
        with app.app_context():
            from ..models import Order
            order = Order.query.get(order_id)
            if order:
                send_push_to_order_subscribers(order, title, body, url=url)

    threading.Thread(target=_send, daemon=True).start()
