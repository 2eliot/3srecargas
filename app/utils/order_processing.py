import json
from datetime import datetime
from uuid import uuid4

import requests
from flask import current_app

from ..models import Affiliate, AffiliateCommission, Order, Pin, RevendedoresItemMapping, db
from .locks import acquire_lock, release_lock
from .minigames import ensure_minigame_opportunity
from .notifications import notify_order_approved, notify_order_completed
from .points import award_points_for_order

# TTL del lock de aprobación por orden: cubre el peor caso realista (un
# intento de VPS/Revendedores de 120s más margen) para que, si el worker
# muere a mitad de camino, el lock se libere solo y la orden no quede
# bloqueada para siempre en lugar de solo "pendiente".
ORDER_APPROVE_LOCK_TTL_SECONDS = 300


def process_affiliate_commission(order):
    if not order.affiliate_id:
        return
    affiliate = Affiliate.query.get(order.affiliate_id)
    if not affiliate or not affiliate.is_active:
        return
    commission_amount = round(float(order.amount) * float(affiliate.commission_rate) / 100, 2)
    commission = AffiliateCommission(
        affiliate_id=affiliate.id,
        order_id=order.id,
        amount=commission_amount,
    )
    affiliate.balance = float(affiliate.balance) + commission_amount
    affiliate.total_earned = float(affiliate.total_earned) + commission_amount
    db.session.add(commission)


def get_order_auto_mapping(order_obj):
    mappings = get_order_auto_mappings(order_obj)
    return mappings[0] if mappings else None


def order_qualifies_for_auto_fulfillment(order):
    """True si la orden puede completarse SOLA (stock de PINs propio o
    mapeo de Revendedores) sin que un admin tenga que hacer la recarga a
    mano (p.ej. Zinli, o un juego al que le quitaron el mapeo).

    La usan tanto el auto-verify de Pabilo como el de Binance Pay para
    decidir si, al confirmarse el pago, la orden puede aprobarse y
    entregarse sola o si debe quedar 'pending' para que el admin la
    procese manualmente.
    """
    if not order:
        return False
    has_mapping = bool(get_order_auto_mapping(order))
    category_slug = (order.game.category.slug if order.game and order.game.category else '').lower()
    uses_pin_stock = bool(order.package and order.package.is_automated) or category_slug == 'tarjetas'
    return has_mapping or uses_pin_stock


def get_order_auto_mappings(order_obj):
    try:
        if not order_obj or not order_obj.package_id:
            return []
        mapping = RevendedoresItemMapping.query.filter_by(
            store_package_id=int(order_obj.package_id),
            active=True,
            auto_enabled=True,
        ).first()
        if not mapping:
            return []

        items = []
        for attr_name in ('catalog_item', 'catalog_item_2'):
            item = getattr(mapping, attr_name, None)
            if not item:
                continue
            items.append(item)

        return items
    except Exception:
        return []


def _load_revendedores_auto_response(order, catalog_items, base_state=None):
    auto_resp = {}
    if isinstance(base_state, dict):
        auto_resp = dict(base_state)
    else:
        try:
            auto_resp = json.loads(order.automation_response or '{}')
        except Exception:
            auto_resp = {}

    existing_steps = auto_resp.get('steps') if isinstance(auto_resp.get('steps'), list) else []
    steps = []

    for idx, item in enumerate(catalog_items):
        current = existing_steps[idx] if idx < len(existing_steps) and isinstance(existing_steps[idx], dict) else {}
        steps.append({
            'slot': idx + 1,
            'catalog_id': item.id,
            'remote_product_id': item.remote_product_id,
            'remote_product_name': item.remote_product_name or '',
            'remote_package_id': item.remote_package_id,
            'remote_package_name': item.remote_package_name or '',
            'success': bool(current.get('success')),
            'pending_verification': bool(current.get('pending_verification')),
            'rev_attempt': int(current.get('rev_attempt') or 0),
            'external_order_id': current.get('external_order_id') or '',
            'player_name': current.get('player_name') or '',
            'reference_no': current.get('reference_no') or '',
            'order_id': current.get('order_id'),
            'error': current.get('error') or '',
            'verified': bool(current.get('verified')),
        })

    if not existing_steps and auto_resp.get('source') == 'revendedores_api' and steps:
        steps[0].update({
            'success': bool(auto_resp.get('success')),
            'pending_verification': bool(auto_resp.get('pending_verification')),
            'rev_attempt': int(auto_resp.get('rev_attempt') or 0),
            'external_order_id': auto_resp.get('external_order_id') or '',
            'player_name': auto_resp.get('player_name') or '',
            'reference_no': auto_resp.get('reference_no') or '',
            'order_id': auto_resp.get('order_id'),
            'error': auto_resp.get('error') or '',
            'verified': bool(auto_resp.get('verified')),
        })

    auto_resp['source'] = 'revendedores_api'
    auto_resp['steps'] = steps
    auto_resp['pending_verification'] = any(step.get('pending_verification') for step in steps)
    auto_resp['current_step_index'] = next((idx for idx, step in enumerate(steps) if not step.get('success')), None)
    auto_resp['step_count'] = len(steps)
    return auto_resp


