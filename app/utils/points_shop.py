"""Tienda de puntos de las CUENTAS (`/tiendapuntos`).

La bolsa de puntos de la cuenta (utils/account_points.py) se gasta aquí en lo que el admin arma desde "Puntos por cuenta": paquetes de cualquier juego,
tarjetas de regalo, puntos de nivel, reputación y marcos, fondos y fotos premium para el perfil. Cada artículo tiene su precio (0 = gratis, pero igual se
canjea para que quede guardado), existencias, un límite por persona y fechas.

El canje sigue la misma regla de seguridad que el canje por ID (utils/points.py): se descuentan los puntos y se anota el canje JUNTOS y ANTES de entregar,
y si la entrega falla sin dejar nada hecho, se devuelven los puntos y la existencia. Todo con un candado por cuenta para que dos canjes a la vez no
pasen los dos con los mismos puntos.
"""
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import update

from ..models import PointsShopItem, PointsShopOrder, SHOP_KINDS, User, UserShopItem, db
from . import account_points
from .locks import acquire_lock, release_lock
from .player_field import clean_value, display_label, field_kind, stores_in_email, validate_value

# Cada cuánto se vuelve a poder canjear el mismo artículo (ventana corrida hacia atrás desde ahora).
LIMIT_PERIODS = {'always': None, 'day': 1, 'week': 7, 'month': 30}
LIMIT_PERIOD_LABELS = {'always': 'en total', 'day': 'por día', 'week': 'por semana', 'month': 'por mes'}

# Lo que se entrega como una orden de premio (con ID o con el correo de la cuenta).
DELIVERY_KINDS = ('package', 'card')
# Lo que se desbloquea para el perfil y se guarda en la colección de la cuenta.
COSMETIC_KINDS = ('frame_avatar', 'frame_banner', 'background', 'banner_photo', 'avatar_photo')
# Lo que se suma a la cuenta como bono.
BONUS_KINDS = ('xp', 'reputation')

MAX_COST = 1_000_000
MAX_BONUS = 1_000_000        # tope de puntos de nivel / reputación por artículo
MAX_ZONE_LENGTH = 20


