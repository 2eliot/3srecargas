"""Rutas públicas de las 3 promociones: Recarga Acumulada, Sorteo Diario y
Adivina el Número. Cada una tiene una página (elige juego si hay más de
uno activo) y un mini-API JSON que la página consume con fetch()."""
from flask import Blueprint, abort, current_app, jsonify, make_response, render_template, request, url_for
from flask_login import current_user

from ..models import Tournament, db
from ..utils import tournaments as tournaments_service
from ..utils.locks import check_rate_limit, client_ip
from ..utils.promos import (
    get_accumulated_enabled_games, get_accumulated_progress_state,
    get_guess_enabled_games, get_guess_public_state, submit_guess,
    HORDE_BANNED_MESSAGE, HordeNotEnoughPoints, HordeOutdatedPage, cancel_horde_run, get_horde_config, get_horde_enabled_games, is_horde_banned, get_horde_public_state, start_horde_run, finish_horde_run,
    get_raffle_enabled_games, get_raffle_public_state, get_raffle_replay_state,
    get_raffle_show_state, register_raffle_entry,
)

promos_bp = Blueprint('promos_bp', __name__, url_prefix='/promos')

# Máximo de intentos de Adivina el Número que se aceptan por IP en 60s. No
# limita a una persona real (nadie manda 9 intentos por minuto a mano),
# pero sí frena un script que prueba muchos IDs reales rápido — que es el
# ataque real posible ahora que cada ID solo tiene 1 intento al día.
ADIVINA_RATE_LIMIT_PER_MINUTE = 8
# Registro del sorteo y consulta de Recarga Acumulada: igual de caros (consulta externa por cada ID nuevo). Con topes amplios para conexiones compartidas.
SORTEO_REGISTRO_RATE_LIMIT_PER_MINUTE = 15
RECARGA_ESTADO_RATE_LIMIT_PER_MINUTE = 30


def _client_ip():
    """IP real del cliente (misma función segura que usan todos los topes: ver utils/locks.py)."""
    return client_ip()


def _selected_game(games, game_id):
    if game_id:
        for g in games:
            if g.id == game_id:
                return g
    return games[0] if games else None


# ─── Torneos ─────────────────────────────────────────────────────────────────

def _torneos_view(tournaments, tournament, preview=False):
    from ..routes.verify import verifiable_game_ids

    verifiable = bool(tournament) and tournament.game_id in verifiable_game_ids()
    scheduled_labels = {t.id: tournaments_service.opens_label(t) for t in tournaments if tournaments_service.is_scheduled(t)}
    return render_template('promos/torneos.html', tournaments=tournaments, tournament=tournament, verifiable=verifiable, preview=preview,
                           scheduled_labels=scheduled_labels)


@promos_bp.route('/torneos')
def torneos_page():
    """Página de Torneos (apartado de "Dinámicas Gratis" del menú). Con un solo torneo visible se muestra directo; con varios, la lista; sin ninguno, "muy pronto"."""
    tournaments = tournaments_service.visible_tournaments()
    return _torneos_view(tournaments, tournaments[0] if len(tournaments) == 1 else None)


@promos_bp.route('/torneos/<int:tournament_id>')
def torneo_page(tournament_id):
    """El link que el dueño publica en WhatsApp. Un torneo en "Preparando" solo lo ve el administrador (vista previa)."""
    tournament = db.session.get(Tournament, tournament_id)
    is_admin = current_user.is_authenticated and current_user.__class__.__name__ == 'AdminUser'
    visible = tournament is not None and tournament.status in tournaments_service.VISIBLE_STATUSES
    if tournament is None or not (visible or is_admin):
        abort(404)
    return _torneos_view([tournament], tournament, preview=not visible)