MAX_REV_RETRIES = 3
REV_STATUS_CHECK_TIMEOUT = 15

# Errores de Revendedores que no tiene sentido reintentar: el problema no es
# la red ni un pico de carga, es un dato que no va a cambiar solo. Antes todos
# estos quemaban los 3 intentos automáticos (con su espera) antes de rendirse,
# y al admin le llegaba un mensaje genérico de "reintentos agotados" en vez
# del motivo real. Casos vistos en producción: mapeos que apuntan a paquetes
# que Revendedores ya dio de baja, IDs de jugador inválidos y saldo agotado.
_REV_NON_RETRYABLE_PATTERNS = (
    ('no encontrado', 'mapping'),
    ('inactivo', 'mapping'),
    ('not found', 'mapping'),
    ('id de jugador inválido', 'player'),
    ('id de jugador invalido', 'player'),
    ('invalid player', 'player'),
    ('usuario no existe', 'player'),
    ('user id não existe', 'player'),
    ('user id nao existe', 'player'),
    ('insufficient credits', 'balance'),
    ('saldo insuficiente', 'balance'),
    ('insufficient balance', 'balance'),
)

_REV_NON_RETRYABLE_HINTS = {
    'mapping': (
        'El paquete ya no existe en Revendedores. Vuelve a sincronizar el catálogo '
        '(Admin → Revendedores → Sincronizar) y revisa el mapeo de este paquete.'
    ),
    'player': 'Verifica el ID del jugador con el cliente antes de reintentar.',
    'balance': 'Recarga saldo en Revendedores y luego reintenta la orden.',
}


def classify_revendedores_error(message):
    """Devuelve la causa si el error es definitivo, o None si vale reintentar."""
    text = str(message or '').strip().lower()
    if not text:
        return None
    for needle, cause in _REV_NON_RETRYABLE_PATTERNS:
        if needle in text:
            return cause
    return None


def _check_revendedores_attempt_status(base_url, api_key, external_order_id):
    """Consulta en Revendedores el estado real de un intento previo.

    Se usa antes de disparar un nuevo intento de recarga: si el intento
    anterior en realidad sí se procesó del lado de Revendedores (pero
    nuestra respuesta se perdió por timeout, caída del worker, etc.),
    evita ejecutar una segunda recarga duplicada para el mismo paso.
    Devuelve None si no se pudo confirmar nada (Revendedores no respondió,
    o no encuentra ese external_order_id).
    """
    if not external_order_id:
        return None
    try:
        resp = requests.get(
            f'{base_url}/api/v1/order-status',
            params={'external_order_id': external_order_id},
            headers={'X-API-Key': api_key},
            timeout=REV_STATUS_CHECK_TIMEOUT,
        )
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get('ok') or not data.get('found'):
        return None
    return data