class ShopError(ValueError):
    """Un motivo por el que no se pudo canjear, con un mensaje listo para mostrar y el código HTTP que corresponde."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _now():
    return datetime.utcnow()


# ─── Disponibilidad ─────────────────────────────────────────────────────────

def _is_card_game(game):
    return bool(game and game.category and (game.category.slug or '').lower() == 'tarjetas')


def purchases_by_user(user_id, item, now=None):
    """Cuántas veces canjeó esta cuenta el artículo dentro de su ventana de límite (los devueltos no cuentan)."""
    query = PointsShopOrder.query.filter_by(user_id=user_id, item_id=item.id, status='done')
    days = LIMIT_PERIODS.get(item.limit_period or 'always')
    if days:
        query = query.filter(PointsShopOrder.created_at >= (now or _now()) - timedelta(days=days))
    return query.count()


def owns(user_id, item):
    return bool(user_id) and UserShopItem.query.filter_by(user_id=user_id, item_id=item.id).first() is not None


def availability(item, user_id=None, now=None):
    """Estado de un artículo para una cuenta: {'state', 'reason', 'owned', 'stock_left', 'remaining'}.

    state: 'available' | 'owned' | 'sold_out' | 'limit_reached' | 'not_started' | 'ended' | 'unavailable'."""
    now = now or _now()
    owned = item.kind in COSMETIC_KINDS and owns(user_id, item)
    out = {'state': 'available', 'reason': '', 'owned': owned, 'stock_left': item.stock, 'remaining': None}

    def blocked(state, reason):
        out['state'], out['reason'] = state, reason
        return out

    if not item.is_active:
        return blocked('unavailable', 'Este artículo ya no está disponible.')
    if item.starts_at and item.starts_at > now:
        return blocked('not_started', 'Todavía no está disponible.')
    if item.ends_at and item.ends_at <= now:
        return blocked('ended', 'Esta oferta ya terminó.')
    if item.kind in DELIVERY_KINDS:
        package = item.package
        if not package or not package.is_active or not package.game or not package.game.is_active:
            return blocked('unavailable', 'Este premio no está disponible por ahora.')
        is_card = _is_card_game(package.game)
        if (item.kind == 'card') != is_card:
            return blocked('unavailable', 'Este premio no está disponible por ahora.')
        from .availability import package_is_out_of_stock
        if package_is_out_of_stock(package):
            return blocked('sold_out', 'Este premio está agotado por ahora.')
    if owned:
        return blocked('owned', 'Ya lo tienes en tu colección.')
    if item.stock is not None and item.stock <= 0:
        return blocked('sold_out', 'Se agotó.')
    if user_id and item.per_user_limit:
        used = purchases_by_user(user_id, item, now)
        out['remaining'] = max(0, item.per_user_limit - used)
        if used >= item.per_user_limit:
            period = LIMIT_PERIODS.get(item.limit_period or 'always')
            return blocked('limit_reached', 'Ya canjeaste este artículo el máximo de veces' + (' por ahora.' if period else '.'))
    elif item.per_user_limit:
        out['remaining'] = item.per_user_limit
    return out


def _item_image(item):
    """La imagen de la tarjeta (ruta dentro de uploads/). Los marcos, fotos y fondos muestran SU PROPIO archivo (así, si se mueve —GIF o WEBP animado—, se ve moviéndose
    en la tarjeta); un video de fondo no es imagen: la tarjeta lo dibuja como video (ver `asset_video`). Lo demás: la que subió el admin o la del paquete."""
    if item.kind in COSMETIC_KINDS and item.asset_path:
        from .shop_assets import asset_media_path, is_video
        return '' if is_video(item) else asset_media_path(item)
    if item.image:
        return item.image
    if item.kind in DELIVERY_KINDS and item.package and item.package.image:
        return item.package.image
    return ''


def item_dict(item, user_id=None, now=None):
    """Lo que el navegador necesita para pintar un artículo (nunca datos internos del admin)."""
    info = availability(item, user_id, now)
    data = {
        'id': item.id, 'kind': item.kind, 'kind_label': SHOP_KINDS.get(item.kind, item.kind),
        'name': item.name, 'description': item.description or '',
        'image': _item_image(item),
        'points_cost': int(item.points_cost or 0), 'is_free': int(item.points_cost or 0) <= 0,
        'state': info['state'], 'reason': info['reason'], 'owned': info['owned'],
        'stock_left': item.stock if item.stock is not None else None,
        'per_user_limit': item.per_user_limit, 'limit_label': LIMIT_PERIOD_LABELS.get(item.limit_period or 'always', ''),
        'remaining': info['remaining'], 'ends_at': item.ends_at.isoformat() + 'Z' if item.ends_at else None,
        'amount': item.amount,
        'needs_id': False,
    }
    if item.kind in COSMETIC_KINDS:
        # el archivo premium (para la vista previa del perfil): ruta dentro de uploads/ y si es un video (fondo)
        from .shop_assets import asset_media_path, is_video
        data['asset'] = asset_media_path(item)
        data['asset_video'] = bool(data['asset']) and is_video(item)
    if item.kind in DELIVERY_KINDS and item.package and item.package.game:
        game = item.package.game
        data['game_name'] = game.name
        data['game_id'] = game.id                # la tienda agrupa por juego (subpestañas bajo Recargas y Tarjetas)
        data['game_image'] = game.image or ''
        data['game_position'] = game.position or 0
        data['package_name'] = item.package.name
        if item.kind == 'package':
            data['needs_id'] = True
            data['id_label'] = display_label(game)
            data['id_kind'] = field_kind(game)
            data['requires_zone'] = bool(game.requires_zone_id)
            data['zone_label'] = game.zone_id_label or 'Zone ID'
        else:
            data['delivery'] = 'email'
    return data


def visible_items(kind=None, now=None):
    """Artículos que se muestran en la tienda: activos y dentro de sus fechas (los agotados se ven, apagados)."""
    now = now or _now()
    query = PointsShopItem.query.filter_by(is_active=True)
    if kind:
        query = query.filter_by(kind=kind)
    items = query.order_by(PointsShopItem.sort_order.asc(), PointsShopItem.points_cost.asc(), PointsShopItem.id.asc()).all()
    return [i for i in items if not (i.starts_at and i.starts_at > now) and not (i.ends_at and i.ends_at <= now)]


def catalog(user_id=None, kind=None):
    now = _now()
    out = []
    for item in visible_items(kind, now):
        data = item_dict(item, user_id, now)
        if data['state'] == 'unavailable':
            continue
        out.append(data)
    return out


# ─── Canje ──────────────────────────────────────────────────────────────────

def _clean_zone(game, zone_id):
    if not game.requires_zone_id:
        return None
    zone = ''.join(str(zone_id or '').split())
    if not zone:
        raise ShopError(f'Escribe tu {game.zone_id_label or "Zone ID"}.')
    if len(zone) > MAX_ZONE_LENGTH or not zone.replace('-', '').replace('_', '').isalnum():
        raise ShopError(f'Revisa tu {game.zone_id_label or "Zone ID"}: no parece válido.')
    return zone


def _prepare_package(item, user, player_id, zone_id):
    game = item.package.game
    kind = field_kind(game)
    value = clean_value(kind, player_id)
    if not value:
        raise ShopError(f'Escribe tu {display_label(game)} para saber dónde enviarte el premio.')
    error = validate_value(kind, value)
    if error:
        raise ShopError(error)
    return {'game': game, 'player_id': value, 'zone_id': _clean_zone(game, zone_id),
            'email': value if stores_in_email(game) else None}


def _prepare_card(item, user, player_id, zone_id):
    email = (getattr(user, 'email', '') or '').strip()
    if not email:
        raise ShopError('Tu cuenta no tiene un correo para enviarte la tarjeta.')
    return {'game': item.package.game, 'player_id': '', 'zone_id': None, 'email': email}


def _deliver_prize(item, user, order, inputs):
    from .order_processing import deliver_prize_to_player

    prize_order, approval = deliver_prize_to_player(
        inputs['game'], item.package, inputs['player_id'], zone_id=inputs['zone_id'],
        note=f'Tienda de puntos ({order.points_spent} pts) para @{user.username}. [TP#{order.id}]',
        reference_prefix='TIENDA', email=inputs['email'],
    )
    return prize_order, approval


def _refund(order_id, reason):
    """Devuelve los puntos y la existencia de un canje que no se pudo entregar. Hace commit."""
    order = db.session.get(PointsShopOrder, order_id)
    if order is None or order.status == 'refunded':
        return
    order.status = 'refunded'
    order.note = (reason or '')[:200]
    if order.points_spent > 0:
        account_points.credit(order.user_id, order.points_spent, kind='tienda', note=f'Devolución: {order.item_name}'[:200])
    if order.item_id:
        db.session.execute(
            update(PointsShopItem).where(PointsShopItem.id == order.item_id, PointsShopItem.stock.isnot(None))
            .values(stock=PointsShopItem.stock + 1)
        )
        if order.kind in COSMETIC_KINDS:     # lo desbloqueado se quita de su colección y de su perfil (si lo tenía puesto)
            UserShopItem.query.filter_by(user_id=order.user_id, item_id=order.item_id).delete(synchronize_session=False)
            from .profile_shop import clear_equipped
            clear_equipped(order.user_id, order.item_id, order.kind)
    db.session.commit()
    if order.kind in BONUS_KINDS:            # el nivel y la reputación se calculan desde los canjes "done": se recalcula sin lo devuelto
        _refresh_profile(order.user_id)


def _refresh_profile(user_id):
    """Recalcula el nivel, la reputación y los puestos de la cuenta. Si algo falla no se tumba el canje: el próximo recálculo general lo arregla."""
    from . import profile_social
    try:
        user = db.session.get(User, user_id)
        if user is not None:
            stat = profile_social.refresh_user(user)
            profile_social.recompute_ranks()
            return stat
    except Exception:
        db.session.rollback()
        current_app.logger.exception('[Tienda de puntos] no se pudo recalcular el perfil de la cuenta %s', user_id)
    return None


def buy(user, item_id, player_id='', zone_id=''):
    """Canjea un artículo con los puntos de la cuenta. Lanza ShopError con un mensaje listo para mostrar si algo no procede.
    Devuelve {'order_id', 'item_name', 'points_spent', 'balance', 'kind', ...detalles del canje}."""
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        raise ShopError('Elige un artículo.')
    item = db.session.get(PointsShopItem, item_id)
    if item is None or not item.is_active:
        raise ShopError('Ese artículo ya no está disponible.', 404)

    handlers = _handlers()
    if item.kind not in handlers:
        raise ShopError('Este artículo todavía no se puede canjear.')

    lock_key = f'points_shop:{user.id}'
    holder = f'shop-{datetime.utcnow().timestamp()}'
    if not acquire_lock(lock_key, 150, holder):     # más que lo que tarda la entrega más lenta
        raise ShopError('Tu canje anterior todavía se está procesando. Espera un momento.')
    try:
        db.session.expire_all()
        item = db.session.get(PointsShopItem, item_id)
        info = availability(item, user.id)
        if info['state'] != 'available':
            raise ShopError(info['reason'] or 'Este artículo no está disponible.')
        prepare, deliver = handlers[item.kind]
        inputs = prepare(item, user, player_id, zone_id)

        cost = int(item.points_cost or 0)
        if cost < 0 or cost > MAX_COST:
            raise ShopError('Este artículo no está disponible.')
        bonus_amount = int(item.amount or 0) if item.kind in BONUS_KINDS else 0
        if item.kind in BONUS_KINDS and not 0 < bonus_amount <= MAX_BONUS:
            raise ShopError('Este artículo no está disponible.')

        # Existencia y puntos se descuentan en la MISMA transacción: si no alcanzan los puntos, la existencia vuelve sola (rollback).
        if item.stock is not None:
            taken = db.session.execute(
                update(PointsShopItem).where(PointsShopItem.id == item.id, PointsShopItem.stock >= 1).values(stock=PointsShopItem.stock - 1)
            ).rowcount
            if taken != 1:
                db.session.rollback()
                raise ShopError('Se agotó.')
        game = inputs.get('game')
        if cost > 0:
            if not account_points.debit(user.id, cost, kind='tienda', note=f'Tienda: {item.name}'[:200],
                                        game_id=game.id if game else None, player_id=inputs.get('player_id') or None):
                db.session.rollback()
                raise ShopError(f'No tienes suficientes puntos. Cuesta {cost} y tienes {account_points.balance(user.id)}.')

        order = PointsShopOrder(
            user_id=user.id, item_id=item.id, item_name=item.name[:80], kind=item.kind, points_spent=cost, status='done',
            amount=bonus_amount, game_id=game.id if game else None, player_id=inputs.get('player_id') or None, zone_id=inputs.get('zone_id'),
        )
        db.session.add(order)
        if item.kind in COSMETIC_KINDS:
            # La colección se guarda en la MISMA transacción que el cobro: nunca queda cobrado sin tenerlo, ni al revés.
            db.session.add(UserShopItem(user_id=user.id, item_id=item.id, points_spent=cost))
        db.session.commit()
        order_id = order.id

        try:
            details = deliver(item, user, order, inputs) or {}
        except Exception as exc:
            db.session.rollback()
            current_app.logger.exception('[Tienda de puntos] falló la entrega del canje %s', order_id)
            # ¿Llegó a crearse la orden de premio antes de fallar? Entonces el canje sí cuenta (queda en Órdenes). Si no, no se entregó nada.
            from ..models import Order
            created = Order.query.filter(Order.payment_method == 'prize', Order.notes.like(f'%[TP#{order_id}]%')).first()
            if created is None:
                _refund(order_id, f'No se pudo entregar: {str(exc)[:120]}')
                raise ShopError('No se pudo entregar el premio. Tus puntos fueron devueltos, intenta de nuevo en un rato.', 500)
            details = {'prize_order': created, 'approval_ok': False}
            order = db.session.get(PointsShopOrder, order_id)
            order.order_id = created.id
            db.session.commit()
    finally:
        release_lock(lock_key, holder)

    result = {
        'order_id': order_id, 'kind': item.kind, 'item_name': item.name, 'points_spent': cost,
        'balance': account_points.balance(user.id),
    }
    prize_order = details.get('prize_order')
    if prize_order is not None:
        result['order_number'] = prize_order.order_number
        result['delivered'] = bool(details.get('approval_ok'))
    for key in ('detail', 'level', 'reputation'):
        if key in details:
            result[key] = details[key]
    return result


def _deliver_prize_kind(item, user, order, inputs):
    prize_order, approval = _deliver_prize(item, user, order, inputs)
    db.session.refresh(order)
    order.order_id = prize_order.id if prize_order else None
    db.session.commit()
    return {'prize_order': prize_order, 'approval_ok': bool(prize_order and approval and approval.get('ok'))}


def _prepare_nothing(item, user, player_id, zone_id):
    """Los artículos que no se mandan a ningún ID (nivel, reputación, cosas del perfil) no piden nada al cliente."""
    return {}


def _deliver_bonus(item, user, order, inputs):
    """Puntos de nivel o de reputación: el canje ya quedó guardado ("done" con su cantidad), solo falta recalcular el perfil para que se vea ya."""
    stat = _refresh_profile(user.id)
    if stat is None:
        raise RuntimeError('no se pudo recalcular el perfil')
    amount = int(order.amount or 0)
    if item.kind == 'xp':
        return {'detail': f'Sumaste {amount} puntos de nivel. Ahora estás en el nivel {stat.xp_level}.', 'level': stat.xp_level}
    return {'detail': f'Sumaste {amount} puntos de reputación. Ahora tienes {stat.reputation}.', 'reputation': stat.reputation}


COSMETIC_DETAIL = {
    'frame_avatar': 'El marco ya está en tu colección.', 'frame_banner': 'El marco ya está en tu colección.',
    'avatar_photo': 'La foto ya está en tu colección.', 'banner_photo': 'La foto ya está en tu colección.',
    'background': 'El fondo ya está en tu colección.',
}


def _deliver_cosmetic(item, user, order, inputs):
    """Un marco, fondo o foto premium: ya quedó en su colección junto con el cobro (ver buy). Se pone desde "Editar mi perfil"."""
    return {'detail': COSMETIC_DETAIL.get(item.kind, 'Ya está en tu colección.') + ' Para ponerlo, entra a Editar mi perfil y toca «Ver mis marcos / fotos / fondos».'}


def _handlers():
    """kind -> (preparar, entregar). Cada tipo de artículo sabe qué pide y cómo se entrega."""
    handlers = {
        'package': (_prepare_package, _deliver_prize_kind),
        'card': (_prepare_card, _deliver_prize_kind),
        'xp': (_prepare_nothing, _deliver_bonus),
        'reputation': (_prepare_nothing, _deliver_bonus),
    }
    for kind in COSMETIC_KINDS:
        handlers[kind] = (_prepare_nothing, _deliver_cosmetic)
    return handlers


# ─── Historial ──────────────────────────────────────────────────────────────

def history(user_id, limit=30):
    rows = (PointsShopOrder.query.filter_by(user_id=user_id)
            .order_by(PointsShopOrder.id.desc()).limit(limit).all())
    out = []
    for row in rows:
        out.append({
            'id': row.id, 'item_name': row.item_name, 'kind': row.kind, 'kind_label': SHOP_KINDS.get(row.kind, row.kind),
            'points_spent': row.points_spent, 'status': row.status,
            'order_number': row.order.order_number if row.order else '',
            'order_status': row.order.status if row.order else '',
            'created_at': row.created_at.isoformat() + 'Z' if row.created_at else None,
        })
    return out
