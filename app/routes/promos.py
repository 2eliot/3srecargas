"""Rutas públicas de las 3 promociones: Recarga Acumulada, Sorteo Diario y
Adivina el Número. Cada una tiene una página (elige juego si hay más de
uno activo) y un mini-API JSON que la página consume con fetch()."""
from flask import Blueprint, jsonify, render_template, request, url_for

from ..utils.locks import check_rate_limit
from ..utils.promos import (
    get_accumulated_enabled_games, get_accumulated_progress_state,
    get_guess_enabled_games, get_guess_public_state, submit_guess,
    HORDE_BANNED_MESSAGE, HordeNotEnoughPoints, get_horde_config, get_horde_enabled_games, is_horde_banned, get_horde_public_state, start_horde_run, finish_horde_run,
    get_raffle_enabled_games, get_raffle_public_state, get_raffle_replay_state,
    get_raffle_show_state, register_raffle_entry,
    run_daily_raffle_draws,
)

promos_bp = Blueprint('promos_bp', __name__, url_prefix='/promos')

# Máximo de intentos de Adivina el Número que se aceptan por IP en 60s. No
# limita a una persona real (nadie manda 9 intentos por minuto a mano),
# pero sí frena un script que prueba muchos IDs reales rápido — que es el
# ataque real posible ahora que cada ID solo tiene 1 intento al día.
ADIVINA_RATE_LIMIT_PER_MINUTE = 8


def _client_ip():
    """IP real detrás de nginx. `remote_addr` a secas sería siempre
    127.0.0.1 y el límite por IP no filtraría absolutamente nada."""
    forwarded = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    return forwarded or request.remote_addr or ''


def _selected_game(games, game_id):
    if game_id:
        for g in games:
            if g.id == game_id:
                return g
    return games[0] if games else None


@promos_bp.route('/api/verify-player')
def verify_player():
    """Verificación pública de ID, compartida por las 3 promos: consulta el
    mismo verificador real que usa la tienda (Free Fire/Blood Strike) y
    devuelve el nombre del jugador, para que el cliente confirme su ID
    antes de registrarse o jugar."""
    from ..routes.verify import verify_player_nick

    game_id = request.args.get('game_id', type=int)
    player_id = (request.args.get('player_id') or '').strip()
    if not game_id:
        return jsonify({'ok': False, 'error': 'Falta el juego.'}), 400

    payload, status = verify_player_nick(player_id, str(game_id), mode='auto')
    return jsonify(payload), status


# ─── Recarga Acumulada ───────────────────────────────────────────────────────

@promos_bp.route('/recarga-acumulada')
def recarga_acumulada_page():
    games = get_accumulated_enabled_games()
    game = _selected_game(games, request.args.get('game_id', type=int))
    return render_template('promos/recarga_acumulada.html', games=games, game=game)


@promos_bp.route('/api/recarga-acumulada/estado')
def recarga_acumulada_estado():
    game_id = request.args.get('game_id', type=int)
    player_id = (request.args.get('player_id') or '').strip()
    if not game_id:
        return jsonify({'ok': False, 'error': 'Falta el juego.'}), 400
    try:
        state = get_accumulated_progress_state(game_id, player_id)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    return jsonify({'ok': True, **state})


# ─── Sorteo Diario ───────────────────────────────────────────────────────────

@promos_bp.route('/sorteo')
def sorteo_page():
    games = get_raffle_enabled_games()
    game = _selected_game(games, request.args.get('game_id', type=int))
    return render_template('promos/sorteo.html', games=games, game=game)


@promos_bp.route('/api/sorteo/estado')
def sorteo_estado():
    game_id = request.args.get('game_id', type=int)
    player_id = (request.args.get('player_id') or '').strip()
    if not game_id:
        return jsonify({'ok': False, 'error': 'Falta el juego.'}), 400
    state = get_raffle_public_state(game_id, player_id)
    return jsonify({'ok': True, **state})