def process_revendedores_queue(order, base_state=None, force=False):
    """Avanza la cola de recargas de Revendedores para `order`.

    Hace como máximo UNA llamada de red por invocación (una consulta de
    estado, o un intento de recarga) y regresa de inmediato — nunca
    reintenta en un loop bloqueante dentro del mismo request. Los
    reintentos posteriores los dispara quien vuelva a llamar a esta
    función: el scheduler de recuperación en segundo plano, el polling de
    /admin/orders, o una aprobación manual. Esto evita que un solo request
    HTTP quede bloqueado varios minutos esperando 2-3 intentos seguidos
    (lo cual, con el timeout por defecto de Gunicorn de 30s, terminaba
    matando el worker a mitad de la operación).

    `force=True` (usado por approve_order, es decir una acción explícita:
    aprobación manual o el primer intento automático tras verificar el
    pago) ignora el límite de reintentos y siempre intenta una vez más.
    `force=False` (usado por el scheduler y el polling pasivo) respeta el
    límite para no bombardear Revendedores indefinidamente si un paso
    quedó roto de forma permanente.
    """
    catalog_items = get_order_auto_mappings(order)
    if not catalog_items:
        return None

    base_url, api_key, _, recharge_path = get_revendedores_env()
    auto_resp = _load_revendedores_auto_response(order, catalog_items, base_state=base_state)
    steps = auto_resp.get('steps') or []

    if not base_url or not api_key:
        auto_resp['pending_verification'] = False
        auto_resp['last_error'] = 'Revendedores API no configurada.'
        order.automation_response = json.dumps(auto_resp)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {
            'ok': False,
            'changed': False,
            'pending_verification': False,
            'message': 'Revendedores API no configurada.',
            'category': 'warning',
        }

    for step_index, step in enumerate(steps):
        if step.get('success'):
            continue

        # Si el paso ya tenía un intento previo (procesando, error de red,
        # error de la API), confirmar con Revendedores antes de generar uno
        # nuevo — así no duplicamos la recarga si aquel intento sí se
        # procesó pero no llegamos a registrar el éxito de nuestro lado.
        previous_ext_id = step.get('external_order_id') or ''
        if previous_ext_id:
            confirmed = _check_revendedores_attempt_status(base_url, api_key, previous_ext_id)
            if confirmed:
                confirmed_status = (confirmed.get('status') or '').strip().lower()
                confirmed_order = confirmed.get('order') or {}

                if confirmed_status == 'completada':
                    step.update({
                        'success': True,
                        'pending_verification': False,
                        'player_name': confirmed_order.get('player_name', step.get('player_name', '')),
                        'reference_no': confirmed_order.get('reference_no', step.get('reference_no', '')),
                        'verified': True,
                        'error': '',
                    })
                    order.notes = ((order.notes or '') + f'\n[Revendedores API][Paso {step_index + 1}] Confirmado como completado al reintentar (intento previo {previous_ext_id}).').strip()
                    continue  # este paso ya quedó resuelto, seguir con el siguiente

                if confirmed_status == 'procesando':
                    auto_resp['pending_verification'] = True
                    auto_resp['current_step_index'] = step_index
                    auto_resp['external_order_id'] = previous_ext_id
                    order.automation_response = json.dumps(auto_resp)
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    return {
                        'ok': False,
                        'changed': False,
                        'pending_verification': True,
                        'current_step_index': step_index,
                        'message': f'La recarga del paso {step_index + 1} sigue en estado "procesando" en Revendedores.',
                        'category': 'warning',
                    }

                # 'fallida' u otro estado → confirmado que el intento
                # anterior no se completó (y Revendedores ya reembolsó su
                # saldo), así que es seguro generar un intento nuevo más
                # abajo sin riesgo de duplicar la recarga. Si ese intento
                # nuevo vuelve a salir 'fallida', el manejo de la respuesta
                # del POST (más abajo) es el que decide detenerse en vez de
                # seguir reintentando solo.

        prior_attempts = int(step.get('rev_attempt') or 0)
        if not force and prior_attempts >= MAX_REV_RETRIES:
            auto_resp['pending_verification'] = False
            auto_resp['current_step_index'] = step_index
            auto_resp['last_error'] = step.get('error') or 'Se agotaron los reintentos automáticos.'
            order.automation_response = json.dumps(auto_resp)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return {
                'ok': False,
                'changed': False,
                'pending_verification': False,
                'current_step_index': step_index,
                'message': (
                    f'Se agotaron los {MAX_REV_RETRIES} reintentos automáticos en el paso {step_index + 1}: '
                    f'{step.get("error") or "sin detalle"}. Requiere revisión manual.'
                ),
                'category': 'danger',
            }

        rev_attempt = prior_attempts + 1
        ext_order_id = f'{order.order_number}-s{step_index + 1}-{rev_attempt}'

        rev_payload = {
            'product_id': step.get('remote_product_id'),
            'package_id': step.get('remote_package_id'),
            'player_id': str(order.player_id or '').strip(),
            'external_order_id': ext_order_id,
        }
        if order.zone_id:
            rev_payload['player_id2'] = str(order.zone_id).strip()

        try:
            resp = requests.post(
                f'{base_url}{recharge_path}',
                json=rev_payload,
                headers={'X-API-Key': api_key, 'Content-Type': 'application/json'},
                timeout=120,
            )
            # Revendedores siempre responde JSON, incluso en 4xx/5xx (402
            # saldo insuficiente, 422 recarga fallida, etc.) — hay que
            # parsearlo siempre. Antes esto se saltaba cuando resp.ok era
            # False, así que el campo 'status': 'fallida' nunca se leía y
            # una recarga rechazada de forma definitiva (ID inválido, sin
            # stock...) se trataba igual que un timeout de red ambiguo.
            try:
                rev_data = resp.json()
            except Exception:
                rev_data = {}
            rev_ok = rev_data.get('ok', False)
            rev_status = (rev_data.get('status') or '').strip().lower()

            # ⚠️ La API whitelabel responde el 202 "en proceso" con ok=False,
            # status='procesando' y pending=True (juegos dinámicos que el
            # proveedor confirma después). NO es un fallo: antes esto caía en
            # la rama de error de abajo, anotaba "Intento N falló: your
            # request has been submited..." y quemaba un reintento, aunque la
            # recarga siguiera viva y terminara completándose. El webhook de
            # Revendedores o el scheduler confirman después vía order-status.
            if rev_status == 'procesando' or rev_data.get('pending'):
                player_name = rev_data.get('player_name', '')
                ref_no = rev_data.get('reference_no', '')
                step.update({
                    'success': False,
                    'pending_verification': True,
                    'rev_attempt': rev_attempt,
                    'external_order_id': ext_order_id,
                    'player_name': player_name,
                    'reference_no': ref_no,
                    'order_id': rev_data.get('order_id'),
                    'error': 'Recarga aceptada pero en estado "procesando" en Revendedores. Verificar manualmente.',
                })
                auto_resp['pending_verification'] = True
                auto_resp['current_step_index'] = step_index
                auto_resp['external_order_id'] = ext_order_id
                auto_resp['last_error'] = step['error']
                order.automation_response = json.dumps(auto_resp)
                order.notes = ((order.notes or '') + f'\n[Revendedores API][Paso {step_index + 1}] Recarga en estado "procesando" (intento {rev_attempt}). Ref: {ref_no or "N/A"}, Player: {player_name or "N/A"}. Pendiente de verificación.').strip()
                db.session.commit()
                return {
                    'ok': False,
                    'changed': False,
                    'pending_verification': True,
                    'current_step_index': step_index,
                    'message': f'Revendedores aceptó la recarga en el paso {step_index + 1} pero está en estado "procesando". Verifica manualmente antes de continuar.',
                    'category': 'warning',
                }

            if rev_ok:
                # rev_status vacío (API antigua) o 'completada' → éxito real
                player_name = rev_data.get('player_name', '')
                ref_no = rev_data.get('reference_no', '')
                step.update({
                    'success': True,
                    'pending_verification': False,
                    'rev_attempt': rev_attempt,
                    'external_order_id': ext_order_id,
                    'player_name': player_name,
                    'reference_no': ref_no,
                    'order_id': rev_data.get('order_id'),
                    'verified': True,
                    'error': '',
                })
                retry_suffix = f' (intento {rev_attempt})' if rev_attempt > 1 else ''
                note = f"[Revendedores API][Paso {step_index + 1}] Ref: {ref_no}, Player: {player_name}{retry_suffix}" if (ref_no or player_name) else f"[Revendedores API][Paso {step_index + 1}] Recarga completada{retry_suffix}."
                order.notes = ((order.notes or '') + '\n' + note).strip()
                continue  # éxito → pasar al siguiente paso

            # Revendedores respondió con error para este intento. Cuando ya
            # trae 'status': 'fallida' es un rechazo definitivo y
            # sincrónico (Revendedores ya reembolsó su saldo del lado de
            # ellos) — no ambiguo como un timeout, así que no vale la pena
            # reintentar solo: el mismo player_id va a fallar otra vez.
            rev_error = rev_data.get('error') or (resp.text[:200] if not rev_data else 'Error desconocido')
            rev_reported_status = str(rev_data.get('status') or '').strip().lower()
            non_retryable_cause = classify_revendedores_error(rev_error)

            # Se detiene aquí sin importar `force`: force solo debía saltar
            # el LÍMITE de reintentos para fallas ambiguas, no hacer que
            # ignoremos un rechazo que Revendedores mismo acaba de
            # confirmar como definitivo. Si alguien quiere reintentar de
            # todas formas (ID corregido u otro motivo), tendrá que
            # disparar una nueva acción explícita — el próximo intento
            # arrancará limpio porque este paso queda con external_order_id
            # fijado, y el chequeo de arriba lo confirmará como 'fallida'
            # antes de generar uno nuevo.
            #
            # Además del 'fallida' explícito, se corta también cuando el texto
            # del error identifica una causa que no se arregla reintentando
            # (paquete dado de baja, ID inválido, saldo agotado): reintentar
            # solo retrasaba el aviso al admin.
            if rev_reported_status == 'fallida' or non_retryable_cause:
                hint = _REV_NON_RETRYABLE_HINTS.get(non_retryable_cause, '')
                step.update({
                    'rev_attempt': rev_attempt,
                    'external_order_id': ext_order_id,
                    'error': rev_error,
                    'success': False,
                    'pending_verification': False,
                    'blocked_cause': non_retryable_cause or 'rejected',
                })
                auto_resp['pending_verification'] = False
                auto_resp['current_step_index'] = step_index
                auto_resp['last_error'] = rev_error
                auto_resp['blocked_cause'] = non_retryable_cause or 'rejected'
                order.automation_response = json.dumps(auto_resp)
                order.notes = ((order.notes or '') + f'\n[Revendedores API][Paso {step_index + 1}] Revendedores rechazó la recarga: {rev_error}. No se reintenta automáticamente.' + (f' {hint}' if hint else '')).strip()
                db.session.commit()
                return {
                    'ok': False,
                    'changed': False,
                    'pending_verification': False,
                    'current_step_index': step_index,
                    'blocked_cause': non_retryable_cause or 'rejected',
                    'message': (
                        f'Revendedores rechazó la recarga en el paso {step_index + 1}: {rev_error}. '
                        + (hint or 'Verifica el ID del jugador u otros datos y reintenta manualmente.')
                    ),
                    'category': 'danger',
                }

            step['rev_attempt'] = rev_attempt
            step['external_order_id'] = ext_order_id
            step['error'] = rev_error
            step['success'] = False
            step['pending_verification'] = True

            auto_resp['pending_verification'] = True
            auto_resp['current_step_index'] = step_index
            auto_resp['external_order_id'] = ext_order_id
            auto_resp['last_error'] = rev_error
            order.automation_response = json.dumps(auto_resp)
            will_retry = rev_attempt < MAX_REV_RETRIES or force
            order.notes = ((order.notes or '') + f'\n[Revendedores API][Paso {step_index + 1}] Intento {rev_attempt} falló: {rev_error}.' + (' Se reintentará automáticamente.' if will_retry else ' Reintentos agotados, requiere revisión manual.')).strip()
            db.session.commit()
            return {
                'ok': False,
                'changed': False,
                'pending_verification': True,
                'current_step_index': step_index,
                'message': f'Revendedores reportó error en el paso {step_index + 1} (intento {rev_attempt}/{MAX_REV_RETRIES}): {rev_error}.',
                'category': 'warning',
            }

        except Exception as exc:
            step['rev_attempt'] = rev_attempt
            step['external_order_id'] = ext_order_id
            step['error'] = str(exc)
            step['success'] = False
            step['pending_verification'] = True

            auto_resp['pending_verification'] = True
            auto_resp['current_step_index'] = step_index
            auto_resp['external_order_id'] = ext_order_id
            auto_resp['last_error'] = str(exc)
            order.automation_response = json.dumps(auto_resp)
            will_retry = rev_attempt < MAX_REV_RETRIES or force
            order.notes = ((order.notes or '') + f'\n[Revendedores API][Paso {step_index + 1}] Intento {rev_attempt} falló (error de red): {exc}.' + (' Se reintentará automáticamente.' if will_retry else ' Reintentos agotados, requiere revisión manual.')).strip()
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return {
                'ok': False,
                'changed': False,
                'pending_verification': True,
                'current_step_index': step_index,
                'message': f'Error contactando Revendedores API en el paso {step_index + 1} (intento {rev_attempt}/{MAX_REV_RETRIES}): {exc}.',
                'category': 'warning',
            }

    auto_resp['pending_verification'] = False
    auto_resp['current_step_index'] = None
    auto_resp['success'] = True
    order.status = 'completed'
    ensure_minigame_opportunity(order)
    award_points_for_order(order)
    order.automation_response = json.dumps(auto_resp)
    order.updated_at = datetime.utcnow()
    process_affiliate_commission(order)
    db.session.commit()
    try:
        notify_order_completed(order, order.package, order.game)
    except Exception:
        pass
    return {
        'ok': True,
        'changed': True,
        'pending_verification': False,
        'message': f'Orden #{order.order_number} completada vía Revendedores API en {len(steps)} paso(s).',
        'category': 'success',
    }


