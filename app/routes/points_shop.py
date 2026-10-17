"""Tienda de puntos de las cuentas: la página `/tiendapuntos` y su mini-API JSON (la lógica vive en utils/points_shop.py).

Es una página propia (no una ventana de la portada) para que la portada no cargue nada de esto. Cualquiera puede VER el catálogo; solo una cuenta con
sesión puede canjear. Los puntos que gasta son los de su cuenta (los que ganó recargando con la sesión abierta, premios de ranking o los que el admin le dio).
"""
from flask import Blueprint, jsonify, make_response, render_template, request
from flask_login import current_user

from ..models import Game, PointsShopItem, SHOP_KINDS, db
from ..utils import account_points, points_shop
from ..utils.points import get_points_enabled_games, get_points_spin_cost, spend_points_and_spin
from ..utils.auth_accounts import is_account_user
from ..utils.locks import check_rate_limit, client_ip
from ..utils.points_shop import ShopError

points_shop_bp = Blueprint('points_shop_bp', __name__)

SHOP_ACTIONS_PER_MINUTE = 20

# Las pestañas de la tienda, en orden: (clave, nombre, tipos de artículo que muestra). Las tarjetas de regalo van junto a las recargas (cada juego es una
# subpestaña); lo del perfil se reparte en "Marcos" (de perfil / de banner), "Fondos" y "Fotos" (de perfil / de banner); "Ruletas" no son artículos: salen de
# los juegos que ya tienen ruleta en "Puntos por ID".
SHOP_TABS = (
    ('recargas', 'Recargas', ('package', 'card')),
    ('marcos', 'Marcos', ('frame_avatar', 'frame_banner')),
    ('fondos', 'Fondos', ('background',)),
    ('fotos', 'Fotos', ('avatar_photo', 'banner_photo')),
    ('ruletas', 'Ruletas', ()),
    ('perfil', 'Nivel y reputación', ('xp', 'reputation')),
)


def _account():
    return current_user if is_account_user(current_user if current_user.is_authenticated else None) else None


