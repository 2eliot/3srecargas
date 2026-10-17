"""Central One: segundo proveedor de recargas automáticas.

Misma idea que Revendedores (una cola de pasos por orden), con diferencias que
importan para no recargar dos veces ni perder dinero:

* Cada pedido lleva una llave de idempotencia (`Idempotency-Key`). Si la
  conexión se cae después de que Central One retuvo el saldo, se reenvía el
  MISMO pedido con la MISMA llave y devuelve el pedido original en vez de
  crear otro. La llave se guarda en la orden antes de enviar.
* La entrega es asíncrona: crear el pedido solo lo deja "confirmado" y hay que
  consultarlo (`GET /orders/{id}`) hasta que termine. Un pedido fallido no se
  cobra: el saldo retenido se libera.
* Un rechazo explícito (sin saldo, sin stock, producto inválido) garantiza que
  no se creó ningún pedido, así que el siguiente intento usa una llave nueva.
  Un error de red o un 5xx NO garantiza nada: se reintenta con la misma llave.

* Los productos de CÓDIGO (gift cards, PINs) entregan el código aparte:
  `GET /orders/{id}/codes`, que exige el permiso `codes:read` de la llave. El
  código se guarda en la orden (`pin_delivered`, como tus PINs propios) y la
  orden no se completa hasta tenerlo.

Todo lo que entra por aquí pasa por `order_processing.process_order_delivery`,
que es quien decide si, tras un rechazo definitivo, la orden pasa al proveedor
de respaldo.
"""
import hashlib
import json
import re
import time
from uuid import uuid4
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import requests
from flask import current_app

from ..models import CentralOneCatalogItem, db
from .locks import acquire_lock, release_lock
from .minigames import ensure_minigame_opportunity
from .notifications import notify_order_completed
from .order_pause import order_paused_now, paused_result
from .order_processing import get_order_centralone_items, process_affiliate_commission
from .points import award_points_for_order

CO_REQUEST_TIMEOUT = 30

# Rechazos por saldo o stock: se reintentan solos (con una llave nueva).
CO_RETRY_SECONDS = 5 * 60
CO_MAX_RETRY_HOURS = 24
# Si Central One no responde (red, timeout, 5xx) se reintenta pronto con la
# MISMA llave: es seguro porque devuelve el pedido original si ya existía.
CO_UNKNOWN_RETRY_SECONDS = 60

# Cada cuánto se pregunta por un pedido en curso: Central One pide no
# consultar en bucle (60 lecturas/min por llave).
CO_POLL_FAST_SECONDS = 20
CO_POLL_FAST_WINDOW_SECONDS = 5 * 60
CO_POLL_SLOW_SECONDS = 5 * 60
# Pausa tras crear el pedido antes de la primera consulta (la mitad se entrega
# en menos de 4 s).
CO_FIRST_CHECK_DELAY_SECONDS = 3
CO_STUCK_HOURS = 24

_HINTS = {
    'mapping': ('Revisa el mapeo de este paquete (Admin → Mapeo de proveedores) y vuelve a sincronizar el '
                'catálogo de Central One. Luego dale Aprobar.'),
    'player': 'Verifica el ID o el servidor del jugador con el cliente antes de reintentar.',
    'config': 'Revisa la llave de Central One (CENTRALONE_API_KEY) y que tenga permiso para crear pedidos. Luego dale Aprobar.',
    'rejected': 'Central One no pudo entregar la recarga y no cobró nada. Dale Aprobar para reintentar.',
    'code_missing': ('Central One entregó el pedido pero no devolvió el código. Revísalo en su portal y dale '
                     'Aprobar para volver a pedirlo.'),
    'code_hidden': ('Este paquete no está en la sección de códigos de tu web, así que el cliente no ve el código. '
                    'Está en el detalle de la orden: canjéalo tú y dale Aprobar, o pasa el juego a la sección de códigos.'),
}

# Bloqueos de una orden que Central One YA entregó: nunca se pasa al respaldo
# (el proveedor sí recargó/entregó), solo se resuelven a mano.
_DELIVERED_BLOCKS = ('code_missing', 'code_hidden')

_ZONE_KEY_RE = re.compile(r'zone|server|region|area|world|realm', re.I)


# ─── Cliente de la API ───────────────────────────────────────────────────────

class CentralOneError(Exception):
    """Central One respondió con un error 4xx de su sobre {"error": {...}}.
    Garantiza que la petición NO se procesó."""

    def __init__(self, status, code, message='', retry_after=None):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code
        self.retry_after = retry_after


class CentralOneUnknown(Exception):
    """No se sabe qué pasó (sin conexión, timeout, 5xx, respuesta rara): la
    operación pudo o no haberse hecho."""


def get_centralone_env():
    base_url = (current_app.config.get('CENTRALONE_BASE_URL') or '').strip().rstrip('/')
    api_key = (current_app.config.get('CENTRALONE_API_KEY') or '').strip()
    return base_url, api_key


def _retry_after_seconds(resp):
    try:
        return max(1, int(float(resp.headers.get('Retry-After'))))
    except (TypeError, ValueError, AttributeError):
        return None