def _torneo_or_404(tournament_id, allow_admin_preview=False):
    tournament = db.session.get(Tournament, tournament_id)
    is_admin = allow_admin_preview and current_user.is_authenticated and current_user.__class__.__name__ == 'AdminUser'
    if tournament is None or (tournament.status not in tournaments_service.VISIBLE_STATUSES and not is_admin):
        abort(404)
    return tournament


@promos_bp.route('/api/torneos/<int:tournament_id>/estado')
def torneo_estado(tournament_id):
    """Estado del torneo para la página. Con `player_id` agrega, si ese ID ya está inscrito, los datos de su sala. Con `room` (la sala que la persona tocó
    para volver a entrar) los datos solo salen si está inscrita EN ESA sala."""
    if not check_rate_limit(f'torneo_estado:{_client_ip()}', tournaments_service.STATE_RATE_PER_MINUTE, 60):
        return jsonify({'ok': False, 'error': 'Demasiadas consultas seguidas. Espera un momento.'}), 429
    tournament = _torneo_or_404(tournament_id, allow_admin_preview=True)
    player_id = (request.args.get('player_id') or '').strip()[:60]
    room_position = (request.args.get('room') or '').strip()[:6] or None
    return jsonify({'ok': True, **tournaments_service.public_state(tournament, player_id, room_position=room_position)})


@promos_bp.route('/api/torneos/<int:tournament_id>/unirme', methods=['POST'])
def torneo_unirme(tournament_id):
    """Inscribe a un ID (verificado en el servidor) en la primera sala con lugar y devuelve los datos de esa sala."""
    ip = _client_ip()
    if not check_rate_limit(f'torneo_join:{ip}', tournaments_service.JOIN_RATE_PER_MINUTE, 60):
        return jsonify({'ok': False, 'error': 'Demasiados intentos seguidos desde tu conexión. Espera un momento e intenta de nuevo.'}), 429
    tournament = _torneo_or_404(tournament_id)
    data = request.get_json(silent=True) or {}
    try:
        entry, _room, already = tournaments_service.join_tournament(tournament.id, data.get('player_id'), ip=ip, room_position=data.get('room_position'))
    except tournaments_service.TournamentError as exc:
        return jsonify({'ok': False, 'error': str(exc), **tournaments_service.public_state(tournament)}), 400
    return jsonify({'ok': True, 'already': already, **tournaments_service.public_state(tournament, entry.player_id)})


@promos_bp.route('/api/verify-player')
def verify_player():
    """Verificación pública de ID, compartida por las 3 promos: consulta el
    mismo verificador real que usa la tienda (Free Fire/Blood Strike) y
    devuelve el nombre del jugador, para que el cliente confirme su ID
    antes de registrarse o jugar."""
    from ..routes.verify import _verify_rate_limited, verify_player_nick

    game_id = request.args.get('game_id', type=int)
    player_id = (request.args.get('player_id') or '').strip()
    if not game_id:
        return jsonify({'ok': False, 'error': 'Falta el juego.'}), 400
    # Mismo tope por IP que las rutas /store/player/verify*: cada ID nuevo dispara una consulta externa (hasta ~40 s) y sin freno
    # cualquiera podía gastarla o hacer que el proveedor bloquee la llave del sitio.
    limited = _verify_rate_limited()
    if limited:
        return limited

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
    # Cada ID nuevo puede disparar una consulta externa (hasta ~85 s) que ocupa un hilo del servidor: se limita por conexión.
    if player_id and not check_rate_limit(f'recarga_estado_ip:{_client_ip()}', RECARGA_ESTADO_RATE_LIMIT_PER_MINUTE, 60):
        return jsonify({'ok': False, 'error': 'Demasiadas consultas seguidas. Espera un momento.'}), 429
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

    # El sorteo (y la entrega de sus premios, que puede tardar de segundos a minutos) ya NO se dispara desde esta visita: lo corre el
    # proceso de fondo cada 20 s (utils/order_scheduler._run_promos_tick). Esta ruta solo lee el resultado.
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
    # Cada ID nuevo dispara una consulta externa (hasta ~85 s) que ocupa un hilo del servidor, y sin verificación se podía llenar el sorteo de IDs falsos.
    if not check_rate_limit(f'sorteo_reg_ip:{_client_ip()}', SORTEO_REGISTRO_RATE_LIMIT_PER_MINUTE, 60):
        return jsonify({'ok': False, 'error': 'Demasiados registros seguidos desde tu conexión. Espera un momento e intenta de nuevo.'}), 429
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

    # Si el sorteo de hoy ya cumplió su hora, lo corre el proceso de fondo en un máximo de ~20 s (la página consulta el resultado sola);
    # ya no se ejecuta dentro de esta petición para que quien se registra no espere por la entrega de premios.
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
        result = submit_guess(game_id, player_id, guess, ip=_client_ip(), user_agent=request.headers.get('User-Agent', ''))
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400

    return jsonify({'ok': True, **result})


