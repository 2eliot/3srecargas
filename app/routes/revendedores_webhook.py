"""Receptor del webhook de la API whitelabel de Revendedores.

Revendedores dispara un POST aquí cuando una orden suya cambia de estado
(en particular cuando el reconciliador completa una recarga de juego
dinámico que quedó 'procesando'). Sin esto, la orden local se quedaba
mostrando "procesando" en el admin hasta que alguien reintentara a mano,
aunque Revendedores ya la hubiera completado.

El payload NUNCA se usa para decidir el resultado — cualquiera podría
falsificarlo, y el secreto HMAC de la cuenta no está disponible de este
lado. Solo se usa `external_order_id` como pista de qué orden re-verificar:
el estado real sale de process_revendedores_queue, que confirma contra
/api/v1/order-status con nuestra propia API key (el mismo camino que usan
el scheduler de recuperación y el botón "Verificar" del admin, así que es
idempotente y no puede duplicar recargas).
"""
import json
import re

from flask import Blueprint, current_app, jsonify, request

from ..models import Order
from ..utils.order_processing import process_revendedores_queue

revendedores_webhook_bp = Blueprint('revendedores_webhook_bp', __name__)

# external_order_id nuestro: "{order_number}-s{paso}-{intento}"
# (ver process_revendedores_queue). order_number es alfanumérico sin guiones.
_EXT_ID_RE = re.compile(r'^([0-9A-Za-z]{4,16})-s\d+-\d+$')


@revendedores_webhook_bp.route('/api/revendedores/webhook', methods=['POST'])
def revendedores_webhook():
    payload = request.get_json(silent=True) or {}
    ext_id = str((payload.get('order') or {}).get('external_order_id') or '').strip()

    if not payload.get('event') or not ext_id:
        return jsonify({'ok': False, 'error': 'Payload inválido: falta event o external_order_id'}), 400

    match = _EXT_ID_RE.match(ext_id)
    if not match:
        # 200 igual para que Revendedores no reintente un id que no es nuestro.
        return jsonify({'ok': True, 'warning': 'external_order_id no corresponde a esta tienda'})

    order = Order.query.filter_by(order_number=match.group(1)).first()
    if not order:
        current_app.logger.warning('[RevWebhook] Orden no encontrada para %s', ext_id)
        return jsonify({'ok': True, 'warning': 'order not found'})

    if order.status != 'pending':
        return jsonify({'ok': True, 'result': 'already_processed', 'order_status': order.status})

    try:
        auto_resp = json.loads(order.automation_response or '{}')
    except Exception:
        auto_resp = {}
    if not auto_resp.get('pending_verification'):
        return jsonify({'ok': True, 'result': 'no_verification_needed'})

    try:
        result = process_revendedores_queue(order, base_state=auto_resp, force=False)
    except Exception:
        current_app.logger.exception('[RevWebhook] Error verificando la orden %s', order.order_number)
        # No se toca la orden: el scheduler de recuperación la retomará.
        return jsonify({'ok': True, 'warning': 'verification failed, ignored'})

    current_app.logger.info(
        '[RevWebhook] Orden %s verificada por webhook: %s',
        order.order_number, (result or {}).get('message') or order.status,
    )
    return jsonify({'ok': True, 'order_status': order.status})