def _request(method, path, json_body=None, params=None, extra_headers=None, timeout=CO_REQUEST_TIMEOUT):
    base_url, api_key = get_centralone_env()
    if not base_url or not api_key:
        raise CentralOneError(0, 'not_configured', 'Central One no está configurado (falta CENTRALONE_API_KEY).')

    headers = {'Authorization': f'Bearer {api_key}', 'Accept': 'application/json'}
    if json_body is not None:
        headers['Content-Type'] = 'application/json'
    if extra_headers:
        headers.update(extra_headers)

    try:
        resp = requests.request(method, f'{base_url}{path}', json=json_body, params=params,
                                headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise CentralOneUnknown(f'No se pudo contactar a Central One: {exc}') from exc

    try:
        data = resp.json()
    except ValueError:
        data = None

    if resp.status_code >= 500:
        raise CentralOneUnknown(f'Central One respondió HTTP {resp.status_code} (falla de su lado).')
    if resp.status_code >= 400:
        err = data.get('error') if isinstance(data, dict) else None
        if isinstance(err, dict) and err.get('code'):
            raise CentralOneError(resp.status_code, str(err['code']), str(err.get('message') or ''),
                                  retry_after=_retry_after_seconds(resp))
        raise CentralOneUnknown(f'Central One respondió HTTP {resp.status_code} sin detalle.')
    if not isinstance(data, dict):
        raise CentralOneUnknown('Central One respondió algo que no es JSON.')
    return data


def fetch_catalog():
    return _request('GET', '/catalog').get('items') or []


def fetch_balance():
    return _request('GET', '/balance')


def create_order(product_id, target_payload, idempotency_key, note=''):
    line = {'catalog_item_id': product_id, 'quantity': 1}
    if target_payload:
        line['target_payload'] = target_payload
    body = {'items': [line]}
    if note:
        body['note'] = note[:500]
    data = _request('POST', '/orders', json_body=body, extra_headers={'Idempotency-Key': idempotency_key})
    return data.get('order') or {}


def fetch_order(remote_order_id):
    return _request('GET', f'/orders/{remote_order_id}').get('order') or {}


def fetch_order_codes(remote_order_id):
    """Códigos de un pedido ya entregado. Necesita el permiso `codes:read`."""
    return _request('GET', f'/orders/{remote_order_id}/codes').get('order') or {}


def probe_codes_permission():
    """¿La llave tiene el permiso `codes:read`? Se pregunta por un pedido que no
    existe: sin el permiso responde 403 (insufficient_scope) y con él 404. Es
    una lectura, no gasta nada. Devuelve True, False o None si no se pudo saber."""
    try:
        _request('GET', '/orders/00000000-0000-0000-0000-000000000000/codes')
    except CentralOneError as exc:
        if exc.code == 'insufficient_scope':
            return False
        return True
    except CentralOneUnknown:
        return None
    return True


def extract_pin(code):
    """Central One puede mandar "PIN - SERIAL": el cliente canjea solo el PIN
    (la primera parte); el serial es el número de control del proveedor."""
    return str(code or '').split(' - ', 1)[0].strip()


def is_code_section(order):
    """La orden es de la sección de códigos de la web (categoría 'tarjetas'),
    donde el cliente recibe el código en su orden y por correo."""
    category = order.game.category if order.game else None
    return bool(category and (category.slug or '').lower() == 'tarjetas')


def _append_codes(order, pins):
    current = [c for c in (order.pin_delivered or '').split('\n') if c.strip()]
    for pin in pins:
        if pin and pin not in current:
            current.append(pin)
    order.pin_delivered = '\n'.join(current)


# ─── Catálogo ────────────────────────────────────────────────────────────────

def _to_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def normalize_catalog_item(raw):
    if not isinstance(raw, dict):
        return None
    product_id = str(raw.get('product_id') or '').strip()
    if not product_id:
        return None
    name = str(raw.get('name') or '').strip()
    fields = raw.get('target_fields') if isinstance(raw.get('target_fields'), list) else []
    schema = raw.get('target_schema') if isinstance(raw.get('target_schema'), list) else []
    stock = raw.get('available_stock')
    return {
        'product_id': product_id,
        'sku': str(raw.get('sku') or '')[:120],
        'name': name[:255],
        'family_id': str(raw.get('product_family_id') or '')[:120],
        'family_name': (str(raw.get('product_family_name') or '').strip() or name)[:200],
        'category': str(raw.get('category') or '')[:60],
        'product_type': str(raw.get('product_type') or '')[:60],
        'currency': str(raw.get('currency') or 'USD')[:10],
        'price': _to_decimal(raw.get('reseller_price')),
        'in_stock': bool(raw.get('in_stock', True)),
        # None = el proveedor no informa cantidad (no es lo mismo que cero).
        'available_stock': int(stock) if isinstance(stock, (int, float)) and not isinstance(stock, bool) else None,
        'requires_target': bool(raw.get('requires_target')),
        'target_fields': json.dumps(fields, ensure_ascii=False),
        'target_schema': json.dumps(schema, ensure_ascii=False),
        'price_updated_at': str(raw.get('price_updated_at') or '')[:40],
        'active': str(raw.get('status') or 'active').strip().lower() == 'active',
        'raw_json': json.dumps(raw, ensure_ascii=False),
    }


def sync_catalog():
    """Trae el catálogo de Central One y lo guarda. Lanza CentralOneError /
    CentralOneUnknown si no se pudo; devuelve un resumen."""
    normalized = [n for n in (normalize_catalog_item(raw) for raw in fetch_catalog()) if n]
    if not normalized:
        # Una respuesta vacía puede ser un fallo de su lado: no se desactiva
        # todo el catálogo por eso.
        raise CentralOneError(0, 'empty_catalog', 'Central One no devolvió ningún producto habilitado para tu cuenta.')

    created = updated = deactivated = 0
    seen = set()
    now = datetime.utcnow()
    for ent in normalized:
        seen.add(ent['product_id'])
        row = CentralOneCatalogItem.query.filter_by(product_id=ent['product_id']).first()
        if not row:
            row = CentralOneCatalogItem(**ent)
            db.session.add(row)
            created += 1
        else:
            for key, value in ent.items():
                setattr(row, key, value)
            updated += 1
        row.synced_at = now

    for row in CentralOneCatalogItem.query.filter(CentralOneCatalogItem.active.is_(True)).all():
        if row.product_id not in seen:
            row.active = False
            deactivated += 1
    db.session.commit()

    games = {}
    for ent in normalized:
        games[ent['family_name']] = games.get(ent['family_name'], 0) + 1
    return {
        'created': created,
        'updated': updated,
        'deactivated': deactivated,
        'total': len(normalized),
        'games': games,
    }


# ─── Datos del jugador del producto ──────────────────────────────────────────

def _load_json_list(text):
    try:
        value = json.loads(text or '[]')
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _resolve_option(value, options):
    """`value` de la opción que corresponde a lo escrito (por valor o por
    nombre), o None. Central One solo acepta el `value`, no el nombre."""
    text = str(value or '').strip().lower()
    if not text:
        return None
    for opt in options or []:
        if not isinstance(opt, dict):
            continue
        if text in (str(opt.get('value') or '').strip().lower(), str(opt.get('label') or '').strip().lower()):
            return str(opt.get('value'))
    return None


def build_target_payload(order, item, defaults=None):
    """Arma el `target_payload` que pide el producto con lo que guarda la
    tienda (ID del jugador y zona). Devuelve (payload, error, causa)."""
    fields = [str(f) for f in _load_json_list(item.target_fields) if f]
    if not fields:
        if item.requires_target:
            return {}, 'El producto exige datos del jugador pero Central One no dice cuáles.', 'mapping'
        return {}, '', ''

    schema = {str(f.get('key')): f for f in _load_json_list(item.target_schema) if isinstance(f, dict) and f.get('key')}
    defaults = {str(k): str(v).strip() for k, v in (defaults or {}).items() if str(v or '').strip()}
    player_id = str(order.player_id or '').strip()
    zone_id = str(order.zone_id or '').strip()

    payload = {}
    player_taken = False
    for key in fields:
        spec = schema.get(key) or {}
        options = spec.get('options') if spec.get('type') == 'select' else None
        valid = ', '.join(str(o.get('label') or o.get('value')) for o in (options or []) if isinstance(o, dict))

        # De dónde sale el valor de este campo.
        if _ZONE_KEY_RE.search(key):
            role, candidates = 'zone', [zone_id, defaults.get(key)]
        elif key in defaults:
            role, candidates = 'fixed', [defaults[key]]
        elif not player_taken:
            role, candidates = 'player', [player_id]
            player_taken = True
        else:
            role, candidates = 'unknown', []

        value = None
        for candidate in candidates:
            candidate = str(candidate or '').strip()
            if not candidate:
                continue
            if options is None:
                value = candidate
                break
            resolved = _resolve_option(candidate, options)
            if resolved is not None:
                value = resolved
                break

        if value is None:
            if role == 'zone' and zone_id and options is not None:
                return {}, f"'{zone_id}' no es un valor válido para '{key}' (opciones: {valid}).", 'player'
            if role == 'player':
                if not player_id:
                    return {}, 'La orden no tiene ID de jugador.', 'player'
                return {}, f"'{player_id}' no es un valor válido para '{key}' (opciones: {valid}).", 'player'
            if role == 'fixed':
                return {}, f"El valor fijo de '{key}' en el mapeo no es válido (opciones: {valid}).", 'mapping'
            return {}, (f"Este producto pide el dato '{key}' y la tienda no lo tiene. Ponle un valor fijo en el "
                        "mapeo o activa 'Zone ID' en el juego."), 'mapping'
        payload[key] = value
    return payload, '', ''


# ─── Estado de la orden ──────────────────────────────────────────────────────

def _parse_iso(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _is_future(value):
    moment = _parse_iso(value)
    return bool(moment and datetime.utcnow() < moment)


def _later(seconds):
    return (datetime.utcnow() + timedelta(seconds=seconds)).isoformat()


def _note(order, text):
    if text not in (order.notes or ''):
        order.notes = ((order.notes or '') + '\n' + text).strip()


def _sync_flags(auto_resp, steps):
    auto_resp['pending_verification'] = any(s.get('pending_verification') for s in steps)
    auto_resp['current_step_index'] = next((i for i, s in enumerate(steps) if not s.get('success')), None)
    auto_resp['step_count'] = len(steps)
    auto_resp['blocked_cause'] = next((s.get('blocked_cause') for s in steps if s.get('blocked_cause')), '')


def _persist(order, auto_resp, steps):
    """Guarda el estado de la orden. Si la base falla no se interrumpe la
    entrega: la llave de idempotencia se vuelve a generar igual en el próximo
    intento (depende de la orden, el paso y el número de intento), así que
    reenviar el pedido no puede duplicarlo."""
    _sync_flags(auto_resp, steps)
    order.automation_response = json.dumps(auto_resp)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('[CentralOne] No se pudo guardar el estado de la orden %s', order.order_number)


def _load_state(order, items, base_state=None):
    if isinstance(base_state, dict):
        auto_resp = dict(base_state)
    else:
        try:
            auto_resp = json.loads(order.automation_response or '{}')
        except (TypeError, ValueError):
            auto_resp = {}
        if not isinstance(auto_resp, dict):
            auto_resp = {}

    same_provider = auto_resp.get('source') == 'centralone_api'
    existing = auto_resp.get('steps') if same_provider and isinstance(auto_resp.get('steps'), list) else []
    steps = []
    for idx, (item, _defaults) in enumerate(items):
        cur = existing[idx] if idx < len(existing) and isinstance(existing[idx], dict) else {}
        steps.append({
            'slot': idx + 1,
            'catalog_id': item.id,
            'remote_product_id': item.product_id,
            'remote_product_name': item.family_name or '',
            'remote_package_name': item.name or '',
            'success': bool(cur.get('success')),
            'pending_verification': bool(cur.get('pending_verification')),
            'attempt': int(cur.get('attempt') or 0),
            'idempotency_key': cur.get('idempotency_key') or '',
            'remote_order_id': cur.get('remote_order_id') or '',
            'reference_code': cur.get('reference_code') or '',
            'remote_status': cur.get('remote_status') or '',
            'cost_usd': cur.get('cost_usd') or '',
            'sent_at': cur.get('sent_at') or '',
            'next_check_at': cur.get('next_check_at') or '',
            'error': cur.get('error') or '',
            'blocked_cause': cur.get('blocked_cause') or '',
            'transient_failures': int(cur.get('transient_failures') or 0),
            'retry_after': cur.get('retry_after') or '',
            'first_transient_at': cur.get('first_transient_at') or '',
            'previous_orders': cur.get('previous_orders') or [],
            'awaiting_codes': bool(cur.get('awaiting_codes')),
            'codes_expected': int(cur.get('codes_expected') or 0),
            'codes_saved': bool(cur.get('codes_saved')),
        })
    auto_resp['source'] = 'centralone_api'
    auto_resp['provider'] = 'centralone'
    auto_resp['steps'] = steps
    _sync_flags(auto_resp, steps)
    return auto_resp


# ─── Resultados ──────────────────────────────────────────────────────────────

def _pending(step_index, message):
    return {
        'ok': False,
        'changed': False,
        'pending_verification': True,
        'current_step_index': step_index,
        'message': message,
        'category': 'warning',
        'provider': 'centralone',
    }


def _rejection_flags(steps, step_index, cause):
    """Central One confirmó que NO entregó nada en este intento. Si además no
    se había hecho ningún paso antes, la orden puede pasar al respaldo."""
    steps_done = sum(1 for s in steps if s.get('success'))
    return {
        'provider': 'centralone',
        'definitive_rejection': True,
        'rejection_cause': cause,
        'steps_done': steps_done,
        'safe_to_switch': step_index == 0 and steps_done == 0,
    }


def _blocked(order, auto_resp, steps, step_index, step, cause, error, label, clear_key=True):
    step.update({
        'success': False,
        'pending_verification': False,
        'blocked_cause': cause,
        'error': error,
        'retry_after': '',
        'next_check_at': '',
    })
    if clear_key:
        step['idempotency_key'] = ''
    auto_resp['last_error'] = error
    hint = _HINTS.get(cause, '')
    if cause in _DELIVERED_BLOCKS:
        note = f'{label} {error}. {hint}'
        message = f'{error}. {hint}'
    else:
        note = f'{label} Central One rechazó la recarga: {error}. No se reintenta automáticamente.' + (f' {hint}' if hint else '')
        message = f'Central One rechazó la recarga en el paso {step_index + 1}: {error}. {hint}'.strip()
    _note(order, note)
    _persist(order, auto_resp, steps)
    result = {
        'ok': False,
        'changed': False,
        'pending_verification': False,
        'current_step_index': step_index,
        'blocked_cause': cause,
        'message': message,
        'category': 'danger',
    }
    if cause not in _DELIVERED_BLOCKS:
        result.update(_rejection_flags(steps, step_index, cause))
    return result


# ─── Cola de recargas ────────────────────────────────────────────────────────

def process_centralone_queue(order, base_state=None, force=False):
    """Avanza la cola de recargas de Central One para `order`.

    Igual que la de Revendedores: no reintenta en un bucle bloqueante; quien la
    vuelva a llamar (scheduler, polling del admin, Aprobar) continúa donde
    quedó. `force=True` (acción explícita del admin o primer intento tras
    verificar el pago) salta las esperas y reintenta los bloqueos que se
    arreglan sin tocar la orden."""
    items = get_order_centralone_items(order)
    if not items:
        return None

    auto_resp = _load_state(order, items, base_state=base_state)
    steps = auto_resp['steps']

    base_url, api_key = get_centralone_env()
    if not base_url or not api_key:
        error = 'Central One no está configurado (falta CENTRALONE_API_KEY).'
        auto_resp['last_error'] = error
        for step in steps:
            if not step['success'] and not step['remote_order_id']:
                step['pending_verification'] = False
        _persist(order, auto_resp, steps)
        return {
            'ok': False,
            'changed': False,
            'pending_verification': False,
            'message': error,
            'category': 'warning',
            'provider': 'centralone',
            'definitive_rejection': True,
            'rejection_cause': 'config',
            'steps_done': sum(1 for s in steps if s['success']),
            'safe_to_switch': not any(s['success'] or s['remote_order_id'] or s['idempotency_key'] for s in steps),
        }

    for step_index, step in enumerate(steps):
        if step['success']:
            continue
        # Pausa de emergencia: se revisa antes de CADA paso. Lo ya entregado queda guardado para que al reanudar no se repita.
        if order_paused_now(order):
            if any(s.get('success') for s in steps):
                _persist(order, auto_resp, steps)
            return {**paused_result(order), 'provider': 'centralone'}
        item, defaults = items[step_index]
        outcome = _advance_step(order, auto_resp, steps, step_index, step, item, defaults, force)
        if outcome is not None:
            return outcome

    auto_resp['pending_verification'] = False
    auto_resp['current_step_index'] = None
    auto_resp['blocked_cause'] = ''
    auto_resp['success'] = True
    order.status = 'completed'
    ensure_minigame_opportunity(order)
    award_points_for_order(order)
    order.automation_response = json.dumps(auto_resp)
    order.updated_at = datetime.utcnow()
    process_affiliate_commission(order)
    db.session.commit()
    try:
        # En la sección de códigos el aviso lleva el código (igual que con tus PINs).
        notify_order_completed(order, order.package, order.game,
                               pin_code=(order.pin_delivered or None) if is_code_section(order) else None)
    except Exception:
        pass
    return {
        'ok': True,
        'changed': True,
        'pending_verification': False,
        'message': f'Orden #{order.order_number} completada vía Central One en {len(steps)} paso(s).',
        'category': 'success',
        'provider': 'centralone',
    }


def _advance_step(order, auto_resp, steps, step_index, step, item, defaults, force):
    """Avanza UN paso. Devuelve None si quedó completado (hay que seguir con
    el siguiente) o el resultado con el que se detiene la cola."""
    label = f'[Central One][Paso {step_index + 1}]'

    blocked = step.get('blocked_cause') or ''
    if blocked == 'code_hidden' and force:
        # El admin ya vio el código en el detalle de la orden y lo canjeó él.
        step.update({'success': True, 'pending_verification': False, 'blocked_cause': '', 'error': ''})
        auto_resp['blocked_cause'] = ''
        _note(order, f'{label} Aprobación manual: se da por entregado el código de Central One (canjeado por el admin).')
        _persist(order, auto_resp, steps)
        return None
    if blocked:
        if force and blocked != 'player':
            # Se arreglan sin tocar la orden (llave, mapeo, un fallo ajeno):
            # una aprobación explícita lo vuelve a intentar con llave nueva.
            step['blocked_cause'] = ''
            step['error'] = ''
            auto_resp['blocked_cause'] = ''
            _note(order, f'{label} Reintento manual (intento {int(step.get("attempt") or 0) + 1}) tras un bloqueo por "{blocked}".')
        else:
            hint = _HINTS.get(blocked, '')
            if blocked in _DELIVERED_BLOCKS:
                return {
                    'ok': False,
                    'changed': False,
                    'pending_verification': False,
                    'current_step_index': step_index,
                    'blocked_cause': blocked,
                    'message': f"{step.get('error') or 'Central One ya entregó el pedido'}. {hint}".strip(),
                    'category': 'danger',
                }
            return {
                'ok': False,
                'changed': False,
                'pending_verification': False,
                'current_step_index': step_index,
                'blocked_cause': blocked,
                'message': (f"Central One ya rechazó esta recarga en el paso {step_index + 1}: "
                            f"{step.get('error') or 'rechazo definitivo'}. {hint}").strip(),
                'category': 'danger',
                **_rejection_flags(steps, step_index, blocked),
            }

    if step.get('remote_order_id'):
        return _poll_step(order, auto_resp, steps, step_index, step, force, label)
    return _create_step_order(order, auto_resp, steps, step_index, step, item, defaults, force, label)


def _create_step_order(order, auto_resp, steps, step_index, step, item, defaults, force, label):
    now = datetime.utcnow()

    if not force and _is_future(step.get('retry_after')):
        return _pending(step_index, 'Central One no pudo crear la recarga hace poco; se reintentará sola en unos minutos.')
    first_transient = _parse_iso(step.get('first_transient_at'))
    if not force and first_transient and now - first_transient > timedelta(hours=CO_MAX_RETRY_HOURS):
        step['pending_verification'] = False
        _persist(order, auto_resp, steps)
        return {
            'ok': False,
            'changed': False,
            'pending_verification': False,
            'current_step_index': step_index,
            'message': (f'Central One no pudo crear esta recarga durante {CO_MAX_RETRY_HOURS} h: se dejó de '
                        'reintentar sola. Dale "Aprobar" para intentarlo de nuevo.'),
            'category': 'danger',
            'provider': 'centralone',
        }

    payload, error, cause = build_target_payload(order, item, defaults)
    if error:
        return _blocked(order, auto_resp, steps, step_index, step, cause, error, label)

    key = step.get('idempotency_key')
    if not key:
        step['attempt'] = int(step.get('attempt') or 0) + 1
        # La llave depende también del ID: si el admin corrige el ID del
        # jugador, el pedido es otro y no debe devolver el viejo (el del ID
        # equivocado); con el mismo ID, los reintentos sí repiten la llave.
        target_sig = hashlib.sha1(f'{order.player_id}|{order.zone_id}'.encode('utf-8')).hexdigest()[:6]
        key = f'3sr-{order.order_number}-s{step_index + 1}-a{step["attempt"]}-{target_sig}'
        step['idempotency_key'] = key
        step['pending_verification'] = True
        # La llave se guarda ANTES de enviar: si el proceso muere a mitad de
        # camino, el reintento usa la misma y Central One devuelve el pedido
        # que ya hubiera creado en vez de crear otro.
        _persist(order, auto_resp, steps)

    try:
        remote = create_order(item.product_id, payload, key, note=f'Orden {order.order_number}')
    except CentralOneUnknown as exc:
        return _unknown_outcome(order, auto_resp, steps, step_index, step, str(exc), label)
    except CentralOneError as exc:
        return _create_error(order, auto_resp, steps, step_index, step, exc, label, now)

    remote_id = str(remote.get('id') or '').strip()
    if not remote_id:
        return _unknown_outcome(order, auto_resp, steps, step_index, step,
                                'Central One aceptó el pedido pero no devolvió su id.', label)

    step.update({
        'remote_order_id': remote_id,
        'reference_code': str(remote.get('reference_code') or ''),
        'remote_status': str(remote.get('status') or ''),
        'cost_usd': str(remote.get('total_sale_amount') or ''),
        'sent_at': now.isoformat(),
        'pending_verification': True,
        'error': '',
        'retry_after': '',
        'next_check_at': '',
    })
    cost = f' (costo USD {step["cost_usd"]})' if step['cost_usd'] else ''
    _note(order, f'{label} Pedido {step["reference_code"] or remote_id} creado en Central One{cost}.')
    _persist(order, auto_resp, steps)

    if CO_FIRST_CHECK_DELAY_SECONDS:
        time.sleep(CO_FIRST_CHECK_DELAY_SECONDS)
    return _poll_step(order, auto_resp, steps, step_index, step, True, label)


def _unknown_outcome(order, auto_resp, steps, step_index, step, detail, label):
    """No se sabe si el pedido se creó. Se conserva la llave: reenviarla es
    seguro y aclara el resultado."""
    step.update({
        'error': detail,
        'pending_verification': True,
        'retry_after': _later(CO_UNKNOWN_RETRY_SECONDS),
    })
    if not step.get('first_transient_at'):
        step['first_transient_at'] = datetime.utcnow().isoformat()
    auto_resp['last_error'] = detail
    _note(order, f'{label} No se pudo confirmar con Central One: {detail} Se reintenta sola sin riesgo de duplicar (misma llave).')
    _persist(order, auto_resp, steps)
    return _pending(step_index, f'No se pudo confirmar con Central One ({detail}). Se reintentará sola.')


def _create_error(order, auto_resp, steps, step_index, step, exc, label, now):
    code = exc.code
    if code in ('insufficient_balance', 'insufficient_stock'):
        cause = 'balance' if code == 'insufficient_balance' else 'stock'
        why = 'sin saldo' if cause == 'balance' else 'sin stock'
        step.update({
            'idempotency_key': '',  # rechazo explícito: no se creó nada; el próximo intento usa otra llave
            'error': exc.message,
            'pending_verification': True,
            'blocked_cause': '',
            'transient_failures': int(step.get('transient_failures') or 0) + 1,
            'retry_after': _later(CO_RETRY_SECONDS),
        })
        if not step.get('first_transient_at'):
            step['first_transient_at'] = now.isoformat()
        auto_resp['last_error'] = exc.message
        _note(order, (f'{label} Central One no pudo crear la recarga ({why}). Se reintentará sola cada '
                      f'{CO_RETRY_SECONDS // 60} minutos durante {CO_MAX_RETRY_HOURS} h.'))
        _persist(order, auto_resp, steps)
        return {
            **_pending(step_index, f'Central One no pudo crear la recarga ahora ({why}). '
                                   f'Se reintentará sola cada {CO_RETRY_SECONDS // 60} minutos.'),
            **_rejection_flags(steps, step_index, cause),
        }

    if code == 'rate_limited':
        # Central One no la procesó: se espera lo que pide y se reenvía igual.
        step['retry_after'] = _later(max(5, exc.retry_after or 30))
        step['pending_verification'] = True
        _persist(order, auto_resp, steps)
        return _pending(step_index, 'Central One pidió esperar un momento (límite de uso); se reintentará sola.')

    if code in ('invalid_api_key', 'insufficient_scope', 'not_configured'):
        return _blocked(order, auto_resp, steps, step_index, step, 'config', exc.message, label)
    if code in ('invalid_request', 'not_found'):
        return _blocked(order, auto_resp, steps, step_index, step, 'mapping', exc.message, label)
    return _blocked(order, auto_resp, steps, step_index, step, 'rejected', exc.message, label)


def _remote_outcome(remote):
    """'delivered' | 'refunded' | 'failed' | 'pending' según el pedido y su
    (única) línea. Se manda UNA línea por pedido: el estado de la línea manda."""
    order_status = str(remote.get('status') or '').strip().lower()
    lines = remote.get('items') if isinstance(remote.get('items'), list) else []
    line = lines[0] if lines and isinstance(lines[0], dict) else {}
    line_status = str(line.get('status') or '').strip().lower()

    if line_status == 'completed':
        return 'delivered'
    if line_status == 'refunded':
        return 'refunded'
    if line_status in ('failed', 'cancelled') or order_status in ('failed', 'cancelled'):
        return 'failed'
    if order_status == 'completed' and not line_status:
        return 'delivered'
    return 'pending'


def _poll_step(order, auto_resp, steps, step_index, step, force, label):
    """Consulta el pedido de Central One de este paso. Devuelve None si ya se
    entregó (se sigue con el siguiente paso) o el resultado que detiene la cola."""
    if step.get('awaiting_codes') and not step.get('codes_saved'):
        # Ya entregado: solo falta traer el código.
        return _collect_codes(order, auto_resp, steps, step_index, step, force, label)
    if not force and _is_future(step.get('next_check_at')):
        return _pending(step_index, 'Central One está procesando la recarga; se verifica sola.')

    now = datetime.utcnow()
    try:
        remote = fetch_order(step['remote_order_id'])
    except CentralOneUnknown as exc:
        step['next_check_at'] = _later(CO_UNKNOWN_RETRY_SECONDS)
        step['error'] = str(exc)
        step['pending_verification'] = True
        _persist(order, auto_resp, steps)
        return _pending(step_index, f'No se pudo consultar a Central One ({exc}). Se reintentará sola.')
    except CentralOneError as exc:
        # El pedido YA existe allá: aunque la consulta falle no se crea otro
        # ni se cambia de proveedor; solo se espera y se revisa de nuevo.
        wait = max(5, exc.retry_after or CO_POLL_SLOW_SECONDS) if exc.code == 'rate_limited' else CO_POLL_SLOW_SECONDS
        step['next_check_at'] = _later(wait)
        step['error'] = exc.message
        step['pending_verification'] = True
        _note(order, f'{label} No se pudo consultar el pedido en Central One ({exc.code}): {exc.message}')
        _persist(order, auto_resp, steps)
        return _pending(step_index, f'No se pudo consultar el pedido en Central One ({exc.message}). Se reintentará sola.')

    outcome = _remote_outcome(remote)
    reference = step.get('reference_code') or step['remote_order_id']
    step['remote_status'] = str(remote.get('status') or step.get('remote_status') or '')

    if outcome == 'refunded':
        # Central One devolvió el importe: lo más probable es que NO entregó
        # (los pedidos fallidos no se cobran), pero no se puede asegurar. Ni se
        # da por entregado, ni se reintenta, ni se pasa al respaldo: lo decide
        # el admin con el portal del proveedor a la vista.
        step['refunded'] = True
        _note(order, f'{label} Central One marcó el pedido {reference} como "refunded" (importe devuelto). '
                     'No se sabe con certeza si la recarga llegó: revísalo en el portal del proveedor antes de reintentar.')
        blocked = _blocked(order, auto_resp, steps, step_index, step, 'refunded',
                           f'el pedido {reference} terminó en "refunded" (importe devuelto)', label)
        blocked['definitive_rejection'] = False
        blocked['safe_to_switch'] = False
        return blocked

    if outcome == 'delivered':
        # (El paso no se da por completo aquí: si el producto entrega un CÓDIGO, hasta traerlo y guardarlo
        # la orden no se completa; el "success" se marca más abajo.)
        step.update({'error': '', 'blocked_cause': '', 'next_check_at': '', 'retry_after': ''})
        _note(order, f'{label} Pedido {reference} completado en Central One.')
        lines = remote.get('items') if isinstance(remote.get('items'), list) else []
        delivered_codes = int((lines[0] or {}).get('delivered_count') or 0) if lines and isinstance(lines[0], dict) else 0

        if delivered_codes or is_code_section(order):
            # Producto de CÓDIGO: la orden no se completa hasta traer el código.
            # (En la sección de códigos se intenta aunque Central One no cuente
            # ninguno, para no dar por entregada una orden sin código.)
            step.update({'awaiting_codes': True, 'codes_expected': delivered_codes, 'pending_verification': True})
            _persist(order, auto_resp, steps)
            return _collect_codes(order, auto_resp, steps, step_index, step, True, label)

        step.update({'success': True, 'pending_verification': False})
        _persist(order, auto_resp, steps)
        return None

    if outcome == 'failed':
        step['previous_orders'] = (step.get('previous_orders') or []) + [{
            'id': step['remote_order_id'],
            'reference_code': step.get('reference_code') or '',
            'status': step['remote_status'],
        }]
        step['remote_order_id'] = ''
        step['reference_code'] = ''
        return _blocked(order, auto_resp, steps, step_index, step, 'rejected',
                        f'el pedido {reference} terminó en "{step["remote_status"] or "fallido"}" y no se cobró', label)

    # Sigue en curso.
    step['pending_verification'] = True
    step['next_check_at'] = _next_check_at(step, now)
    step['error'] = ''
    sent = _parse_iso(step.get('sent_at'))
    stuck = bool(sent and now - sent > timedelta(hours=CO_STUCK_HOURS))
    if stuck:
        _note(order, f'{label} El pedido {reference} lleva más de {CO_STUCK_HOURS} h en proceso en Central One: revísalo con su soporte.')
    _persist(order, auto_resp, steps)
    return _pending(step_index, (f'El pedido {reference} lleva más de {CO_STUCK_HOURS} h en proceso en Central One: revísalo con su soporte.'
                                 if stuck else 'Central One está procesando la recarga; se verifica sola.'))


def _collect_codes(order, auto_resp, steps, step_index, step, force, label):
    """Trae de Central One el código de un pedido YA entregado y lo guarda en la
    orden. Devuelve None si el paso quedó completo o el resultado que detiene la
    cola. Aquí nunca se abre el respaldo: Central One sí entregó."""
    reference = step.get('reference_code') or step['remote_order_id']
    if not force and _is_future(step.get('retry_after')):
        return _pending(step_index, 'Central One entregó el pedido; se está trayendo el código.')

    try:
        remote = fetch_order_codes(step['remote_order_id'])
    except CentralOneUnknown as exc:
        step['retry_after'] = _later(CO_UNKNOWN_RETRY_SECONDS)
        step['error'] = str(exc)
        _persist(order, auto_resp, steps)
        return _pending(step_index, f'No se pudo pedir el código a Central One ({exc}). Se reintentará sola.')
    except CentralOneError as exc:
        wait = max(5, exc.retry_after or 30) if exc.code == 'rate_limited' else CO_RETRY_SECONDS
        step['retry_after'] = _later(wait)
        step['error'] = exc.message
        if exc.code == 'insufficient_scope':
            step['error'] = "La llave de Central One no tiene el permiso «ver códigos» (codes:read)."
            _note(order, (f'{label} Central One YA entregó el pedido {reference}, pero la llave no tiene el permiso «ver códigos» '
                          '(codes:read): el código no puede llegar al cliente. Crea una llave nueva con ese permiso, ponla en el '
                          '.env del servidor y la orden lo trae sola (o dale Aprobar).'))
        else:
            _note(order, f'{label} No se pudo pedir el código del pedido {reference} ({exc.code}): {exc.message}')
        _persist(order, auto_resp, steps)
        return _pending(step_index, step['error'] + ' La orden espera; no se recargará dos veces.')

    pins = [pin for line in (remote.get('items') or []) if isinstance(line, dict)
            for pin in (extract_pin(code) for code in (line.get('codes') or [])) if pin]

    if not pins:
        if int(step.get('codes_expected') or 0) > 0:
            # Central One cuenta un código pero todavía no lo devuelve.
            step['retry_after'] = _later(CO_UNKNOWN_RETRY_SECONDS)
            step['error'] = 'Central One aún no devuelve el código.'
            _persist(order, auto_resp, steps)
            return _pending(step_index, 'Central One entregó el pedido pero aún no devuelve el código; se reintentará sola.')
        return _blocked(order, auto_resp, steps, step_index, step, 'code_missing',
                        f'Central One entregó el pedido {reference} pero no devolvió ningún código', label, clear_key=False)

    _append_codes(order, pins)
    step.update({'codes_saved': True, 'awaiting_codes': False, 'error': '', 'retry_after': ''})
    if not is_code_section(order):
        # El código queda guardado (el admin lo ve), pero a este cliente no se
        # le muestra: no es la sección de códigos. Se deja para el admin.
        return _blocked(order, auto_resp, steps, step_index, step, 'code_hidden',
                        f'Central One entregó el código del pedido {reference}', label, clear_key=False)

    step.update({'success': True, 'pending_verification': False, 'blocked_cause': ''})
    _note(order, f'{label} Código del pedido {reference} recibido y guardado en la orden.')
    _persist(order, auto_resp, steps)
    return None


def attach_delivered_codes(order):
    """Trae los códigos de pedidos que Central One YA entregó pero cuyo código
    nunca llegó a la orden (órdenes de antes de que la tienda los pidiera, o
    que se completaron con una llave sin el permiso «ver códigos»). Si la orden
    ya está completada y es de la sección de códigos, avisa al cliente.

    Devuelve {'ok', 'message', 'count'}."""
    lock_key = f'order_delivery:{order.id}'
    holder = uuid4().hex
    if not acquire_lock(lock_key, 120, holder):
        return {'ok': False, 'count': 0, 'message': 'Esta orden se está procesando en este momento. Intenta de nuevo en unos segundos.'}
    try:
        db.session.refresh(order)
        try:
            state = json.loads(order.automation_response or '{}')
        except (TypeError, ValueError):
            state = {}
        steps = state.get('steps') if isinstance(state, dict) and state.get('source') == 'centralone_api' else None
        targets = [s for s in (steps or []) if isinstance(s, dict) and s.get('remote_order_id') and not s.get('codes_saved')]
        if not targets:
            return {'ok': False, 'count': 0, 'message': 'Esta orden no tiene pedidos de Central One con códigos pendientes de traer.'}

        pins = []
        for step in targets:
            try:
                remote = fetch_order_codes(step['remote_order_id'])
            except CentralOneError as exc:
                text = ('La llave de Central One no tiene el permiso «ver códigos» (codes:read). Crea una llave nueva con '
                        'ese permiso y ponla en el .env.' if exc.code == 'insufficient_scope' else exc.message)
                return {'ok': False, 'count': 0, 'message': f'No se pudo pedir el código: {text}'}
            except CentralOneUnknown as exc:
                return {'ok': False, 'count': 0, 'message': f'No se pudo pedir el código: {exc}'}
            found = [pin for line in (remote.get('items') or []) if isinstance(line, dict)
                     for pin in (extract_pin(code) for code in (line.get('codes') or [])) if pin]
            if found:
                pins.extend(found)
                step['codes_saved'] = True
                step['awaiting_codes'] = False
        if not pins:
            return {'ok': False, 'count': 0, 'message': 'Central One no tiene códigos para esta orden (fue una recarga directa).'}

        _append_codes(order, pins)
        _note(order, f'[Admin] Se trajeron {len(pins)} código(s) de Central One y se guardaron en la orden.')
        _persist(order, state, steps)
        notified = False
        if order.status == 'completed' and is_code_section(order):
            try:
                notify_order_completed(order, order.package, order.game, pin_code=order.pin_delivered)
                notified = True
            except Exception:
                pass
        return {
            'ok': True,
            'count': len(pins),
            'message': (f'Se trajeron {len(pins)} código(s) y quedaron en la orden'
                        + (' (se avisó al cliente por correo y notificación).' if notified else '.')),
        }
    finally:
        release_lock(lock_key, holder)


def _next_check_at(step, now):
    sent = _parse_iso(step.get('sent_at')) or now
    fast = (now - sent).total_seconds() < CO_POLL_FAST_WINDOW_SECONDS
    return (now + timedelta(seconds=CO_POLL_FAST_SECONDS if fast else CO_POLL_SLOW_SECONDS)).isoformat()
