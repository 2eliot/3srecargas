"""Costo automático de los paquetes de Free Fire, Mobile Legends y Blood Strike.

El costo de un paquete (`packages.cost_usd`) es lo que le cuesta a la tienda conseguirlo, y con él se calcula la ganancia en
Estadísticas. Antes era un número escrito a mano que se quedaba viejo cuando el proveedor subía o bajaba sus precios. Aquí se
calcula solo: se toma el proveedor PRINCIPAL del paquete (el del plan de entrega; sin plan, Revendedores), se mira su mapeo y se
SUMA lo que vale cada paso del mapeo en el catálogo de ese proveedor, por su cantidad.

Para que la ganancia de lo que ya se vendió no cambie sola cuando cambie el costo, justo antes de cambiarlo se le conserva el
costo viejo a cada venta anterior del paquete (`orders.cost_usd`). Las estadísticas usan el costo de la venta si lo tiene y el
del paquete si no.

El precio de VENTA nunca se toca aquí.
"""
import json
import logging
import threading
import time
from decimal import Decimal, InvalidOperation

import requests

from ..models import (
    CentralOneItemMapping, Game, Order, Package, PackageCostChange, PackageDeliveryPlan,
    RevendedoresCatalogItem, RevendedoresItemMapping, db,
)

log = logging.getLogger(__name__)

# Juegos a los que se les calcula el costo solo (nombre en minúsculas). Para sumar otro juego basta con agregarlo aquí.
AUTO_COST_GAMES = ('free fire', 'mobile legends', 'blood strike')
SOLD_STATUSES = ('approved', 'completed')
PLACES = Decimal('0.0001')
# Cada cuánto se le pregunta el catálogo a los proveedores (el lock de la base lo comparten los 3 workers).
REFRESH_EVERY_SECONDS = 30 * 60
_LOCK_KEY = 'package_costs_refresh'
# Si el costo nuevo sale más de 3 veces el actual casi seguro es un catálogo mal leído: no se aplica solo y queda para revisar.
# Las bajadas sí se aceptan siempre.
MAX_JUMP_RATIO = Decimal('3')

_last_attempt = 0.0
_attempt_guard = threading.Lock()