# ─── Hordas de Diamantes ─────────────────────────────────────────────────────

# Por IP y por minuto. Varios clientes comparten la misma IP (datos móviles, una casa, un local con wifi), así que los topes no pueden ser muy cortos:
# antes eran 10 y 20 y una IP compartida podía quedarse sin poder iniciar o guardar partidas.
HORDAS_START_RATE_LIMIT_PER_MINUTE = 30
HORDAS_FINISH_RATE_LIMIT_PER_MINUTE = 60
HORDAS_CANCEL_RATE_LIMIT_PER_MINUTE = 20


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
    response = make_response(render_template(
        'promos/hordas.html', games=games, game=game, verifiable=verifiable,
        characters=characters,
        horde_extra_cost=int(config.points_per_extra_run or 0) if config else 0,
    ))
    # Sin caché: tras una actualización del juego el navegador debe traer
    # siempre la versión nueva (una vieja jugaría con reglas viejas).
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


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
        result = start_horde_run(game_id, data.get('player_id'), ip=ip, use_points=bool(data.get('use_points')),
                                 rules=data.get('rules'))
    except HordeOutdatedPage as exc:
        return jsonify({'ok': False, 'code': 'outdated', 'error': str(exc)}), 409
    except HordeNotEnoughPoints as exc:
        return jsonify({'ok': False, 'code': 'no_points', 'error': str(exc), 'points_balance': exc.balance}), 400
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception:
        # Falla inesperada: siempre una respuesta JSON clara (así el juego distingue "servidor ocupado" de "sin internet") y queda en el registro.
        db.session.rollback()
        current_app.logger.exception('Hordas: error inesperado al iniciar una partida')
        return jsonify({'ok': False, 'code': 'server', 'error': 'El servidor está ocupado. Intenta de nuevo en unos segundos.'}), 503
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
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Hordas: error inesperado al guardar una partida')
        return jsonify({'ok': False, 'code': 'server', 'error': 'El servidor está ocupado. Intenta de nuevo en unos segundos.'}), 503
    return jsonify({'ok': True, **result})


@promos_bp.route('/api/hordas/cancelar', methods=['POST'])
def hordas_cancelar():
    """Anula una partida que se abrió pero el teléfono no pudo armar (falla del juego en ese equipo): no se gasta la partida del día ni los puntos."""
    if not check_rate_limit(f'hordas_cancel_ip:{_client_ip()}', HORDAS_CANCEL_RATE_LIMIT_PER_MINUTE, 60):
        return jsonify({'ok': False, 'error': 'Demasiados intentos seguidos. Espera un momento.'}), 429
    data = request.get_json(silent=True, force=True) or {}
    try:
        result = cancel_horde_run(data.get('token'))
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Hordas: error inesperado al anular una partida')
        return jsonify({'ok': False, 'code': 'server', 'error': 'El servidor está ocupado. Intenta de nuevo en unos segundos.'}), 503
    return jsonify({'ok': True, 'refunded_points': result['refunded_points']})