@promos_bp.route('/api/sorteo/show')
def sorteo_show():
    """Estado completo con la lista real de participantes y ganadores de
    hoy, para la animación de la ruleta/canvas del sorteo."""
    game_id = request.args.get('game_id', type=int)
    player_id = (request.args.get('player_id') or '').strip()
    if not game_id:
        return jsonify({'ok': False, 'error': 'Falta el juego.'}), 400

    # Quien está viendo la página en vivo consulta este endpoint cada 20s
    # (ver cargarEstado() en sorteo.html) — si ya se cumplió la hora del
    # sorteo pero nadie se registró justo en ese momento (que es lo único
    # que hasta ahora disparaba run_daily_raffle_draws al instante, ver
    # sorteo_registrar), el sorteo se quedaba esperando al próximo tick del
    # scheduler en segundo plano (hasta 20s más) antes de correr. Disparar
    # el chequeo también aquí hace que corra apenas alguien esté mirando,
    # en vez de depender solo de esa otra coincidencia.
    run_daily_raffle_draws()

    state = get_raffle_show_state(game_id, player_id)
    return jsonify({'ok': True, **state})


@promos_bp.route('/api/sorteo/replay')
def sorteo_replay():
    """Participantes y ganadores de un día ya sorteado, para reproducir de
    nuevo la animación de la ruleta sobre ese resultado real."""
    game_id = request.args.get('game_id', type=int)
    day_key = (request.args.get('day_key') or '').strip()
    if not game_id or not day_key:
        return jsonify({'ok': False, 'error': 'Falta el juego o el día.'}), 400
    state = get_raffle_replay_state(game_id, day_key)
    if not state.get('enabled'):
        return jsonify({'ok': False, 'error': state.get('error') or 'No se pudo cargar ese sorteo.'}), 404
    return jsonify({'ok': True, **state})


@promos_bp.route('/api/sorteo/registrar', methods=['POST'])
def sorteo_registrar():
    data = request.get_json(silent=True) or {}
    game_id = data.get('game_id')
    player_id = data.get('player_id')
    try:
        game_id = int(game_id)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Falta el juego.'}), 400

    try:
        entry, is_next_day = register_raffle_entry(game_id, player_id)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400

    # Si ya se cumplió la hora del sorteo de hoy y este registro entró al
    # pool de hoy (todavía no se había corrido), se corre en el momento en
    # vez de esperar al próximo tick del scheduler (hasta 20s) — para que
    # quien se acaba de registrar vea el resultado al instante. Si el
    # registro ya cayó en el pool de mañana (hoy ya se sorteó), no hay nada
    # que correr ahora.
    if not is_next_day:
        run_daily_raffle_draws()

    return jsonify({
        'ok': True, 'ticket_number': entry.ticket_number, 'is_next_day': is_next_day,
        'player_nick': entry.player_nick,
    })


# ─── Adivina el Número ───────────────────────────────────────────────────────

@promos_bp.route('/adivina-el-numero')
def adivina_page():
    from ..routes.verify import verifiable_game_ids

    games = get_guess_enabled_games()
    game = _selected_game(games, request.args.get('game_id', type=int))
    verifiable = bool(game) and game.id in verifiable_game_ids()
    return render_template('promos/adivina_numero.html', games=games, game=game, verifiable=verifiable)


@promos_bp.route('/api/adivina/estado')
def adivina_estado():
    game_id = request.args.get('game_id', type=int)
    player_id = (request.args.get('player_id') or '').strip()
    if not game_id:
        return jsonify({'ok': False, 'error': 'Falta el juego.'}), 400
    state = get_guess_public_state(game_id, player_id)
    return jsonify({'ok': True, **state})


@promos_bp.route('/api/adivina/intentar', methods=['POST'])
def adivina_intentar():
    if not check_rate_limit(f'adivina_ip:{_client_ip()}', ADIVINA_RATE_LIMIT_PER_MINUTE, 60):
        return jsonify({
            'ok': False,
            'error': 'Demasiados intentos seguidos desde tu conexión. Espera un momento e intenta de nuevo.',
        }), 429

    data = request.get_json(silent=True) or {}
    game_id = data.get('game_id')
    player_id = data.get('player_id')
    guess = data.get('guess')
    try:
        game_id = int(game_id)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Falta el juego.'}), 400

    try:
        result = submit_guess(game_id, player_id, guess)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400

    return jsonify({'ok': True, **result})