def get_revendedores_env():
    base_url = current_app.config.get('REVENDEDORES_BASE_URL', '').rstrip('/')
    api_key = current_app.config.get('REVENDEDORES_API_KEY', '')
    catalog_path = '/api/v1/products'
    recharge_path = '/api/v1/recharge'
    return base_url, api_key, catalog_path, recharge_path


def approve_order(order, delivery_proof_path=None):
    """Aprueba/completa una orden pendiente.

    Protegido con un lock por orden en la BD: dos disparadores casi
    simultáneos para la misma orden (p.ej. el poll automático del cliente
    y un admin haciendo clic en "Aprobar" a la vez, o dos pestañas de
    admin abiertas) ya no pueden ejecutar ambos la recarga externa — el
    segundo simplemente se encuentra la orden bloqueada y no hace nada.
    Sin esto, ambos podían leer status == 'pending' antes de que
    cualquiera confirmara el cambio y terminar disparando la recarga dos
    veces.
    """
    if order.status != 'pending':
        return {
            'ok': False,
            'changed': False,
            'message': 'Solo se pueden aprobar órdenes pendientes.',
            'category': 'warning',
        }

    lock_key = f'order_approve:{order.id}'
    lock_holder = uuid4().hex
    if not acquire_lock(lock_key, ORDER_APPROVE_LOCK_TTL_SECONDS, lock_holder):
        return {
            'ok': False,
            'changed': False,
            'message': 'Esta orden ya se está procesando en este momento (otra solicitud en curso). Intenta de nuevo en unos segundos.',
            'category': 'warning',
        }

    try:
        db.session.refresh(order)
        if order.status != 'pending':
            return {
                'ok': False,
                'changed': False,
                'message': 'Solo se pueden aprobar órdenes pendientes.',
                'category': 'warning',
            }
        return _approve_order_locked(order, delivery_proof_path=delivery_proof_path)
    finally:
        release_lock(lock_key, lock_holder)


