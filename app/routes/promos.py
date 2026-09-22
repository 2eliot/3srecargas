"""Rutas públicas de las 3 promociones: Recarga Acumulada, Sorteo Diario y
Adivina el Número. Cada una tiene una página (elige juego si hay más de
uno activo) y un mini-API JSON que la página consume con fetch()."""
from flask import Blueprint, jsonify, render_template, request

from ..utils.promos import (
    get_accumulated_enabled_games, get_accumulated_progress_state,
    get_guess_enabled_games, get_guess_public_state, submit_guess,
    get_raffle_enabled_games, get_raffle_public_state, get_raffle_show_state, register_raffle_entry,
    run_daily_raffle_draws,
)

promos_bp = Blueprint('promos_bp', __name__, url_prefix='/promos')


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
    state = get_raffle_show_state(game_id, player_id)
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
