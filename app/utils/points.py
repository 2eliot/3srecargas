"""Sistema de puntos por recarga.

Cada recarga POR ID (nunca códigos ni wallet) otorga puntos proporcionales
al monto pagado, guardados por (juego, player_id). El cliente los canjea
en una ruleta tipo Counter-Strike: cada giro cuesta una cantidad fija de
puntos y, cada cierta cantidad de giros (de todos los clientes juntos, por
juego), se entrega el premio real configurado — reusando el mismo mecanismo
de entrega automática que el resto de la tienda.
"""

from datetime import datetime

from ..models import Game, MiniGameCounter, Package, PlayerPoints, PointsPrizeMapping, PointsSpinLog, Setting, db

DEFAULT_POINTS_PER_DOLLAR = 10
DEFAULT_POINTS_SPIN_COST = 5
DEFAULT_POINTS_WIN_INTERVAL = 20


def _get_setting_value(key):
    setting = Setting.query.filter_by(key=key).first()
    return setting.value if setting and setting.value else ''


def get_points_per_dollar_rate():
    raw = _get_setting_value('points_per_dollar')
    try:
        value = float(raw)
        return value if value > 0 else DEFAULT_POINTS_PER_DOLLAR
    except (TypeError, ValueError):
        return DEFAULT_POINTS_PER_DOLLAR


def get_points_spin_cost():
    raw = _get_setting_value('points_spin_cost')
    try:
        value = int(float(raw))
        return value if value > 0 else DEFAULT_POINTS_SPIN_COST
    except (TypeError, ValueError):
        return DEFAULT_POINTS_SPIN_COST


def get_points_win_interval():
    raw = _get_setting_value('points_win_every_n_spins')
    try:
        value = int(float(raw))
        return value if value > 0 else DEFAULT_POINTS_WIN_INTERVAL
    except (TypeError, ValueError):
        return DEFAULT_POINTS_WIN_INTERVAL


def get_package_fixed_points(package):
    """Puntos clavados a mano para el paquete, o None si usa el rate global.

    0 es un valor válido y significativo: es como se apaga el premio de un
    paquete puntual sin tener que tocar el rate de toda la tienda."""
    if package is None:
        return None
    raw = getattr(package, 'points_reward', None)
    if raw is None or raw == '':
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def calculate_points_for_purchase(amount, package=None):
    """Puntos que otorga una compra.

    Si el paquete tiene su cifra fija, esa manda y no depende del monto: así
    el cliente recibe exactamente los puntos que la tarjeta le prometió,
    aunque haya usado un descuento o un método de pago con precio distinto.
    Si no, se calcula con el rate global sobre lo que efectivamente pagó."""
    fixed = get_package_fixed_points(package)
    if fixed is not None:
        return fixed
    return int(round(float(amount or 0) * get_points_per_dollar_rate()))


def package_points_preview(package):
    """Los puntos que se le anuncian al cliente en la tarjeta del paquete.

    Para el fallback por rate usa el precio base del paquete, que es el mismo
    que la tarjeta muestra convertido a Bs."""
    if package is None:
        return 0
    return calculate_points_for_purchase(package.price, package)


def order_qualifies_for_points(order):
    """Solo recargas por ID de juego (no códigos, no wallet) cuentan para
    puntos, y solo una vez que el pago quedó verificado/completado."""
    if not order:
        return False
    if order.status == 'rejected':
        return False
    if not (order.status == 'completed' or order.payment_verified_at):
        return False
    if not (order.player_id or '').strip():
        return False
    category_slug = (order.game.category.slug if order.game and order.game.category else '').lower()
    if category_slug in ('tarjetas', 'wallet'):
        return False
    return True