def deliver_prize_to_player(game, package, player_id, zone_id=None, note='', reference_prefix='PREMIO'):
    """Entrega un premio (minijuego ganado o canje de puntos) a un ID real.

    Crea una orden interna de $0 y la manda por el MISMO camino de entrega
    automática que cualquier compra real (PIN propio o Revendedores), en
    vez de reinventar la lógica de fulfillment. Así el premio queda
    visible en Órdenes para auditoría, y si no hay stock en el momento,
    la orden simplemente se queda 'pending' para que el admin la resuelva
    a mano — igual que le pasaría a cualquier compra normal sin stock.

    Devuelve (order, approval_dict).
    """
    if not game or not package:
        return None, {
            'ok': False, 'changed': False,
            'message': 'Falta el juego o el paquete premio configurado para esta entrega.',
            'category': 'danger',
        }

    clean_player_id = str(player_id or '').strip()
    if not clean_player_id:
        return None, {
            'ok': False, 'changed': False,
            'message': 'No hay un ID de jugador válido para entregar el premio.',
            'category': 'danger',
        }

    order = Order(
        game_id=game.id,
        package_id=package.id,
        player_id=clean_player_id,
        zone_id=(str(zone_id or '').strip() or None),
        payment_method='prize',
        payment_reference=f'{reference_prefix}-{uuid4().hex[:10].upper()}',
        amount=0,
        original_amount=0,
        discount_amount=0,
        status='pending',
        notes=note or 'Premio entregado automáticamente.',
    )
    db.session.add(order)
    db.session.commit()

    approval = approve_order(order)
    return order, approval