# ─── Hordas de Diamantes ─────────────────────────────────────────────────────

HORDAS_START_RATE_LIMIT_PER_MINUTE = 10
HORDAS_FINISH_RATE_LIMIT_PER_MINUTE = 20


@promos_bp.route('/hordas')
def hordas_page():
    from ..routes.verify import verifiable_game_ids

    from ..utils.promos import get_horde_characters

    games = get_horde_enabled_games()
    game = _selected_game(games, request.args.get('game_id', type=int))
    verifiable = bool(game) and game.id in verifiable_game_ids()
    config = get_horde_config(game.id) if game else None
    # Solo los personajes que tienen imagen cargada se pueden elegir.
    characters = [
        {'slot': c['slot'], 'name': c['name'], 'mode': c['mode'],
         'url': url_for('static', filename='uploads/' + c['image']),
         'cover_url': url_for('static', filename='uploads/' + (c['cover'] or c['image']))}
        for c in get_horde_characters() if c['image']
    ]
    return render_template(
        'promos/hordas.html', games=games, game=game, verifiable=verifiable,
        characters=characters,
        horde_extra_cost=int(config.points_per_extra_run or 0) if config else 0,
    )


@promos_bp.route('/api/hordas/estado')
def hordas_estado():
    game_id = request.args.get('game_id', type=int)
    player_id = (request.args.get('player_id') or '').strip()
    if not game_id:
        return jsonify({'ok': False, 'error': 'Falta el juego.'}), 400
    return jsonify({'ok': True, **get_horde_public_state(game_id, player_id)})


@promos_bp.route('/api/hordas/posicion')
def hordas_posicion():
    from ..utils.promos import get_horde_player_position

    if not check_rate_limit(f'hordas_lookup_ip:{_client_ip()}', 30, 60):
        return jsonify({'ok': False, 'error': 'Demasiadas búsquedas seguidas. Espera un momento.'}), 429
    game_id = request.args.get('game_id', type=int)
    player_id = (request.args.get('player_id') or '').strip()
    if not game_id or not player_id:
        return jsonify({'ok': False, 'error': 'Escribe tu ID primero.'}), 400
    if is_horde_banned(game_id, player_id):
        return jsonify({'ok': False, 'banned': True, 'error': HORDE_BANNED_MESSAGE}), 403
    return jsonify({'ok': True, 'position': get_horde_player_position(game_id, player_id)})


@promos_bp.route('/api/hordas/iniciar', methods=['POST'])
def hordas_iniciar():
    ip = _client_ip()
    if not check_rate_limit(f'hordas_start_ip:{ip}', HORDAS_START_RATE_LIMIT_PER_MINUTE, 60):
        return jsonify({'ok': False, 'error': 'Demasiados intentos seguidos. Espera un momento.'}), 429

    data = request.get_json(silent=True) or {}
    try:
        game_id = int(data.get('game_id'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Falta el juego.'}), 400

    try:
        result = start_horde_run(game_id, data.get('player_id'), ip=ip, use_points=bool(data.get('use_points')))
    except HordeNotEnoughPoints as exc:
        return jsonify({'ok': False, 'code': 'no_points', 'error': str(exc), 'points_balance': exc.balance}), 400
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    return jsonify({'ok': True, **result})


@promos_bp.route('/api/hordas/terminar', methods=['POST'])
def hordas_terminar():
    if not check_rate_limit(f'hordas_finish_ip:{_client_ip()}', HORDAS_FINISH_RATE_LIMIT_PER_MINUTE, 60):
        return jsonify({'ok': False, 'error': 'Demasiados intentos seguidos. Espera un momento.'}), 429

    data = request.get_json(silent=True, force=True) or {}
    try:
        result = finish_horde_run(
            data.get('token'), data.get('diamonds'), data.get('kills'),
            wave=data.get('wave'), final_cleared=bool(data.get('final_cleared')),
            replay=data.get('replay'),
        )
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    return jsonify({'ok': True, **result})