def award_points_for_order(order):
    """Acredita los puntos de esta orden al saldo (juego, player_id). Es
    idempotente: si ya se acreditaron para esta orden, no hace nada."""
    if not order or bool(order.points_awarded):
        return None
    if not order_qualifies_for_points(order):
        return None

    points = calculate_points_for_purchase(order.amount, order.package)
    order.points_awarded = True
    if points <= 0:
        return None

    record = PlayerPoints.query.filter_by(game_id=order.game_id, player_id=order.player_id).first()
    if not record:
        record = PlayerPoints(game_id=order.game_id, player_id=order.player_id, points_balance=0)
        db.session.add(record)
        db.session.flush()

    record.points_balance = int(record.points_balance or 0) + points
    record.updated_at = datetime.utcnow()
    return record


def get_points_enabled_games():
    """Juegos que sí tienen un premio de puntos configurado (los únicos
    que el cliente puede elegir en 'Canjear puntos')."""
    mappings = (
        PointsPrizeMapping.query
        .filter_by(is_active=True)
        .join(Game, Game.id == PointsPrizeMapping.game_id)
        .filter(Game.is_active.is_(True))
        .all()
    )
    games = []
    for mapping in mappings:
        if not mapping.game or not mapping.package or not mapping.package.is_active:
            continue
        games.append({
            'game_id': mapping.game.id,
            'game_name': mapping.game.name,
            'prize_label': mapping.package.name,
        })
    return games


def get_player_points_balance(game_id, player_id):
    record = PlayerPoints.query.filter_by(game_id=game_id, player_id=player_id).first()
    return int(record.points_balance) if record else 0


def spend_points_and_spin(game_id, player_id):
    """Gasta los puntos de un giro y resuelve el resultado. Lanza
    ValueError con un mensaje listo para mostrar si algo no procede."""
    game_id = int(game_id)
    player_id = str(player_id or '').strip()
    if not player_id:
        raise ValueError('Ingresa el ID del juego para poder girar.')

    mapping = PointsPrizeMapping.query.filter_by(game_id=game_id, is_active=True).first()
    if not mapping or not mapping.package or not mapping.package.is_active:
        raise ValueError('Este juego no tiene un premio de puntos configurado en este momento.')

    cost = get_points_spin_cost()
    record = PlayerPoints.query.filter_by(game_id=game_id, player_id=player_id).first()
    balance = int(record.points_balance) if record else 0
    if balance < cost:
        raise ValueError(f'No tienes suficientes puntos. Cada giro cuesta {cost} puntos y tienes {balance}.')

    record.points_balance = balance - cost
    record.updated_at = datetime.utcnow()

    counter_key = f'points_{game_id}'
    counter = MiniGameCounter.query.filter_by(game_key=counter_key).first()
    if not counter:
        counter = MiniGameCounter(game_key=counter_key, play_count=0, last_position=0)
        db.session.add(counter)
        db.session.flush()
    counter.play_count = int(counter.play_count or 0) + 1
    counter.last_position = counter.play_count
    counter.updated_at = datetime.utcnow()

    win_interval = get_points_win_interval()
    is_win = counter.play_count % win_interval == 0

    prize_order = None
    reward_label = 'Fallaste'
    if is_win:
        from .order_processing import deliver_prize_to_player

        game = Game.query.get(game_id)
        prize_order, approval = deliver_prize_to_player(
            game, mapping.package, player_id,
            note=f'Premio canjeado con puntos ({cost} pts, giro #{counter.play_count}).',
            reference_prefix='PUNTOS',
        )
        reward_label = mapping.package.name
        if prize_order and approval and not approval.get('ok'):
            reward_label = f'{reward_label} (pendiente de entrega)'

    log = PointsSpinLog(
        game_id=game_id,
        player_id=player_id,
        points_spent=cost,
        won=is_win,
        reward_label=reward_label,
        prize_order_id=prize_order.id if prize_order else None,
    )
    db.session.add(log)
    db.session.commit()

    return {
        'won': is_win,
        'reward_label': reward_label if is_win else 'Fallaste',
        'prize_label': mapping.package.name,
        'points_spent': cost,
        'points_balance': record.points_balance,
    }