def _claim_pin_for_order(package, order):
    """Reserva de forma atómica un PIN libre del stock para `order`.

    El lock por orden de `approve_order` impide que la MISMA orden se
    procese dos veces, pero no que dos órdenes distintas del mismo paquete
    avancen a la vez (cliente + admin, o el scheduler y una aprobación
    manual). Antes ambas leían el mismo "primer PIN sin usar" y se lo
    llevaban las dos: el bot redimía el PIN para el primer jugador y la
    segunda orden fallaba con un PIN ya quemado.

    El UPDATE condicional (`order_id IS NULL AND is_used = 0`) es atómico
    tanto en SQLite como en Postgres: solo una de las dos transacciones ve
    filas afectadas y la otra vuelve a intentar con el siguiente PIN.
    """
    for _ in range(10):
        candidate = (
            Pin.query
            .filter(
                Pin.package_id == package.id,
                Pin.is_used.is_(False),
                Pin.order_id.is_(None),
            )
            .order_by(Pin.created_at.asc())
            .first()
        )
        if candidate is None:
            return None

        try:
            claimed = (
                db.session.query(Pin)
                .filter(
                    Pin.id == candidate.id,
                    Pin.is_used.is_(False),
                    Pin.order_id.is_(None),
                )
                .update({'order_id': order.id}, synchronize_session=False)
            )
            if claimed:
                db.session.commit()
                return Pin.query.get(candidate.id)
            db.session.rollback()
        except Exception:
            db.session.rollback()
            return None
    return None