def _dec(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number > 0 else None


def auto_cost_game_ids():
    return [g.id for g in Game.query.all() if (g.name or '').strip().lower() in AUTO_COST_GAMES]


# ─── Precios del catálogo de cada proveedor ──────────────────────────────────

def _revendedores_item_price(item):
    try:
        raw = json.loads(item.raw_json or '{}')
    except (TypeError, ValueError):
        return None
    return _dec(raw.get('price') if isinstance(raw, dict) else None)


def _fetch_revendedores_prices():
    """{(producto, paquete): (precio, datos del paquete)} del catálogo en vivo de Revendedores, o None si no se pudo leer."""
    from .order_processing import get_revendedores_env
    base_url, api_key, catalog_path, _ = get_revendedores_env()
    if not base_url or not api_key:
        return None
    try:
        resp = requests.get(f'{base_url}{catalog_path}', headers={'X-API-Key': api_key}, timeout=30)
        if not resp.ok:
            return None
        payload = resp.json()
    except Exception:
        return None
    games = (payload.get('games') or payload.get('products') or []) if isinstance(payload, dict) else payload
    prices = {}
    for game in games or []:
        if not isinstance(game, dict):
            continue
        game_id = game.get('game_id') if game.get('game_id') is not None else game.get('id')
        for pkg in game.get('packages') or game.get('paquetes') or []:
            if not isinstance(pkg, dict):
                continue
            pkg_id = pkg.get('package_id') if pkg.get('package_id') is not None else pkg.get('id')
            price = _dec(pkg.get('price') if pkg.get('price') is not None else pkg.get('precio'))
            try:
                key = (int(game_id), int(pkg_id))
            except (TypeError, ValueError):
                continue
            if price is not None:
                prices[key] = (price, pkg)
    return prices or None


def _refresh_revendedores_catalog():
    prices = _fetch_revendedores_prices()
    if prices is None:
        return False
    changed = 0
    for row in RevendedoresCatalogItem.query.all():
        entry = prices.get((row.remote_product_id, row.remote_package_id))
        if not entry:
            continue
        price, pkg = entry
        if _revendedores_item_price(row) == price:
            continue
        try:
            raw = json.loads(row.raw_json or '{}')
        except (TypeError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw.update(pkg)
        row.raw_json = json.dumps(raw, ensure_ascii=False)
        changed += 1
    db.session.commit()
    log.info('[PackageCosts] Revendedores: %d precios actualizados', changed)
    return True


def _refresh_centralone_catalog():
    from .central_one import fetch_catalog, normalize_catalog_item
    from ..models import CentralOneCatalogItem
    try:
        items = fetch_catalog()
    except Exception:
        return False
    normalized = {n['product_id']: n for n in (normalize_catalog_item(raw) for raw in items) if n}
    if not normalized:
        return False
    changed = 0
    for row in CentralOneCatalogItem.query.all():
        ent = normalized.get(row.product_id)
        if not ent or ent['price'] is None:
            continue
        if row.price is None or Decimal(str(row.price)) != ent['price']:
            row.price = ent['price']
            changed += 1
        row.price_updated_at = ent['price_updated_at']
    db.session.commit()
    log.info('[PackageCosts] Central One: %d precios actualizados', changed)
    return True


def refresh_provider_prices():
    """Vuelve a leer los precios de los dos proveedores (solo el precio: no activa ni desactiva nada del catálogo).
    Devuelve {'revendedores': bool, 'centralone': bool} según si se pudo leer."""
    result = {}
    for name, fn in (('revendedores', _refresh_revendedores_catalog), ('centralone', _refresh_centralone_catalog)):
        try:
            result[name] = bool(fn())
        except Exception:
            db.session.rollback()
            log.exception('[PackageCosts] No se pudieron leer los precios de %s', name)
            result[name] = False
    return result


# ─── Costo de un paquete ─────────────────────────────────────────────────────

def primary_provider(package_id):
    plan = PackageDeliveryPlan.query.filter_by(store_package_id=package_id).first()
    return plan.primary_provider if plan and plan.primary_provider else 'revendedores'


def compute_package_cost(package):
    """(costo, proveedor, detalle). El costo es None si no se puede calcular exacto (sin mapeo o a un paso le falta el precio)."""
    provider = primary_provider(package.id)
    parts = []
    total = Decimal('0')

    if provider == 'centralone':
        mapping = CentralOneItemMapping.query.filter_by(store_package_id=package.id, active=True).first()
        rows = sorted(mapping.items, key=lambda r: r.sort_order or 0) if mapping and mapping.items else []
        entries = [(row.catalog_item, max(1, int(row.quantity or 1))) for row in rows]

        def price_of(item):
            return _dec(item.price)

        def name_of(item):
            return item.name or ''
    else:
        provider = 'revendedores'
        mapping = RevendedoresItemMapping.query.filter_by(store_package_id=package.id, active=True).first()
        entries = []
        if mapping:
            rows = sorted(mapping.items, key=lambda r: r.sort_order or 0) if mapping.items else []
            if rows:
                entries = [(row.catalog_item, max(1, int(row.quantity or 1))) for row in rows]
            else:
                # Mapeo viejo, de antes del editor con cantidades: solo tiene las 2 casillas fijas.
                entries = [(item, 1) for item in (mapping.catalog_item, mapping.catalog_item_2) if item]
        price_of = _revendedores_item_price

        def name_of(item):
            return item.remote_package_name or ''

    if not entries:
        return None, provider, 'sin mapeo en el proveedor principal'
    for item, qty in entries:
        if item is None:
            return None, provider, 'un paso del mapeo ya no existe en el catálogo'
        price = price_of(item)
        if price is None:
            return None, provider, f'sin precio para "{name_of(item)[:40]}"'
        total += price * qty
        parts.append(f'{qty} x {name_of(item)[:30]} ({price.normalize():f})')
    return total.quantize(PLACES), provider, ' + '.join(parts)[:290]


def refresh_package_costs(fetch=True, package_ids=None):
    """Pone al día el costo de los paquetes de los juegos de AUTO_COST_GAMES (o solo de `package_ids`).

    fetch=True vuelve a leer antes los precios de los proveedores. Si un proveedor no responde se sigue con el precio que ya
    estaba guardado: nunca se borra ni se pone un costo a ciegas. Devuelve un resumen con lo que cambió."""
    provider_ok = refresh_provider_prices() if fetch else {}

    game_ids = auto_cost_game_ids()
    summary = {'changed': [], 'unchanged': 0, 'skipped': [], 'providers': provider_ok}
    if not game_ids:
        return summary

    query = Package.query.filter(Package.game_id.in_(game_ids))
    if package_ids is not None:
        query = query.filter(Package.id.in_(list(package_ids)))
    for package in query.order_by(Package.game_id, Package.id).all():
        try:
            cost, provider, detail = compute_package_cost(package)
            if cost is None:
                summary['skipped'].append({'package_id': package.id, 'name': package.name, 'reason': detail})
                continue
            old = _dec(package.cost_usd) if package.cost_usd is not None else None
            if old is not None and old.quantize(PLACES) == cost:
                summary['unchanged'] += 1
                continue
            if old is not None and cost > old * MAX_JUMP_RATIO:
                summary['skipped'].append({
                    'package_id': package.id, 'name': package.name,
                    'reason': f'el costo nuevo ({cost}) es demasiado distinto al actual ({old}); se deja para revisar a mano',
                })
                continue

            # Lo ya vendido conserva el costo de antes: la ganancia de esos días no cambia sola. Si el paquete no tenía costo, las
            # estadísticas ya contaban esas ventas con costo 0, y así se quedan.
            frozen = Order.query.filter(
                Order.package_id == package.id,
                Order.status.in_(SOLD_STATUSES),
                Order.cost_usd.is_(None),
            ).update({'cost_usd': (old or Decimal('0')).quantize(PLACES)}, synchronize_session=False)
            package.cost_usd = cost
            db.session.add(PackageCostChange(
                package_id=package.id, old_cost=old, new_cost=cost, provider=provider,
                detail=detail, frozen_orders=int(frozen or 0),
            ))
            db.session.commit()
            summary['changed'].append({
                'package_id': package.id, 'name': package.name,
                'old': str(old) if old is not None else None, 'new': str(cost), 'provider': provider,
            })
        except Exception:
            db.session.rollback()
            log.exception('[PackageCosts] No se pudo actualizar el costo del paquete %s', package.id)
            summary['skipped'].append({'package_id': package.id, 'name': package.name, 'reason': 'error interno'})
    return summary


def run_scheduled_refresh():
    """Lo llama el proceso de fondo en cada vuelta: solo trabaja una vez cada REFRESH_EVERY_SECONDS entre todos los workers."""
    global _last_attempt
    with _attempt_guard:
        now = time.monotonic()
        if now - _last_attempt < 60:
            return None
        _last_attempt = now

    from .locks import acquire_lock
    holder = f'{threading.get_ident()}:{id(_attempt_guard)}'
    # El lock NO se libera: su vencimiento es lo que espacia las revisiones.
    if not acquire_lock(_LOCK_KEY, REFRESH_EVERY_SECONDS, holder):
        return None
    return refresh_package_costs(fetch=True)


def sold_order_cost(order):
    """Costo a usar para la ganancia de una venta: el que se le conservó, o el del paquete si no tiene."""
    if order.cost_usd is not None:
        return order.cost_usd
    package = getattr(order, 'package', None)
    return getattr(package, 'cost_usd', None) if package else None
