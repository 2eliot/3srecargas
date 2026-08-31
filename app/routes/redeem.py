"""Canje público de códigos de regalo (/redimir).

Dos pasos: primero el código, y solo cuando ese código resulta válido se
pide el ID del jugador. La entrega es automática, por el mismo camino que
una compra normal.
"""

from flask import Blueprint, current_app, jsonify, render_template, request

from ..models import Game, Setting
from ..utils.gift_codes import (
    clear_attempts,
    describe_code_problem,
    find_code,
    format_code,
    get_redemption_history,
    is_rate_limited,
    normalize_code,
    redeem_code,
    register_failed_attempt,
    serialize_redemption,
)

redeem_bp = Blueprint('redeem_bp', __name__)

RATE_LIMIT_MESSAGE = (
    'Demasiados intentos seguidos. Espera unos minutos antes de volver a probar.'
)


def _client_ip():
    """IP real del cliente. La app corre detrás de nginx, así que la
    cabecera del proxy es la que tiene la IP de verdad."""
    forwarded = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    return forwarded or (request.remote_addr or '')


def _setting(key, default=''):
    row = Setting.query.filter_by(key=key).first()
    return ((row.value if row else '') or default).strip()


def _game_payload(game):
    if not game:
        return None
    return {
        'id': game.id,
        'name': game.name,
        'requires_zone_id': bool(game.requires_zone_id),
        'player_id_label': game.player_id_label or 'Player ID',
        'zone_id_label': game.zone_id_label or 'Zone ID',
        'player_id_input_type': (game.player_id_input_type or 'numeric'),
        'category_slug': (game.category.slug if game.category else ''),
    }


def _verify_payload(game):
    """Misma configuración de verificación que usa la tienda (heredada de
    Inefablestore): Free Fire contra /store/player/verify y Blood Strike
    contra su ruta hermana. Aquí solo se dice si aplica y a qué endpoint;
    el front hace el resto igual que en el checkout."""
    disabled = {'enabled': False, 'endpoint': '', 'numeric_only': False}
    if not game:
        return disabled

    slug = (game.category.slug if game.category else '') or ''
    input_type = (game.player_id_input_type or 'numeric').lower()
    numeric_only = input_type == 'numeric' and slug.lower() not in ('wallet', 'tarjetas')

    ff_game_id = _setting('active_login_game_id')
    bs_game_id = _setting('bs_package_id')
    is_ff = bool(ff_game_id) and ff_game_id == str(game.id)
    is_bs = bool(bs_game_id) and bs_game_id == str(game.id)
    scrape_enabled = bool(current_app.config.get('SCRAPE_ENABLED', True))

    if not (scrape_enabled and numeric_only and (is_ff or is_bs)):
        return disabled
    return {
        'enabled': True,
        'endpoint': '/store/player/verify/bloodstrike' if is_bs else '/store/player/verify',
        'numeric_only': True,
    }


@redeem_bp.route('/redimir')
def redeem_page():
    return render_template('redeem.html')


@redeem_bp.route('/redimir/validar', methods=['POST'])
def validate():
    """Paso 1: comprueba el código y devuelve qué premio trae.

    No entrega nada todavía ni marca el código: solo habilita el paso 2.
    """
    ip = _client_ip()
    if is_rate_limited(ip):
        return jsonify({'ok': False, 'message': RATE_LIMIT_MESSAGE}), 429

    data = request.get_json(silent=True) or request.form
    code = normalize_code(data.get('code'))
    if not code:
        return jsonify({'ok': False, 'message': 'Escribe tu código.'}), 400

    gift = find_code(code)
    problema = describe_code_problem(gift)
    if problema:
        register_failed_attempt(ip)
        return jsonify({'ok': False, 'message': problema}), 404

    package = gift.package
    game = package.game if package else None
    if not game or not game.is_active:
        return jsonify({
            'ok': False,
            'message': 'El premio de ese código ya no está disponible. Escríbenos por soporte.',
        }), 409

    # Código bueno: se limpia el contador para no castigar a quien se
    # equivocó un par de veces antes de dar con el correcto.
    clear_attempts(ip)

    return jsonify({
        'ok': True,
        'code': format_code(gift.code),
        'prize': {
            'game': game.name,
            'package': package.name,
            'image': package.image or None,
        },
        'game': _game_payload(game),
        'verify': _verify_payload(game),
    })


@redeem_bp.route('/redimir/canjear', methods=['POST'])
def redeem():
    """Paso 2: con el ID del jugador, canjea y entrega."""
    ip = _client_ip()
    if is_rate_limited(ip):
        return jsonify({'ok': False, 'message': RATE_LIMIT_MESSAGE}), 429

    data = request.get_json(silent=True) or request.form
    code = normalize_code(data.get('code'))
    player_id = (data.get('player_id') or '').strip()
    zone_id = (data.get('zone_id') or '').strip()
    nickname = (data.get('nickname') or '').strip()

    if not code:
        return jsonify({'ok': False, 'message': 'Falta el código.'}), 400
    if not player_id:
        return jsonify({'ok': False, 'message': 'Escribe tu ID de jugador.'}), 400
    if len(player_id) > 100:
        return jsonify({'ok': False, 'message': 'Ese ID no es válido.'}), 400

    gift = find_code(code)
    problema = describe_code_problem(gift)
    if problema:
        register_failed_attempt(ip)
        return jsonify({'ok': False, 'message': problema}), 404

    game = gift.package.game if gift.package else None
    if game and game.requires_zone_id and not zone_id:
        return jsonify({
            'ok': False,
            'message': f'Escribe tu {game.zone_id_label or "Zone ID"}.',
        }), 400

    ok, mensaje, order = redeem_code(
        gift, player_id, zone_id=zone_id, nickname=nickname
    )
    if not ok:
        return jsonify({'ok': False, 'message': mensaje}), 409

    clear_attempts(ip)
    return jsonify({
        'ok': True,
        'message': mensaje,
        'order_number': order.order_number if order else '',
        'delivered': bool(order and order.status in ('approved', 'completed')),
        'prize': {
            'game': game.name if game else '',
            'package': gift.package.name if gift.package else '',
        },
    })


@redeem_bp.route('/redimir/historial', methods=['POST'])
def history():
    """Canjes hechos con un ID. Va aparte y detrás de un enlace discreto:
    no tiene sentido tenerlo compitiendo con el formulario principal."""
    data = request.get_json(silent=True) or request.form
    player_id = (data.get('player_id') or '').strip()
    if not player_id:
        return jsonify({'ok': False, 'message': 'Escribe tu ID para buscar.'}), 400
    if len(player_id) > 100:
        return jsonify({'ok': False, 'message': 'Ese ID no es válido.'}), 400

    # Se traen holgados: el historial se pagina en el front, así que 20
    # se quedaba corto en cuanto alguien acumula canjes.
    canjes = get_redemption_history(player_id, limit=200)
    return jsonify({
        'ok': True,
        'player_id': player_id,
        'items': [serialize_redemption(g) for g in canjes],
    })