def _resolve_ambiguous_redeem(order, pin, reason):
    """Resuelve un canje del que no sabemos si consumió el PIN.

    El bot expone `POST /redeem/verify`, que consulta en el proveedor si un
    PIN ya fue canjeado **sin** canjearlo, y responde
    `{'status': 'used'|'available'|'unknown', ...}`. Preguntarle es la única
    forma de no equivocarse en los dos sentidos: dar por perdida una recarga
    que sí se hizo, o devolver al stock un PIN ya quemado que haría fallar a
    la siguiente orden.
    """
    verify_url = current_app.config.get('VPS_VERIFY_URL')
    status = 'unknown'
    if verify_url:
        try:
            resp = requests.post(
                verify_url,
                json={
                    'pin_key': str(pin.code).strip(),
                    'player_id': str(order.player_id or '').strip(),
                    'request_id': order.order_number,
                    'verify_only': True,
                },
                timeout=current_app.config.get('VPS_VERIFY_TIMEOUT', 60),
                headers={'Content-Type': 'application/json'},
            )
            data = resp.json() if resp.ok else {}
            status = str(data.get('status') or '').strip().lower() or 'unknown'
            if not status or status == 'unknown':
                if data.get('already_redeemed') or data.get('used'):
                    status = 'used'
                elif data.get('pin_still_valid'):
                    status = 'available'
        except Exception as exc:
            order.notes = ((order.notes or '') + f'\n[VPS] No se pudo verificar si el PIN se consumió: {exc}').strip()

    if status == 'used':
        # La recarga sí ocurrió: se consuma el PIN y se completa la orden en
        # vez de dejar al cliente esperando algo que ya recibió.
        pin.is_used = True
        pin.used_at = datetime.utcnow()
        pin.order_id = order.id
        order.status = 'completed'
        ensure_minigame_opportunity(order)
        award_points_for_order(order)
        order.pin_id = pin.id
        order.pin_delivered = pin.code
        order.automation_response = json.dumps({
            'success': True,
            'message': f'{reason} Verificado después: el PIN sí fue canjeado.',
        })
        order.updated_at = datetime.utcnow()
        order.notes = ((order.notes or '') + f'\n[VPS] {reason} La verificación posterior confirmó que el PIN sí se canjeó; orden completada.').strip()
        process_affiliate_commission(order)
        db.session.commit()
        try:
            notify_order_completed(order, order.package, order.game)
        except Exception:
            pass
        return {
            'ok': True,
            'changed': True,
            'message': f'Orden #{order.order_number} completada: el PIN sí se canjeó pese al error inicial.',
            'category': 'success',
        }

    if status == 'available':
        # Confirmado que el PIN sigue intacto: se puede devolver al stock.
        _release_pin(pin)
        order.notes = ((order.notes or '') + f'\n[VPS] {reason} El PIN sigue sin canjear y volvió al stock.').strip()
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {
            'ok': False,
            'changed': False,
            'message': f'{reason} El PIN no se consumió y volvió al stock. La orden sigue pendiente.',
            'category': 'danger',
        }

    # No se pudo determinar: el PIN queda reservado a esta orden, nunca de
    # vuelta al stock, para que nadie más se lo lleve.
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return {
        'ok': False,
        'changed': False,
        'message': (
            f'{reason} No se pudo confirmar si el PIN {pin.code} se canjeó, así que quedó '
            f'reservado para la orden #{order.order_number}. Revísalo antes de reintentar.'
        ),
        'category': 'danger',
    }


def _release_pin(pin):
    """Devuelve al stock un PIN reservado cuya redención no se completó."""
    if pin is None or pin.is_used:
        return
    try:
        pin.order_id = None
        db.session.commit()
    except Exception:
        db.session.rollback()