def _no_store(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


def _state_payload(user):
    """Todo lo que la página necesita para pintarse: quién es, su saldo, el catálogo y las pestañas que tienen artículos."""
    from .verify import verifiable_game_ids
    user_id = user.id if user else None
    items = points_shop.catalog(user_id)
    verifiable = verifiable_game_ids()
    for item in items:
        # Solo los juegos con verificador de ID (Free Fire, Blood Strike) piden confirmar el nombre del jugador antes de gastar los puntos.
        item['verifiable'] = bool(item.get('needs_id')) and item.get('game_id') in verifiable
    present = {item['kind'] for item in items}
    roulettes = _roulettes()
    tabs = [
        {'key': key, 'label': label, 'kinds': list(kinds)} for key, label, kinds in SHOP_TABS
        if (key == 'ruletas' and roulettes) or (present & set(kinds))
    ]
    return {
        'ok': True, 'logged_in': bool(user), 'username': user.username if user else '',
        'balance': account_points.balance(user_id) if user else 0,
        'items': items, 'tabs': tabs, 'roulettes': roulettes,
    }


def _roulettes():
    """Los juegos que tienen ruleta (los mismos de "Canjear puntos" por ID): se gira con los puntos de la cuenta, escribiendo el ID donde llega el premio."""
    out = []
    cost = get_points_spin_cost()
    for entry in get_points_enabled_games():
        game = db.session.get(Game, entry['game_id'])
        out.append({
            'game_id': entry['game_id'], 'game_name': entry['game_name'], 'prize_label': entry['prize_label'], 'spin_cost': cost,
            'game_image': (game.image or '') if game else '', 'game_position': (game.position or 0) if game else 0,
        })
    out.sort(key=lambda r: (r['game_position'], r['game_name'].lower()))
    return out


@points_shop_bp.route('/tiendapuntos')
def shop_page():
    user = _account()
    response = make_response(render_template(
        'tiendapuntos.html', boot=_state_payload(user), shop_kinds=SHOP_KINDS, login_next='/tiendapuntos',
    ))
    return _no_store(response)


@points_shop_bp.route('/api/tiendapuntos/items')
def shop_items():
    return _no_store(jsonify(_state_payload(_account())))


@points_shop_bp.route('/api/tiendapuntos/historial')
def shop_history():
    user = _account()
    if not user:
        return _no_store(jsonify({'ok': False, 'message': 'Inicia sesión con tu cuenta para ver tus canjes.'})), 401
    return _no_store(jsonify({'ok': True, 'orders': points_shop.history(user.id), 'balance': account_points.balance(user.id)}))


def _verify_nick(item, player_id):
    """Para los juegos con verificador de ID (Free Fire, Blood Strike): confirma que el ID existe y devuelve el nombre del jugador.
    (nick, mensaje_de_error, status). Para los demás juegos no hay a quién preguntar: se devuelve vacío."""
    from .verify import verifiable_game_ids, verify_player_nick
    package = item.package if item is not None else None
    if item is None or item.kind != 'package' or package is None or package.game_id not in verifiable_game_ids():
        return None, '', 200
    payload, status = verify_player_nick(player_id, str(package.game_id), mode='auto')
    if not payload.get('ok'):
        return None, payload.get('error') or 'No se pudo verificar tu ID.', status
    return payload.get('nick'), '', 200


@points_shop_bp.route('/api/tiendapuntos/verificar', methods=['POST'])
def shop_verify_id():
    """Muestra el nombre del jugador de ese ID ANTES de gastar los puntos, para que confirme que es el ID correcto."""
    user = _account()
    if not user:
        return _no_store(jsonify({'ok': False, 'message': 'Inicia sesión con tu cuenta para canjear.'})), 401
    if not check_rate_limit(f'shop_action:{client_ip()}', SHOP_ACTIONS_PER_MINUTE, 60):
        return _no_store(jsonify({'ok': False, 'message': 'Demasiados intentos seguidos. Espera un momento.'})), 429
    payload = request.get_json(silent=True) or {}
    item = db.session.get(PointsShopItem, int(payload['item_id'])) if str(payload.get('item_id') or '').isdigit() else None
    if item is None or not item.is_active:
        return _no_store(jsonify({'ok': False, 'message': 'Ese artículo ya no está disponible.'})), 404
    nick, error, status = _verify_nick(item, str(payload.get('player_id') or '').strip())
    if error:
        return _no_store(jsonify({'ok': False, 'message': error})), status
    return _no_store(jsonify({'ok': True, 'nick': nick}))


@points_shop_bp.route('/api/tiendapuntos/canjear', methods=['POST'])
def shop_redeem():
    user = _account()
    if not user:
        return _no_store(jsonify({'ok': False, 'message': 'Inicia sesión con tu cuenta para canjear tus puntos.'})), 401
    if not check_rate_limit(f'shop_action:{client_ip()}', SHOP_ACTIONS_PER_MINUTE, 60):
        return _no_store(jsonify({'ok': False, 'message': 'Demasiados intentos seguidos. Espera un momento.'})), 429
    payload = request.get_json(silent=True) or {}
    item_id = payload.get('item_id')
    player_id = str(payload.get('player_id') or '').strip()
    zone_id = str(payload.get('zone_id') or '').strip()

    # El ID tiene que existir de verdad en el juego (cuando hay verificador) ANTES de gastar los puntos: así nadie manda un premio a un ID inventado.
    item = db.session.get(PointsShopItem, int(item_id)) if str(item_id or '').isdigit() else None
    nick, error, status = _verify_nick(item, player_id)
    if error:
        return _no_store(jsonify({'ok': False, 'message': error})), status

    try:
        result = points_shop.buy(user, item_id, player_id=player_id, zone_id=zone_id)
    except ShopError as exc:
        db.session.rollback()
        return _no_store(jsonify({'ok': False, 'message': exc.message})), exc.status
    except Exception:
        db.session.rollback()
        return _no_store(jsonify({'ok': False, 'message': 'No se pudo procesar el canje. Intenta de nuevo.'})), 500
    if nick:
        result['player_nick'] = nick
    return _no_store(jsonify({'ok': True, 'result': result}))


# ─── Ruleta con los puntos de la cuenta ─────────────────────────────────────

def _roulette_game(game_id):
    """El juego si tiene ruleta (premio de puntos configurado); si no, None."""
    try:
        game_id = int(game_id)
    except (TypeError, ValueError):
        return None
    if not any(entry['game_id'] == game_id for entry in get_points_enabled_games()):
        return None
    return db.session.get(Game, game_id)


def _verify_roulette_id(game, player_id):
    """Para los juegos con verificador de ID (Free Fire, Blood Strike): el ID tiene que existir ANTES de gastar los puntos. (nick, mensaje_de_error, status)."""
    from .verify import verifiable_game_ids, verify_player_nick
    if game.id not in verifiable_game_ids():
        return None, '', 200
    payload, status = verify_player_nick(player_id, str(game.id), mode='auto')
    if not payload.get('ok'):
        return None, payload.get('error') or 'No se pudo verificar tu ID.', status
    return payload.get('nick'), '', 200


@points_shop_bp.route('/api/tiendapuntos/ruleta/saldo', methods=['POST'])
def roulette_balance():
    """Primer paso de la ruleta de la cuenta: comprueba el ID y devuelve lo mismo que "Consultar saldo" de los IDs, pero con los puntos de la CUENTA."""
    user = _account()
    if not user:
        return _no_store(jsonify({'ok': False, 'message': 'Inicia sesión con tu cuenta para girar con tus puntos.'})), 401
    if not check_rate_limit(f'shop_action:{client_ip()}', SHOP_ACTIONS_PER_MINUTE, 60):
        return _no_store(jsonify({'ok': False, 'message': 'Demasiados intentos seguidos. Espera un momento.'})), 429
    payload = request.get_json(silent=True) or {}
    game = _roulette_game(payload.get('game_id'))
    if game is None or not game.is_active:
        return _no_store(jsonify({'ok': False, 'message': 'Ese juego no tiene ruleta por ahora.'})), 404
    player_id = str(payload.get('player_id') or '').strip()
    if not player_id:
        return _no_store(jsonify({'ok': False, 'message': 'Ingresa el ID del juego.'})), 400
    nick, error, status = _verify_roulette_id(game, player_id)
    if error:
        return _no_store(jsonify({'ok': False, 'message': error})), status
    return _no_store(jsonify({
        'ok': True, 'game_id': game.id, 'game_name': game.name, 'player_id': player_id, 'player_nick': nick,
        'balance': account_points.balance(user.id), 'spin_cost': get_points_spin_cost(),
    }))


@points_shop_bp.route('/api/tiendapuntos/ruleta/girar', methods=['POST'])
def roulette_spin():
    """Un giro pagado con los puntos de la cuenta. Cuenta en los MISMOS giros que los de los IDs (mismo contador, mismo premio, misma entrega al ID)."""
    user = _account()
    if not user:
        return _no_store(jsonify({'ok': False, 'message': 'Inicia sesión con tu cuenta para girar con tus puntos.'})), 401
    if not check_rate_limit(f'shop_action:{client_ip()}', SHOP_ACTIONS_PER_MINUTE, 60):
        return _no_store(jsonify({'ok': False, 'message': 'Demasiados intentos seguidos. Espera un momento.'})), 429
    payload = request.get_json(silent=True) or {}
    game = _roulette_game(payload.get('game_id'))
    if game is None or not game.is_active:
        return _no_store(jsonify({'ok': False, 'message': 'Ese juego no tiene ruleta por ahora.'})), 404
    player_id = str(payload.get('player_id') or '').strip()
    if not player_id:
        return _no_store(jsonify({'ok': False, 'message': 'Ingresa el ID del juego para poder girar.'})), 400
    # el ID tiene que existir de verdad (cuando hay verificador) ANTES de gastar puntos: así nadie manda un premio a un ID inventado
    _nick, error, status = _verify_roulette_id(game, player_id)
    if error:
        return _no_store(jsonify({'ok': False, 'message': error})), status
    try:
        result = spend_points_and_spin(game.id, player_id, account_user=user)
    except ValueError as exc:
        db.session.rollback()
        return _no_store(jsonify({'ok': False, 'message': str(exc)})), 400
    except Exception:
        db.session.rollback()
        return _no_store(jsonify({'ok': False, 'message': 'No se pudo procesar el giro. Tus puntos no se descontaron: intenta de nuevo.'})), 500
    return _no_store(jsonify({'ok': True, 'result': result}))


# ─── Vista previa del perfil (marcos, fotos y fondos) ───────────────────────

@points_shop_bp.route('/api/tiendapuntos/vista-previa')
def profile_preview():
    """El perfil de la cuenta, tal cual lo ve en el editor (la misma tarjeta), para probarle encima un marco, foto o fondo antes de canjearlo. Solo con cuenta."""
    user = _account()
    if not user:
        return _no_store(jsonify({'ok': False, 'message': 'Inicia sesión con tu cuenta para ver cómo te queda en tu perfil.'})), 401
    from ..utils.profiles import get_profile
    from .profiles import build_view
    view = build_view(user, get_profile(user), owner_view=True, preview=True)
    html = render_template('tiendapuntos_preview.html', v=view, scope_class='pf-scope-stage', preview=True)
    return _no_store(jsonify({'ok': True, 'html': html}))