def _approve_order_locked(order, delivery_proof_path=None):
    package = order.package
    category_slug = (order.game.category.slug if order.game and order.game.category else '').lower()
    rev_mapping = get_order_auto_mapping(order)

    # Qué vía usa cada paquete lo decide el admin con el interruptor
    # "Automatizado ⚡ (usa stock de PINs)". Antes esa elección no servía de
    # nada: el mapeo de Revendedores se consultaba PRIMERO y siempre
    # retornaba, así que un paquete marcado para stock y con PINs cargados
    # se recargaba igual por Revendedores y el bot nunca se usaba.
    #
    # Ahora manda la configuración: con stock disponible se usa el bot, y si
    # el stock se agotó Revendedores queda como respaldo para que la orden no
    # se trabe (que es justamente lo que el mapeo permite cubrir).
    pin = _claim_pin_for_order(package, order) if package.is_automated else None

    if pin is None and rev_mapping:
        result = process_revendedores_queue(order, force=True)
        if result:
            return result

    if package.is_automated and pin is None:
        return {
            'ok': False,
            'changed': False,
            'message': 'Sin stock de códigos para este paquete. Carga PINs primero.',
            'category': 'danger',
        }

    needs_pin_delivery = package.is_automated or category_slug == 'tarjetas'
    if needs_pin_delivery and pin is None:
        pin = _claim_pin_for_order(package, order)
        if not pin:
            return {
                'ok': False,
                'changed': False,
                'message': 'Sin stock de códigos para este paquete. Carga PINs primero.',
                'category': 'danger',
            }

    if package.is_automated:
        vps_url = current_app.config.get('VPS_REDEEM_URL')
        vps_timeout = current_app.config.get('VPS_TIMEOUT', 120)

        payload = {
            'pin_key': str(pin.code).strip(),
            'player_id': str(order.player_id).strip(),
            'full_name': current_app.config.get('VPS_FULL_NAME', 'Usuario Recarga'),
            'birth_date': current_app.config.get('VPS_BIRTH_DATE', '01/01/1995'),
            'country': current_app.config.get('VPS_COUNTRY', 'Venezuela'),
            'request_id': order.order_number,
        }

        try:
            resp = requests.post(
                vps_url,
                json=payload,
                timeout=vps_timeout,
                headers={'Content-Type': 'application/json'},
            )

            try:
                data = resp.json()
            except Exception:
                data = {}

            mensaje = data.get('message') or data.get('mensaje') or data.get('error') or ''
            player_name = data.get('player_name') or data.get('nombre_jugador') or ''

            # Solo se da por buena una confirmación EXPLÍCITA del bot. Antes,
            # un 200 cuyo cuerpo no fuera JSON (una página de error de nginx,
            # un servicio equivocado escuchando en ese puerto, un proxy en
            # medio) caía en la rama `not data` y se trataba como éxito: se
            # quemaba el PIN y la orden se marcaba completada sin que nadie
            # hubiera recargado nada.
            exito = bool(
                resp.ok
                and (
                    data.get('success') is True
                    or data.get('exito') is True
                    or str(data.get('status') or '').strip().lower() in ('ok', 'success', 'completed', 'completada')
                )
            )

            if not exito and not data:
                mensaje = mensaje or (
                    f'El servicio de automatización respondió HTTP {resp.status_code} sin un JSON válido. '
                    f'Verifica que VPS_REDEEM_URL ({vps_url}) apunte al bot de recargas.'
                )

            if exito:
                pin.is_used = True
                pin.used_at = datetime.utcnow()
                pin.order_id = order.id
                order.status = 'completed'
                ensure_minigame_opportunity(order)
                award_points_for_order(order)
                order.pin_id = pin.id
                order.pin_delivered = pin.code
                order.automation_response = json.dumps({
                    'success': True,
                    'message': mensaje,
                    'player_name': player_name,
                })
                order.updated_at = datetime.utcnow()
                process_affiliate_commission(order)
                db.session.commit()
                try:
                    notify_order_completed(order, order.package, order.game)
                except Exception:
                    pass
                extra = f' (Jugador: {player_name})' if player_name else ''
                return {
                    'ok': True,
                    'changed': True,
                    'message': f'Orden #{order.order_number} completada vía automatización.{extra}',
                    'category': 'success',
                }

            # El bot distingue un rechazo limpio de un resultado AMBIGUO: con
            # `manual_review: true` la validación ya se envió al proveedor y
            # el PIN pudo consumirse igual. Devolver ese PIN al stock le
            # entregaría un código quemado a la siguiente orden, así que se
            # resuelve preguntándole al propio bot si se usó.
            if data.get('manual_review'):
                return _resolve_ambiguous_redeem(
                    order, pin, mensaje or 'Resultado ambiguo del bot de recargas.'
                )

            # Rechazo limpio: el PIN nunca se usó, vuelve al stock.
            _release_pin(pin)
            return {
                'ok': False,
                'changed': False,
                'message': (
                    f'Redención fallida: {mensaje or "Error desconocido del VPS"}. '
                    'El PIN se mantiene en stock. La orden sigue pendiente.'
                ),
                'category': 'danger',
            }
        except requests.exceptions.Timeout:
            # También ambiguo: la petición llegó al bot y pudo haber redimido
            # el PIN aunque no alcanzáramos a leer la respuesta.
            return _resolve_ambiguous_redeem(
                order, pin, f'El bot de recargas no respondió en {vps_timeout}s.'
            )
        except requests.exceptions.ConnectionError:
            # Nunca se estableció la conexión: el PIN con seguridad no se usó.
            _release_pin(pin)
            return {
                'ok': False,
                'changed': False,
                'message': (
                    f'No se pudo conectar al bot de recarga en {vps_url}. '
                    'Verifica que el servicio esté activo en el VPS y que VPS_REDEEM_URL apunte a él.'
                ),
                'category': 'danger',
            }
        except Exception as exc:
            return {
                'ok': False,
                'changed': False,
                'message': (
                    f'Error inesperado al contactar el VPS: {exc}. '
                    f'El PIN {pin.code} quedó reservado para la orden #{order.order_number}.'
                ),
                'category': 'danger',
            }

    if needs_pin_delivery:
        pin.is_used = True
        pin.used_at = datetime.utcnow()
        pin.order_id = order.id
        order.status = 'completed'
        ensure_minigame_opportunity(order)
        award_points_for_order(order)
        order.pin_id = pin.id
        order.pin_delivered = pin.code
        order.updated_at = datetime.utcnow()
        process_affiliate_commission(order)
        db.session.commit()
        try:
            notify_order_completed(order, order.package, order.game, pin_code=pin.code)
        except Exception:
            pass
        return {
            'ok': True,
            'changed': True,
            'message': f'Orden #{order.order_number} completada y PIN entregado.',
            'category': 'success',
        }

    order.status = 'approved'
    if delivery_proof_path:
        order.delivery_proof = delivery_proof_path
    order.updated_at = datetime.utcnow()
    # El pago ya está confirmado aunque la recarga sea manual: el cliente
    # no debería perder su giro gratis ni sus puntos solo porque este
    # producto no tiene entrega automática.
    ensure_minigame_opportunity(order)
    award_points_for_order(order)
    process_affiliate_commission(order)
    db.session.commit()
    try:
        notify_order_approved(order, order.package, order.game, delivery_proof_path=delivery_proof_path)
    except Exception:
        pass
    return {
        'ok': True,
        'changed': True,
        'message': f'Orden #{order.order_number} aprobada.',
        'category': 'success',
    }