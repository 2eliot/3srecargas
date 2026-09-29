"""Lógica de las 3 promociones de engagement: Recarga Acumulada (barra por
niveles), Sorteo Diario (rifa con ticket por ID) y Adivina el Número.

Los tres comparten el mismo mecanismo de entrega de premio que el resto de
la tienda (deliver_prize_to_player: orden interna de $0 por el bot/stock
configurado) y, cuando el juego tiene verificación de ID configurada, la
misma verificación real que usa la tienda antes de aceptar un registro o
una jugada — así no se le puede regalar un premio a un ID inventado.
"""
import random
import secrets
from datetime import datetime, time, timedelta
from uuid import uuid4

from sqlalchemy import update

from .locks import acquire_lock, release_lock
from .order_units import extract_order_units
from .points import get_player_points_balance
from .timezone import now_ve, today_ve_str, format_ve
from ..models import (
    Game, Order, Package, PlayerPoints,
    PromoAccumulatedAward, PromoAccumulatedLevel, PromoAccumulatedOrderLog, PromoAccumulatedProgress,
    PromoGuessAttempt, PromoGuessConfig, PromoGuessRound, PromoGuessWinner,
    PromoHordeBan, PromoHordeConfig, PromoHordeReplay, PromoHordeRun, PromoHordeWinner,
    PromoRaffleConfig, PromoRaffleEntry, PromoRaffleWinner,
    Setting, db,
)


def _current_month_key():
    return now_ve().strftime('%Y-%m')


def _tomorrow_ve_str():
    return (now_ve() + timedelta(days=1)).strftime('%Y-%m-%d')


def _verify_id_if_needed(game_id, player_id, require_verification):
    """Corre la verificación real de ID (la misma que usa la tienda) cuando
    el juego la tiene configurada y el admin la pidió para esta promo.
    Devuelve (ok, error_message, nick_or_none)."""
    if not require_verification:
        return True, None, None

    from ..routes.verify import verifiable_game_ids, verify_player_nick

    if int(game_id) not in verifiable_game_ids():
        # El juego no tiene verificación configurada en absoluto: no hay
        # contra qué comprobar el ID, así que se deja pasar.
        return True, None, None

    payload, status = verify_player_nick(str(player_id), str(game_id), mode='auto')
    if not payload.get('ok'):
        return False, payload.get('error') or 'No se pudo verificar tu ID. Revísalo e intenta de nuevo.', None
    return True, None, payload.get('nick')


# ─── Recarga Acumulada ───────────────────────────────────────────────────────

def get_accumulated_levels(game_id):
    return (
        PromoAccumulatedLevel.query
        .filter_by(game_id=game_id)
        .order_by(PromoAccumulatedLevel.level_number.asc())
        .all()
    )


def get_accumulated_enabled_games():
    """Juegos activos que tienen al menos un nivel configurado, ordenados
    por el campo "Posición" propio de esta promo (editable en Admin >
    Mini Juegos, junto al selector de juego) — el más bajo va primero, y
    ese primero es el que queda seleccionado por defecto sin game_id en
    la URL."""
    order_by_game = dict(
        db.session.query(
            PromoAccumulatedLevel.game_id,
            db.func.min(PromoAccumulatedLevel.sort_order),
        ).group_by(PromoAccumulatedLevel.game_id).all()
    )
    if not order_by_game:
        return []
    games = (
        Game.query
        .filter(Game.id.in_(order_by_game.keys()), Game.is_active.is_(True))
        .all()
    )
    games.sort(key=lambda g: (order_by_game.get(g.id) or 100, g.name.lower()))
    return games


def award_accumulated_recharge_for_order(order):
    """Suma las unidades (diamantes/oro) de esta orden a la barra de
    Recarga Acumulada del (juego, ID) correspondiente, entregando el premio
    automáticamente cada vez que se completa un nivel. Idempotente por
    orden.

    Usa el mismo criterio que el Ranking mensual (extract_order_units):
    el número que trae el nombre del paquete, ej. "550 Diamantes" → 550,
    no el monto pagado — así la barra mide lo mismo que el ranking."""
    if not order or not order.player_id:
        return
    if PromoAccumulatedOrderLog.query.filter_by(order_id=order.id).first():
        return

    levels = get_accumulated_levels(order.game_id)
    if not levels:
        return

    amount_to_apply = extract_order_units(order)
    if amount_to_apply <= 0:
        return

    db.session.add(PromoAccumulatedOrderLog(order_id=order.id))

    month_key = _current_month_key()
    progress = PromoAccumulatedProgress.query.filter_by(
        game_id=order.game_id, player_id=order.player_id, month_key=month_key,
    ).first()
    if not progress:
        progress = PromoAccumulatedProgress(
            game_id=order.game_id, player_id=order.player_id, month_key=month_key,
            current_level_number=1, accumulated_amount=0,
        )
        db.session.add(progress)
        db.session.flush()

    from .order_processing import deliver_prize_to_player

    game = Game.query.get(order.game_id)
    remaining = amount_to_apply
    while remaining > 0 and progress.current_level_number <= len(levels):
        level = levels[progress.current_level_number - 1]
        threshold = float(level.threshold_amount)
        new_total = float(progress.accumulated_amount or 0) + remaining

        if new_total < threshold:
            progress.accumulated_amount = new_total
            remaining = 0
            break

        overflow = new_total - threshold
        prize_order, _approval = deliver_prize_to_player(
            game, level.package, order.player_id,
            zone_id=order.zone_id,
            note=f'Premio Recarga Acumulada — nivel {level.level_number} ({level.level_name}), ID {order.player_id}.',
            reference_prefix='RECARGA-ACUM',
        )
        db.session.add(PromoAccumulatedAward(
            game_id=order.game_id, player_id=order.player_id, month_key=month_key,
            level_number=level.level_number, level_name=level.level_name,
            prize_order_id=prize_order.id if prize_order else None,
        ))
        progress.current_level_number += 1
        progress.accumulated_amount = 0
        remaining = overflow

    progress.updated_at = datetime.utcnow()
    db.session.flush()


def get_accumulated_progress_state(game_id, player_id):
    """Estado para pintar la barra: nivel actual, % de progreso y los
    premios ya ganados este mes."""
    levels = get_accumulated_levels(game_id)
    levels_out = [{
        'level_number': lvl.level_number,
        'level_name': lvl.level_name,
        'threshold_amount': float(lvl.threshold_amount),
        'reward_label': lvl.package.name if lvl.package else '',
    } for lvl in levels]

    result = {
        'enabled': bool(levels),
        'levels': levels_out,
        'month_key': _current_month_key(),
        'current_level_number': 1,
        'accumulated_amount': 0.0,
        'percentage': 0,
        'completed_all': False,
        'awards_this_month': [],
        'player_nick': None,
    }
    if not levels or not player_id:
        return result

    player_id = str(player_id).strip()

    # Si el juego tiene verificación configurada, el ID tiene que existir de
    # verdad: no tiene sentido mostrarle una barra de progreso a un ID
    # inventado. En juegos sin verificador configurado esto se salta solo
    # (ver _verify_id_if_needed) y el progreso se sigue mostrando igual.
    ok, error, nick = _verify_id_if_needed(game_id, player_id, True)
    if not ok:
        raise ValueError(error)
    result['player_nick'] = nick

    progress = PromoAccumulatedProgress.query.filter_by(
        game_id=game_id, player_id=player_id, month_key=_current_month_key(),
    ).first()
    if progress:
        result['current_level_number'] = progress.current_level_number
        result['accumulated_amount'] = float(progress.accumulated_amount or 0)
        if progress.current_level_number > len(levels):
            result['completed_all'] = True
            result['percentage'] = 100
        else:
            threshold = float(levels[progress.current_level_number - 1].threshold_amount)
            result['percentage'] = min(100, int((result['accumulated_amount'] / threshold) * 100)) if threshold else 0

        awards = (
            PromoAccumulatedAward.query
            .filter_by(game_id=game_id, player_id=player_id, month_key=_current_month_key())
            .order_by(PromoAccumulatedAward.level_number.asc())
            .all()
        )
        result['awards_this_month'] = [{
            'level_number': a.level_number, 'level_name': a.level_name,
        } for a in awards]

    return result


# ─── Sorteo Diario ───────────────────────────────────────────────────────────

def get_raffle_config(game_id):
    return PromoRaffleConfig.query.filter_by(game_id=game_id, is_active=True).first()


def get_raffle_enabled_games():
    """Juegos con el Sorteo Diario activo, ordenados por su "Posición en el
    selector" propia de esta promo (editable en Admin > Mini Juegos)."""
    configs = PromoRaffleConfig.query.filter_by(is_active=True).all()
    order_by_game = {c.game_id: (c.sort_order or 100) for c in configs}
    if not order_by_game:
        return []
    games = (
        Game.query
        .filter(Game.id.in_(order_by_game.keys()), Game.is_active.is_(True))
        .all()
    )
    games.sort(key=lambda g: (order_by_game.get(g.id, 100), g.name.lower()))
    return games


# Cuánto antes de la hora del sorteo se cierra el registro: así la lista de
# participantes queda fija un momento antes de sortear, en vez de perseguir
# a quien se registra justo cuando el contador llega a 0.
RAFFLE_REGISTRATION_CUTOFF_SECONDS = 30


def _raffle_registration_closed_for_today(config):
    """True si faltan menos de RAFFLE_REGISTRATION_CUTOFF_SECONDS para la
    hora configurada del sorteo de hoy (o ya se pasó)."""
    now = now_ve()
    draw_at_today = now.replace(
        hour=config.draw_hour or 21, minute=config.draw_minute or 0,
        second=0, microsecond=0,
    )
    cutoff = draw_at_today - timedelta(seconds=RAFFLE_REGISTRATION_CUTOFF_SECONDS)
    return now >= cutoff


def _raffle_open_day_key(game_id, config=None):
    """A qué día se apunta un registro nuevo en este momento.

    Mientras el sorteo de hoy no se haya corrido, el registro entra al
    pool de hoy. En cuanto ya hay ganadores de hoy, o faltan menos de
    RAFFLE_REGISTRATION_CUTOFF_SECONDS para la hora del sorteo, un registro
    nuevo NO puede colarse en un sorteo que ya está por resolverse — pasa a
    contar para el sorteo de mañana, tal como se pidió ("Asegura tu Ticket
    Gratis para el Siguiente Sorteo"). Sin esto, cualquiera que se
    registrara justo antes/después de la hora del sorteo se quedaba con un
    ticket que nunca se iba a sortear (o forzaba a esperar el último
    registro para poder arrancar)."""
    day_key = today_ve_str()
    already_drawn = PromoRaffleWinner.query.filter_by(game_id=game_id, day_key=day_key).first()
    if already_drawn:
        return _tomorrow_ve_str()
    if config and _raffle_registration_closed_for_today(config):
        return _tomorrow_ve_str()
    return day_key


def register_raffle_entry(game_id, player_id):
    """Registra un ID en el sorteo de hoy, o en el de mañana si el de hoy
    ya se corrió. Verifica el ID contra el sistema real antes de aceptar
    el registro cuando la promo lo exige. Lanza ValueError con un mensaje
    listo para mostrar si algo no procede. Devuelve (entry, is_next_day)."""
    config = get_raffle_config(game_id)
    if not config:
        raise ValueError('El sorteo no está activo para este juego en este momento.')

    player_id = str(player_id or '').strip()
    if not player_id:
        raise ValueError('Ingresa tu ID de juego.')

    day_key = _raffle_open_day_key(game_id, config=config)
    is_next_day = day_key != today_ve_str()

    existing = PromoRaffleEntry.query.filter_by(game_id=game_id, player_id=player_id, day_key=day_key).first()
    if existing:
        raise ValueError(
            'Este ID ya está registrado para el sorteo de mañana.' if is_next_day
            else 'Este ID ya está registrado para el sorteo de hoy.'
        )

    ok, error, nick = _verify_id_if_needed(game_id, player_id, config.require_verification)
    if not ok:
        raise ValueError(error)

    for _attempt in range(5):
        current_count = PromoRaffleEntry.query.filter_by(game_id=game_id, day_key=day_key).count()
        entry = PromoRaffleEntry(
            game_id=game_id, player_id=player_id, player_nick=nick,
            day_key=day_key, ticket_number=current_count + 1,
        )
        db.session.add(entry)
        try:
            db.session.commit()
            return entry, is_next_day
        except Exception:
            db.session.rollback()
    raise ValueError('No se pudo asignar tu ticket, intenta de nuevo.')


def get_raffle_entry(game_id, player_id, day_key=None):
    player_id = str(player_id or '').strip()
    if not player_id:
        return None
    return PromoRaffleEntry.query.filter_by(
        game_id=game_id, player_id=player_id, day_key=day_key or today_ve_str(),
    ).first()


def run_daily_raffle_draws():
    """Corre el sorteo del día para cada juego que ya llegó a su hora de
    sorteo y todavía no tiene ganadores hoy. La llaman tanto el scheduler en
    segundo plano (cada ~20s) como cada consulta en vivo de la página del
    sorteo (para que arranque al instante sin esperar al scheduler) — dos
    llamadas pueden caer casi al mismo tiempo, así que cada juego se sortea
    bajo un lock exclusivo: sin él, ambas podían leer "todavía no hay
    ganadores" antes de que ninguna confirmara los suyos, y el sorteo
    terminaba con el doble (o más) de ganadores y premios entregados de los
    configurados."""
    configs = PromoRaffleConfig.query.filter_by(is_active=True).all()
    current_time = now_ve().time().replace(second=0, microsecond=0)
    day_key = today_ve_str()

    from .order_processing import deliver_prize_to_player

    for config in configs:
        draw_time = time(config.draw_hour or 21, config.draw_minute or 0)
        if current_time < draw_time:
            continue

        lock_key = f'raffle_draw:{config.game_id}:{day_key}'
        lock_holder = uuid4().hex
        if not acquire_lock(lock_key, 60, lock_holder):
            # Otra llamada ya está sorteando este mismo juego/día ahora
            # mismo — no hay nada que hacer aquí, la que tiene el lock lo
            # resuelve.
            continue

        try:
            already_drawn = PromoRaffleWinner.query.filter_by(game_id=config.game_id, day_key=day_key).first()
            if already_drawn:
                continue

            entries = PromoRaffleEntry.query.filter_by(game_id=config.game_id, day_key=day_key).all()
            if not entries:
                continue

            # El registro ya garantiza un ID por día (constraint único en
            # PromoRaffleEntry), pero se deduplica igual antes de sortear: así
            # un mismo ID nunca puede quedar elegido dos veces en el mismo
            # sorteo pase lo que pase con los datos.
            entries_by_player = {}
            for entry in entries:
                entries_by_player.setdefault(entry.player_id, entry)
            unique_entries = list(entries_by_player.values())

            game = Game.query.get(config.game_id)
            winners_needed = min(config.winners_per_draw or 5, len(unique_entries))
            chosen = random.sample(unique_entries, winners_needed)

            for entry in chosen:
                prize_order = None
                if config.package:
                    prize_order, _approval = deliver_prize_to_player(
                        game, config.package, entry.player_id,
                        note=f'Premio Sorteo Diario — ticket #{entry.ticket_number} ({day_key}).',
                        reference_prefix='SORTEO',
                    )
                db.session.add(PromoRaffleWinner(
                    game_id=config.game_id, day_key=day_key, player_id=entry.player_id,
                    player_nick=entry.player_nick, ticket_number=entry.ticket_number,
                    prize_order_id=prize_order.id if prize_order else None,
                ))
            db.session.commit()
        finally:
            release_lock(lock_key, lock_holder)


def get_raffle_public_state(game_id, player_id):
    config = get_raffle_config(game_id)
    if not config:
        return {'enabled': False}

    day_key = today_ve_str()
    total_entries = PromoRaffleEntry.query.filter_by(game_id=game_id, day_key=day_key).count()
    winners = (
        PromoRaffleWinner.query
        .filter_by(game_id=game_id, day_key=day_key)
        .order_by(PromoRaffleWinner.created_at.asc())
        .all()
    )
    drawn = bool(winners)

    my_entry_today = get_raffle_entry(game_id, player_id, day_key=day_key)
    my_entry_tomorrow = None
    total_entries_tomorrow = 0
    if drawn:
        # El sorteo de hoy ya se resolvió: cualquier registro a partir de
        # ahora entra al pool de mañana, así que se informa aparte.
        tomorrow_key = _tomorrow_ve_str()
        my_entry_tomorrow = get_raffle_entry(game_id, player_id, day_key=tomorrow_key)
        total_entries_tomorrow = PromoRaffleEntry.query.filter_by(game_id=game_id, day_key=tomorrow_key).count()

    # Los últimos 3 días CON sorteo (no las últimas 30 filas, que con
    # winners_per_draw chico podían abarcar muchos más de 3 días, o con uno
    # grande cortar un día a la mitad). Incluye hoy si ya se corrió: al
    # concluir, "Ganadores de hoy" se oculta y el historial es lo único que
    # queda mostrando quién ganó, así que hoy tiene que entrar ahí también.
    recent_day_keys = [
        row[0] for row in
        db.session.query(PromoRaffleWinner.day_key)
        .filter(PromoRaffleWinner.game_id == game_id, PromoRaffleWinner.day_key <= day_key)
        .distinct()
        .order_by(PromoRaffleWinner.day_key.desc())
        .limit(3)
        .all()
    ]
    history = (
        PromoRaffleWinner.query
        .filter(PromoRaffleWinner.game_id == game_id, PromoRaffleWinner.day_key.in_(recent_day_keys))
        .order_by(PromoRaffleWinner.day_key.desc(), PromoRaffleWinner.created_at.asc())
        .all()
    ) if recent_day_keys else []
    history_by_day = {}
    for w in history:
        history_by_day.setdefault(w.day_key, []).append({
            'player_id': w.player_id, 'ticket_number': w.ticket_number,
            'name': w.player_nick or 'Jugador',
        })

    now = now_ve()
    next_draw = now.replace(hour=config.draw_hour or 21, minute=config.draw_minute or 0, second=0, microsecond=0)
    if drawn or now >= next_draw:
        next_draw = next_draw + timedelta(days=1)

    return {
        'enabled': True,
        'draw_hour': config.draw_hour,
        'draw_minute': config.draw_minute or 0,
        'winners_per_draw': config.winners_per_draw,
        'reward_label': config.package.name if config.package else '',
        'total_entries': total_entries,
        'my_ticket': my_entry_today.ticket_number if my_entry_today else None,
        'my_nick': my_entry_today.player_nick if my_entry_today else None,
        'my_registered_at': format_ve(my_entry_today.created_at, '%d/%m/%Y %H:%M:%S') if my_entry_today else None,
        'drawn': drawn,
        'winners_today': [{'player_id': w.player_id, 'ticket_number': w.ticket_number} for w in winners],
        'history': [{'day_key': day, 'winners': items} for day, items in history_by_day.items()],
        'next_draw_at': next_draw.isoformat(),
        'registration_open_for': 'tomorrow' if drawn else 'today',
        'my_ticket_tomorrow': my_entry_tomorrow.ticket_number if my_entry_tomorrow else None,
        'my_nick_tomorrow': my_entry_tomorrow.player_nick if my_entry_tomorrow else None,
        'my_registered_at_tomorrow': format_ve(my_entry_tomorrow.created_at, '%d/%m/%Y %H:%M:%S') if my_entry_tomorrow else None,
        'total_entries_tomorrow': total_entries_tomorrow,
    }


def get_raffle_show_state(game_id, player_id):
    """Estado completo para la versión "show" del sorteo: además de todo lo
    de get_raffle_public_state, trae la lista de participantes de hoy y los
    ganadores ya elegidos por el servidor (con nombre para mostrar), para
    que el front pueda reproducir la animación de la ruleta/canvas sobre
    datos reales en vez de la lista de 500 IDs inventados del mockup.

    La selección del ganador la sigue decidiendo el servidor
    (run_daily_raffle_draws) — el navegador solo dramatiza un resultado que
    ya quedó decidido, nunca elige él mismo quién se gana el premio real."""
    base = get_raffle_public_state(game_id, player_id)
    if not base.get('enabled'):
        return base

    day_key = today_ve_str()
    entries = (
        PromoRaffleEntry.query
        .filter_by(game_id=game_id, day_key=day_key)
        .order_by(PromoRaffleEntry.ticket_number.asc())
        .all()
    )
    winners = (
        PromoRaffleWinner.query
        .filter_by(game_id=game_id, day_key=day_key)
        .order_by(PromoRaffleWinner.created_at.asc())
        .all()
    )

    def _display_name(nick):
        return nick or 'Jugador'

    base['participants'] = [
        {'ticket': e.ticket_number, 'id': e.player_id, 'name': _display_name(e.player_nick)}
        for e in entries
    ]
    base['winners_show'] = [
        {'ticket': w.ticket_number, 'id': w.player_id, 'name': _display_name(w.player_nick)}
        for w in winners
    ]
    now = now_ve()
    draw_time = time(base['draw_hour'] or 21, base.get('draw_minute') or 0)
    base['is_draw_time'] = now.time().replace(second=0, microsecond=0) >= draw_time
    return base


def get_raffle_replay_state(game_id, day_key):
    """Participantes y ganadores de un día YA sorteado, para que el front
    pueda reproducir de nuevo la animación de la ruleta sobre ese resultado
    real — ver "Ver repetición del sorteo" en el historial. No decide nada
    nuevo, solo relee lo que run_daily_raffle_draws ya resolvió ese día."""
    config = get_raffle_config(game_id)
    if not config:
        return {'enabled': False, 'error': 'Este sorteo no está activo.'}

    winners = (
        PromoRaffleWinner.query
        .filter_by(game_id=game_id, day_key=day_key)
        .order_by(PromoRaffleWinner.created_at.asc())
        .all()
    )
    if not winners:
        return {'enabled': False, 'error': 'Ese día no tiene un sorteo registrado.'}

    entries = (
        PromoRaffleEntry.query
        .filter_by(game_id=game_id, day_key=day_key)
        .order_by(PromoRaffleEntry.ticket_number.asc())
        .all()
    )

    def _display_name(nick):
        return nick or 'Jugador'

    return {
        'enabled': True,
        'day_key': day_key,
        'reward_label': config.package.name if config.package else '',
        'participants': [
            {'ticket': e.ticket_number, 'id': e.player_id, 'name': _display_name(e.player_nick)}
            for e in entries
        ],
        'winners': [
            {'ticket': w.ticket_number, 'id': w.player_id, 'name': _display_name(w.player_nick)}
            for w in winners
        ],
    }


# ─── Adivina el Número ───────────────────────────────────────────────────────

def get_guess_config(game_id):
    return PromoGuessConfig.query.filter_by(game_id=game_id, is_active=True).first()


def get_guess_enabled_games():
    """Juegos con Adivina el Número activo, ordenados por su "Posición en
    el selector" propia de esta promo (editable en Admin > Mini Juegos)."""
    configs = PromoGuessConfig.query.filter_by(is_active=True).all()
    order_by_game = {c.game_id: (c.sort_order or 100) for c in configs}
    if not order_by_game:
        return []
    games = (
        Game.query
        .filter(Game.id.in_(order_by_game.keys()), Game.is_active.is_(True))
        .all()
    )
    games.sort(key=lambda g: (order_by_game.get(g.id, 100), g.name.lower()))
    return games


def _get_or_create_round(game_id, number_max):
    day_key = today_ve_str()
    round_row = PromoGuessRound.query.filter_by(game_id=game_id, day_key=day_key).first()
    if round_row:
        return round_row
    round_row = PromoGuessRound(
        game_id=game_id, day_key=day_key,
        secret_number=random.randint(1, number_max), winners_count=0,
    )
    db.session.add(round_row)
    db.session.flush()
    return round_row


def submit_guess(game_id, player_id, guess_value):
    """Procesa un intento. Lanza ValueError con un mensaje listo para
    mostrar si algo no procede. Devuelve un dict con el resultado."""
    config = get_guess_config(game_id)
    if not config:
        raise ValueError('Este juego no está activo en este momento.')

    player_id = str(player_id or '').strip()
    if not player_id:
        raise ValueError('Ingresa tu ID de juego.')
    try:
        guess_value = int(guess_value)
    except (TypeError, ValueError):
        raise ValueError('Ingresa un número válido.')
    if guess_value < 1 or guess_value > config.number_max:
        raise ValueError(f'Ingresa un número entre 1 y {config.number_max}.')

    round_row = _get_or_create_round(game_id, config.number_max)
    if round_row.winners_count >= config.winners_per_day:
        raise ValueError('Ya se completaron los cupos ganadores de hoy. Espera al reinicio (mira el temporizador arriba).')

    # Lock por (juego, día, ID): sin esto, dos intentos casi simultáneos del
    # mismo ID (doble tap, reintento de red, o alguien mandando el mismo
    # intento dos veces a propósito) podían pasar AMBOS la validación de
    # "¿ya ganó hoy?" antes de que el primero terminara de guardarse, y
    # los dos entregaban el premio — el mismo ID ganaba dos veces con el
    # mismo número. El lock queda en la base de datos (no en memoria) para
    # que funcione igual con varios workers de gunicorn.
    lock_key = f'adivina_guess:{game_id}:{round_row.day_key}:{player_id}'
    lock_holder = uuid4().hex
    if not acquire_lock(lock_key, 15, lock_holder):
        raise ValueError('Tu intento anterior todavía se está procesando. Espera unos segundos e intenta de nuevo.')

    try:
        db.session.refresh(round_row)

        already_won = PromoGuessWinner.query.filter_by(
            game_id=game_id, day_key=round_row.day_key, player_id=player_id,
        ).first()
        if already_won:
            raise ValueError('Este ID ya ganó un cupo hoy.')

        attempt = PromoGuessAttempt.query.filter_by(
            game_id=game_id, day_key=round_row.day_key, player_id=player_id,
        ).first()
        if not attempt:
            attempt = PromoGuessAttempt(game_id=game_id, day_key=round_row.day_key, player_id=player_id, attempts_used=0)
            db.session.add(attempt)
            db.session.flush()

        if attempt.attempts_used >= config.max_attempts:
            raise ValueError('Ya agotaste tus intentos de hoy para este ID.')

        # La verificación corre antes de gastar el intento: un ID inválido no
        # debe consumirle una oportunidad a nadie.
        ok, error, verified_nick = _verify_id_if_needed(game_id, player_id, config.require_verification)
        if not ok:
            raise ValueError(error)

        attempt.attempts_used += 1
        attempt.updated_at = datetime.utcnow()
        remaining_attempts = config.max_attempts - attempt.attempts_used

        if guess_value == round_row.secret_number:
            from .order_processing import deliver_prize_to_player

            game = Game.query.get(game_id)
            prize_order = None
            if config.package:
                prize_order, _approval = deliver_prize_to_player(
                    game, config.package, player_id,
                    note=f'Premio Adivina el Número — cupo #{round_row.winners_count + 1} ({round_row.day_key}).',
                    reference_prefix='ADIVINA',
                )
            round_row.winners_count += 1
            db.session.add(PromoGuessWinner(
                game_id=game_id, day_key=round_row.day_key, player_id=player_id,
                slot_index=round_row.winners_count, guessed_number=guess_value,
                player_nick=verified_nick,
                prize_order_id=prize_order.id if prize_order else None,
            ))

            closed_for_today = round_row.winners_count >= config.winners_per_day
            if not closed_for_today:
                # Nuevo número secreto para el siguiente cupo: distinto a TODOS
                # los que ya ganaron hoy, no solo al inmediatamente anterior
                # (con eso solo, un número ya premiado hoy podía volver a salir
                # como secreto en un cupo más adelante).
                used_today = {
                    row[0] for row in
                    db.session.query(PromoGuessWinner.guessed_number)
                    .filter_by(game_id=game_id, day_key=round_row.day_key)
                    .all()
                }
                available = [n for n in range(1, config.number_max + 1) if n not in used_today]
                round_row.secret_number = random.choice(available) if available else random.randint(1, config.number_max)
            round_row.updated_at = datetime.utcnow()
            db.session.commit()

            return {
                'won': True,
                'slot_index': round_row.winners_count,
                'reward_label': config.package.name if config.package else '',
                'closed_for_today': closed_for_today,
            }

        db.session.commit()
        return {
            'won': False,
            # Ya no se dice si el número real es mayor o menor: con esa pista,
            # entre varias personas comparando resultados (o generando IDs falsos
            # para sacar más intentos) le hacían búsqueda binaria al número
            # secreto y lo sacaban en un puñado de intentos. Ahora solo se les
            # da ánimo genérico, sin información real para acorralar el número.
            'hint': 'cerca',
            'attempts_remaining': remaining_attempts,
        }
    finally:
        release_lock(lock_key, lock_holder)


GUESS_HISTORY_DAYS = 2  # días que muestra el historial de Adivina el Número


def get_guess_public_state(game_id, player_id):
    config = get_guess_config(game_id)
    if not config:
        return {'enabled': False}

    round_row = _get_or_create_round(game_id, config.number_max)
    db.session.commit()

    player_id = str(player_id or '').strip()
    attempts_used = 0
    already_won = False
    if player_id:
        attempt = PromoGuessAttempt.query.filter_by(
            game_id=game_id, day_key=round_row.day_key, player_id=player_id,
        ).first()
        attempts_used = attempt.attempts_used if attempt else 0
        already_won = bool(PromoGuessWinner.query.filter_by(
            game_id=game_id, day_key=round_row.day_key, player_id=player_id,
        ).first())

    winners_today = (
        PromoGuessWinner.query
        .filter_by(game_id=game_id, day_key=round_row.day_key)
        .order_by(PromoGuessWinner.slot_index.asc())
        .all()
    )
    # Solo los 2 días más recientes con ganadores (antes de hoy; hoy sale
    # aparte en "Ganadores de hoy"). Antes eran las últimas 30 filas, que
    # abarcaban muchos días.
    recent_day_keys = [
        row[0] for row in
        db.session.query(PromoGuessWinner.day_key)
        .filter(PromoGuessWinner.game_id == game_id, PromoGuessWinner.day_key < round_row.day_key)
        .distinct()
        .order_by(PromoGuessWinner.day_key.desc())
        .limit(GUESS_HISTORY_DAYS)
        .all()
    ]
    history = (
        PromoGuessWinner.query
        .filter(PromoGuessWinner.game_id == game_id, PromoGuessWinner.day_key.in_(recent_day_keys))
        .order_by(PromoGuessWinner.day_key.desc(), PromoGuessWinner.slot_index.asc())
        .all()
    ) if recent_day_keys else []
    history_by_day = {}
    for w in history:
        history_by_day.setdefault(w.day_key, []).append({
            'player_id': w.player_id,
            'player_nick': w.player_nick,
            'guessed_number': w.guessed_number,
            'won_at': format_ve(w.created_at, '%d/%m/%Y %H:%M:%S'),
        })

    # Medianoche Venezuela del día siguiente: es cuando se reinician los
    # cupos, así el frontend arma la cuenta regresiva una vez se agotan.
    now = now_ve()
    next_reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    return {
        'enabled': True,
        'number_max': config.number_max,
        'max_attempts': config.max_attempts,
        'winners_per_day': config.winners_per_day,
        'reward_label': config.package.name if config.package else '',
        'attempts_used': attempts_used,
        'attempts_remaining': max(0, config.max_attempts - attempts_used),
        'already_won': already_won,
        'closed_for_today': round_row.winners_count >= config.winners_per_day,
        'next_reset_at': next_reset.isoformat(),
        'winners_today': [{
            'player_id': w.player_id,
            'player_nick': w.player_nick,
            'guessed_number': w.guessed_number,
            'won_at': format_ve(w.created_at, '%d/%m/%Y %H:%M:%S'),
        } for w in winners_today],
        'history': [{'day_key': day, 'winners': items} for day, items in history_by_day.items()],
    }


# ─── Hordas de Diamantes (ranking semanal) ───────────────────────────────────

# Tope de diamantes aceptados por segundo real de partida (medido por el
# servidor, no por el teléfono). Simulando partidas, un jugador normal
# promedia ~1 por segundo y ~1.1 por enemigo; con todos los poderes al
# máximo llega a ~5.3/s (con la dificultad +25% salen más enemigos) y ~1.6
# por enemigo. El margen solo corta puntajes inventados a mano.
HORDE_MAX_DIAMONDS_PER_SECOND = 7
HORDE_MAX_DIAMONDS_PER_KILL = 4
HORDE_MAX_RUN_SECONDS = 30 * 60
HORDE_RANKING_SIZE = 10

# ─── Oleadas: copia exacta de las reglas del juego (hordas.html) ────────────
# El juego tiene 10 oleadas. La 10 es la "oleada final": imposible de ganar
# y sin diamantes. Si alguien dice haberla ganado, hizo trampa.
# Qué enemigos salen en cada oleada lo decide una semilla que da el servidor
# (mismo generador que el juego), así el servidor sabe cuántos diamantes
# existen de verdad en cada partida y no acepta ni uno más.
# Si se cambian estas reglas en el juego hay que cambiarlas aquí también.
HORDE_MAX_WAVE = 10
HORDE_DIFFICULTY = 1.25
# Tamaño fijo del campo (FIELD_W/FIELD_H en el juego): igual para todos,
# así el zoom del navegador no agranda el campo ni aleja a los monstruos.
HORDE_FIELD_W = 400
HORDE_FIELD_H = 800
# Versión de reglas vigente (RULES_VERSION en el juego). Una partida nueva
# con reglas viejas (más fáciles, o moviéndose con el dedo en vez del
# joystick) no suma: sería un juego modificado.
HORDE_RULES_VERSION = 4
HORDE_HARD_FROM_WAVE = 6   # desde aquí los monstruos salen más seguido (reglas 2)
HORDE_DROPS = {'grunt': 1, 'runner': 1, 'tank': 3, 'boss': 10, 'shooter': 2}
HORDE_BOSS_MAX_SUMMONS = 4      # cada invocación son 3 esbirros de 1 💎
HORDE_MINION_DROPS = HORDE_BOSS_MAX_SUMMONS * 3


def _mulberry32(seed):
    """Generador idéntico al del juego (aritmética de 32 bits)."""
    a = seed & 0xFFFFFFFF

    def imul(x, y):
        return (x * y) & 0xFFFFFFFF

    def rng():
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = imul(a ^ (a >> 15), 1 | a)
        t = ((t + imul(t ^ (t >> 7), 61 | t)) & 0xFFFFFFFF) ^ t
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296
    return rng


def _horde_wave_quota(wave):
    return 10 + wave * 5


def _horde_pick_type(wave, rng):
    if wave >= 5 and rng() < min(0.3, 0.18 + (wave - 5) * 0.03):
        return 'shooter'
    roll = rng()
    if wave >= 3 and roll < min(0.25, 0.05 * wave):
        return 'tank'
    if wave >= 2 and roll < 0.5:
        return 'runner'
    return 'grunt'


def horde_max_diamonds_by_wave(seed):
    """Lista acumulada: [0, máx. hasta la oleada 1, ..., hasta la 9]. La
    oleada final no suelta diamantes."""
    rng = _mulberry32(seed)
    totals = [0]
    for wave in range(1, HORDE_MAX_WAVE):
        quota = _horde_wave_quota(wave)
        boss_pending = wave % 5 == 0
        wave_total = 0
        for spawned in range(quota):
            if boss_pending and spawned >= quota // 2:
                wave_total += HORDE_DROPS['boss'] + HORDE_MINION_DROPS
                boss_pending = False
            else:
                wave_total += HORDE_DROPS[_horde_pick_type(wave, rng)]
        totals.append(totals[-1] + wave_total)
    return totals


def horde_min_seconds_by_wave():
    """Segundos mínimos para que TERMINEN de salir los enemigos de las
    oleadas 1..n (sin contar el tiempo de matarlos ni de elegir mejoras)."""
    totals = [0.0]
    for wave in range(1, HORDE_MAX_WAVE):
        # Con las reglas 2 salen un 6% más seguido por oleada desde la 6.
        faster = 1 + max(0, wave - (HORDE_HARD_FROM_WAVE - 1)) * 0.06
        interval = max(0.16, 0.9 - wave * 0.06) / HORDE_DIFFICULTY / faster
        totals.append(totals[-1] + 0.6 + (_horde_wave_quota(wave) - 1) * interval)
    return totals


def _horde_week_start(now=None):
    """Lunes (fecha, hora Venezuela) de la semana en curso."""
    today = (now or now_ve()).date()
    return today - timedelta(days=today.weekday())


def _horde_week_key(now=None):
    return _horde_week_start(now).strftime('%Y-%m-%d')


def mask_player_id(player_id):
    value = str(player_id or '')
    if len(value) <= 4:
        return value[:1] + '*' * max(0, len(value) - 1)
    if len(value) <= 7:
        return value[:2] + '*' * (len(value) - 4) + value[-2:]
    return value[:3] + '***' + value[-3:]


HORDE_CHARACTER_SLOTS = 3
HORDE_CHARACTER_MODES = ('rotate', 'rotate_right', 'flip')


def horde_character_keys(slot):
    """Claves de Setting de un personaje. El 1 usa las claves de cuando solo
    había un personaje, así la imagen que ya estaba subida no se pierde."""
    suffix = '' if slot == 1 else f'_{slot}'
    return {
        'image': f'hordas_player_image{suffix}',
        'mode': f'hordas_player_image_mode{suffix}',
        'name': f'hordas_player_name_{slot}',
        'cover': f'hordas_player_cover_{slot}',
    }


def get_horde_characters():
    """Los 3 puestos de personaje, tengan o no imagen cargada."""
    keys = [horde_character_keys(s) for s in range(1, HORDE_CHARACTER_SLOTS + 1)]
    wanted = [k for slot_keys in keys for k in slot_keys.values()]
    values = {row.key: row.value or '' for row in Setting.query.filter(Setting.key.in_(wanted)).all()}
    characters = []
    for slot, slot_keys in enumerate(keys, start=1):
        mode = values.get(slot_keys['mode'])
        characters.append({
            'slot': slot,
            'image': values.get(slot_keys['image'], ''),
            'mode': mode if mode in HORDE_CHARACTER_MODES else 'flip',
            'name': values.get(slot_keys['name'], '').strip() or f'Personaje {slot}',
            # Portada: la que se ve al elegir. Si no hay, se muestra el diseño.
            'cover': values.get(slot_keys['cover'], ''),
        })
    return characters


def get_horde_config(game_id):
    return PromoHordeConfig.query.filter_by(game_id=game_id, is_active=True).first()


def get_horde_enabled_games():
    configs = PromoHordeConfig.query.filter_by(is_active=True).all()
    order_by_game = {c.game_id: (c.sort_order or 100) for c in configs}
    if not order_by_game:
        return []
    games = (
        Game.query
        .filter(Game.id.in_(order_by_game.keys()), Game.is_active.is_(True))
        .all()
    )
    games.sort(key=lambda g: (order_by_game.get(g.id, 100), g.name.lower()))
    return games


HORDE_BANNED_MESSAGE = 'Este ID está bloqueado en Hordas de Diamantes para este juego.'


def is_horde_banned(game_id, player_id):
    player_id = str(player_id or '').strip()
    if not player_id:
        return False
    return PromoHordeBan.query.filter_by(game_id=game_id, player_id=player_id).first() is not None


def get_horde_week_ranking(game_id, week_key, limit=HORDE_RANKING_SIZE):
    """[(player_id, nick, total_diamonds)] de la semana, de mayor a menor.
    Empate: gana quien llegó primero a ese total (su última partida
    terminó antes). Los ID bloqueados no aparecen (ni ganan premio)."""
    total = db.func.sum(PromoHordeRun.diamonds)
    last_finish = db.func.max(PromoHordeRun.finished_at)
    banned = db.session.query(PromoHordeBan.player_id).filter(PromoHordeBan.game_id == game_id)
    query = (
        db.session.query(PromoHordeRun.player_id, db.func.max(PromoHordeRun.player_nick), total)
        .filter(
            PromoHordeRun.game_id == game_id,
            PromoHordeRun.week_key == week_key,
            PromoHordeRun.finished_at.isnot(None),
            ~PromoHordeRun.player_id.in_(banned),
        )
        .group_by(PromoHordeRun.player_id)
        .having(total > 0)
        .order_by(total.desc(), last_finish.asc())
    )
    if limit:
        query = query.limit(limit)
    return [(pid, nick, int(t or 0)) for pid, nick, t in query.all()]


class HordeOutdatedPage(ValueError):
    """La página del juego abierta es de una versión vieja (se actualizó el
    juego mientras la tenía abierta): se recarga antes de jugar, así nadie
    juega una partida que después se anularía por 'reglas viejas'."""


class HordeNotEnoughPoints(ValueError):
    """Faltan puntos para la partida extra (la página muestra un popup)."""

    def __init__(self, message, balance):
        super().__init__(message)
        self.balance = balance


def _horde_free_runs_today(game_id, player_id, day_key):
    return PromoHordeRun.query.filter_by(
        game_id=game_id, player_id=player_id, day_key=day_key, points_spent=0,
    ).count()


def start_horde_run(game_id, player_id, ip='', use_points=False, rules=None):
    """Abre una partida y devuelve su token. Lanza ValueError con un
    mensaje listo para mostrar si no se puede jugar.

    Al acabarse las partidas gratis del día se puede abrir una extra
    canjeando puntos del saldo de ese ID (si el juego lo tiene activo). El
    descuento de puntos y la partida se guardan en la misma transacción:
    nunca se cobran puntos sin que se abra la partida."""
    config = get_horde_config(game_id)
    if not config:
        raise ValueError('Este juego no está activo en este momento.')

    player_id = str(player_id or '').strip()
    if not player_id:
        raise ValueError('Ingresa tu ID de juego.')
    if len(player_id) > 40:
        raise ValueError('Ese ID no es válido.')
    if is_horde_banned(game_id, player_id):
        raise ValueError(HORDE_BANNED_MESSAGE)
    try:
        page_rules = int(rules) if rules is not None else None
    except (TypeError, ValueError):
        page_rules = None
    if page_rules != HORDE_RULES_VERSION:
        raise HordeOutdatedPage('El juego se actualizó. Recargando para jugar con la versión nueva…')

    day_key = today_ve_str()
    week_key = _horde_week_key()
    runs_per_day = config.runs_per_day or 5

    lock_key = f'horde_start:{game_id}:{player_id}'
    lock_holder = uuid4().hex
    if not acquire_lock(lock_key, 15, lock_holder):
        raise ValueError('Tu partida anterior todavía se está abriendo. Espera unos segundos.')

    try:
        runs_today = _horde_free_runs_today(game_id, player_id, day_key)
        points_cost = 0
        if runs_today >= runs_per_day:
            extra_cost = int(config.points_per_extra_run or 0)
            if extra_cost <= 0:
                raise ValueError(f'Ya usaste tus {runs_per_day} partidas de hoy con este ID. Vuelve mañana.')
            if not use_points:
                raise ValueError(f'Ya usaste tus {runs_per_day} partidas gratis de hoy. Puedes jugar otra canjeando {extra_cost} puntos.')
            points_cost = extra_cost

        # La verificación real cuesta una consulta externa: basta con hacerla
        # una vez por semana por ID, después se reutiliza el nombre ya
        # verificado de sus partidas anteriores.
        known = (
            PromoHordeRun.query
            .filter(
                PromoHordeRun.game_id == game_id,
                PromoHordeRun.player_id == player_id,
                PromoHordeRun.week_key == week_key,
                PromoHordeRun.player_nick.isnot(None),
            )
            .first()
        )
        nick = known.player_nick if known else None
        if not known:
            ok, error, nick = _verify_id_if_needed(game_id, player_id, config.require_verification)
            if not ok:
                raise ValueError(error)

        points_balance = None
        if points_cost:
            # Descuento atómico: solo pasa si el saldo alcanza en ese instante,
            # aunque el mismo ID esté gastando puntos en otra parte a la vez.
            spent = db.session.execute(
                update(PlayerPoints)
                .where(
                    PlayerPoints.game_id == game_id,
                    PlayerPoints.player_id == player_id,
                    PlayerPoints.points_balance >= points_cost,
                )
                .values(points_balance=PlayerPoints.points_balance - points_cost, updated_at=datetime.utcnow())
            ).rowcount
            if spent != 1:
                db.session.rollback()
                balance = get_player_points_balance(game_id, player_id)
                raise HordeNotEnoughPoints(
                    f'No tienes puntos suficientes: cada partida extra cuesta {points_cost} puntos y este ID tiene {balance}.',
                    balance,
                )
            points_balance = get_player_points_balance(game_id, player_id)

        run = PromoHordeRun(
            token=uuid4().hex, game_id=game_id, player_id=player_id, player_nick=nick,
            week_key=week_key, day_key=day_key, ip=(ip or '')[:64],
            started_at=datetime.utcnow(), points_spent=points_cost,
            seed=secrets.randbelow(2 ** 31),
        )
        db.session.add(run)
        db.session.commit()
        return {
            'token': run.token,
            'seed': run.seed,
            'player_nick': nick,
            'runs_left_today': max(0, runs_per_day - runs_today - (0 if points_cost else 1)),
            'points_spent': points_cost,
            'points_balance': points_balance,
        }
    finally:
        release_lock(lock_key, lock_holder)


HORDE_REPLAY_MAX_BYTES = 400 * 1024          # una partida de 30 min cabe de sobra
HORDE_REPLAY_WEEK_MAX_BYTES = 300 * 1024 * 1024
HORDE_REPLAY_KEEP_TOP = 5
HORDE_REPLAY_SAFETY_TOP = 20


def _store_horde_replay(run, replay):
    """Guarda la grabación de la partida si viene bien formada. Nunca hace
    fallar el cierre de la partida: si algo no cuadra, simplemente no se
    guarda (en el admin se verá 'sin repetición')."""
    if not isinstance(replay, dict) or replay.get('v') != 1:
        return
    data = replay.get('data')
    try:
        width, height = int(replay.get('W')), int(replay.get('H'))
        slot = int(replay.get('ch') or 0)
        rules = min(max(int(replay.get('r') or 1), 1), 99)
    except (TypeError, ValueError):
        return
    if not isinstance(data, str) or not data or len(data) > HORDE_REPLAY_MAX_BYTES:
        return
    if not (100 <= width <= 5000 and 100 <= height <= 5000):
        return

    # Freno de espacio: si esta semana ya se guardó demasiado, solo se
    # guardan las de quienes están cerca del top (los demás no pueden ganar).
    week_bytes = (
        db.session.query(db.func.coalesce(db.func.sum(PromoHordeReplay.size), 0))
        .filter(PromoHordeReplay.game_id == run.game_id, PromoHordeReplay.week_key == run.week_key)
        .scalar()
    )
    if week_bytes + len(data) > HORDE_REPLAY_WEEK_MAX_BYTES:
        top = get_horde_week_ranking(run.game_id, run.week_key, limit=HORDE_REPLAY_SAFETY_TOP)
        if run.player_id not in {pid for pid, _nick, _total in top}:
            return

    db.session.add(PromoHordeReplay(
        run_id=run.id, game_id=run.game_id, week_key=run.week_key, player_id=run.player_id,
        width=width, height=height, character_slot=slot, rules=rules, data=data, size=len(data),
    ))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def _replay_rules(replay):
    try:
        return int(replay.get('r') or 1)
    except (TypeError, ValueError):
        return None


def _replay_field_size(replay):
    try:
        return int(replay.get('W')), int(replay.get('H'))
    except (TypeError, ValueError):
        return None


def cleanup_horde_replays():
    """Al cerrar cada semana deja solo las grabaciones del top 5 (por si
    alguno del top 3 hizo trampa y hay que correr los puestos) y borra el
    resto. Se marca cada semana ya limpia para no repetir el trabajo."""
    current_week = _horde_week_key()
    pending = (
        db.session.query(PromoHordeReplay.game_id, PromoHordeReplay.week_key)
        .filter(PromoHordeReplay.week_key < current_week)
        .distinct()
        .all()
    )
    for game_id, week_key in pending:
        marker = f'hordas_replays_clean:{game_id}:{week_key}'
        if Setting.query.filter_by(key=marker).first():
            continue
        keep = {pid for pid, _nick, _total in get_horde_week_ranking(game_id, week_key, limit=HORDE_REPLAY_KEEP_TOP)}
        query = PromoHordeReplay.query.filter(
            PromoHordeReplay.game_id == game_id, PromoHordeReplay.week_key == week_key,
        )
        if keep:
            query = query.filter(~PromoHordeReplay.player_id.in_(keep))
        query.delete(synchronize_session=False)
        db.session.add(Setting(key=marker, value=datetime.utcnow().isoformat()))
        db.session.commit()


def finish_horde_run(token, diamonds, kills, wave=None, final_cleared=False, replay=None):
    """Cierra una partida una sola vez y guarda los diamantes aceptados.

    Topes, del más fácil de pasar al más estricto:
    - por tiempo (7/s) y por enemigo (4 c/u), como antes;
    - por oleada: nunca más diamantes de los que existen en las oleadas
      que alcanzó (según la semilla de la partida);
    - por velocidad: si dice ir en una oleada a la que no se puede llegar
      en el tiempo que duró, se toma la que sí era posible y se marca;
    - ganar la oleada final (imposible) o pasar de la 10 = trampa: 0 💎."""
    token = str(token or '').strip()
    run = PromoHordeRun.query.filter_by(token=token).first() if token else None
    if not run:
        raise ValueError('Partida no encontrada.')
    if run.finished_at:
        raise ValueError('Esta partida ya fue registrada.')

    try:
        diamonds = max(0, int(diamonds))
        kills = max(0, int(kills))
        wave = int(wave) if wave is not None else HORDE_MAX_WAVE - 1
    except (TypeError, ValueError):
        raise ValueError('Datos de la partida inválidos.')

    now = datetime.utcnow()
    elapsed = min((now - run.started_at).total_seconds(), HORDE_MAX_RUN_SECONDS)
    cap = min(
        int(max(0.0, elapsed) * HORDE_MAX_DIAMONDS_PER_SECOND),
        kills * HORDE_MAX_DIAMONDS_PER_KILL,
    )

    flag = None
    if final_cleared or wave > HORDE_MAX_WAVE:
        flag = 'gano_oleada_final'
        cap = 0
    elif isinstance(replay, dict) and _replay_field_size(replay) != (HORDE_FIELD_W, HORDE_FIELD_H):
        # Campo de otro tamaño = juego modificado (o una versión vieja de
        # la página abierta desde antes del cambio): no suma.
        flag = 'campo_distinto'
        cap = 0
    elif isinstance(replay, dict) and _replay_rules(replay) != HORDE_RULES_VERSION:
        flag = 'reglas_viejas'
        cap = 0
    elif run.seed is not None:
        # Oleada a la que de verdad se puede llegar en el tiempo que duró:
        # la que está en curso cuenta completa (hasta la 9; la 10 no da 💎).
        min_seconds = horde_min_seconds_by_wave()
        reachable = 1
        while reachable < HORDE_MAX_WAVE and min_seconds[reachable] <= elapsed:
            reachable += 1
        claimed_wave = max(1, wave)
        if claimed_wave > reachable:
            flag = 'demasiado_rapido'
        counted_wave = min(claimed_wave, reachable, HORDE_MAX_WAVE - 1)
        max_by_wave = horde_max_diamonds_by_wave(run.seed)
        cap = min(cap, max_by_wave[counted_wave])
        if not flag and diamonds > max_by_wave[HORDE_MAX_WAVE - 1]:
            flag = 'mas_diamantes_que_los_posibles'
    accepted = min(diamonds, cap)

    # UPDATE condicional: si llegan dos cierres a la vez (doble envío,
    # sendBeacon al salir + el normal), solo uno cuenta.
    updated = (
        PromoHordeRun.query
        .filter(PromoHordeRun.id == run.id, PromoHordeRun.finished_at.is_(None))
        .update({
            'finished_at': now, 'diamonds': accepted,
            'claimed_diamonds': diamonds, 'kills': kills,
            'wave': min(max(0, wave), 99), 'flag': flag,
        }, synchronize_session=False)
    )
    db.session.commit()
    if not updated:
        raise ValueError('Esta partida ya fue registrada.')
    _store_horde_replay(run, replay)

    ranking = get_horde_week_ranking(run.game_id, run.week_key, limit=None)
    my_total = 0
    my_rank = None
    for index, (pid, _nick, total) in enumerate(ranking, start=1):
        if pid == run.player_id:
            my_total, my_rank = total, index
            break
    return {
        'diamonds': accepted,
        'capped': accepted < diamonds,
        'flagged': bool(flag),
        'week_total': my_total,
        'week_rank': my_rank,
    }


def get_horde_player_position(game_id, player_id):
    """Puesto de un ID en el ranking de esta semana, con cuántos diamantes
    le faltan para pasar al de arriba (como el ranking de recargas)."""
    player_id = str(player_id or '').strip()
    if not player_id or not get_horde_config(game_id):
        return None
    full_ranking = get_horde_week_ranking(game_id, _horde_week_key(), limit=None)
    for index, (pid, nick, total) in enumerate(full_ranking, start=1):
        if pid != player_id:
            continue
        above_total = full_ranking[index - 2][2] if index > 1 else None
        # En empate gana quien llegó primero, así que hay que superarlo por 1.
        missing = (above_total - total + 1) if above_total is not None else 0
        progress = 100 if above_total is None else int(min(100, total * 100 / max(1, above_total + 1)))
        return {
            'place': index,
            'nick': nick or 'Jugador',
            'player_id': mask_player_id(pid),
            'diamonds': total,
            'missing': missing,
            'progress_percent': progress,
            'total_players': len(full_ranking),
        }
    return None


def get_horde_public_state(game_id, player_id):
    config = get_horde_config(game_id)
    if not config:
        return {'enabled': False}

    player_id = str(player_id or '').strip()
    week_key = _horde_week_key()
    week_start = _horde_week_start()
    now = now_ve()
    week_ends_at = now.replace(
        year=week_start.year, month=week_start.month, day=week_start.day,
        hour=0, minute=0, second=0, microsecond=0,
    ) + timedelta(days=7)

    full_ranking = get_horde_week_ranking(game_id, week_key, limit=None)
    ranking = [{
        'place': index,
        'nick': nick or 'Jugador',
        'player_id': mask_player_id(pid),
        'diamonds': total,
        'is_me': bool(player_id) and pid == player_id,
    } for index, (pid, nick, total) in enumerate(full_ranking[:HORDE_RANKING_SIZE], start=1)]

    my_total = 0
    my_rank = None
    runs_left_today = config.runs_per_day or 5
    points_balance = None
    if player_id:
        for index, (pid, _nick, total) in enumerate(full_ranking, start=1):
            if pid == player_id:
                my_total, my_rank = total, index
                break
        runs_today = _horde_free_runs_today(game_id, player_id, today_ve_str())
        runs_left_today = max(0, (config.runs_per_day or 5) - runs_today)
        points_balance = get_player_points_balance(game_id, player_id)

    past_winners = (
        PromoHordeWinner.query
        .filter_by(game_id=game_id, status='approved')
        .order_by(PromoHordeWinner.week_key.desc(), PromoHordeWinner.place.asc())
        .limit(9)
        .all()
    )

    return {
        'enabled': True,
        'reward_label': config.package.name if config.package else '',
        'prizes': [{'place': place, 'label': pkg.name} for place, pkg in config.place_packages()],
        'runs_per_day': config.runs_per_day or 5,
        'runs_left_today': runs_left_today,
        'banned': bool(player_id) and is_horde_banned(game_id, player_id),
        'extra_run_cost': int(config.points_per_extra_run or 0),
        'points_balance': points_balance,
        'week_key': week_key,
        'week_ends_at': week_ends_at.isoformat(),
        'ranking': ranking,
        'total_players': len(full_ranking),
        'my_total': my_total,
        'my_rank': my_rank,
        'past_winners': [{
            'week_key': w.week_key,
            'place': w.place,
            'nick': w.player_nick or 'Jugador',
            'player_id': mask_player_id(w.player_id),
            'diamonds': w.diamonds,
        } for w in past_winners],
    }


def _fill_pending_horde_winners(config, week_key):
    """Arma (o rearma) los ganadores PENDIENTES de una semana según el
    ranking, que ya excluye a los bloqueados. Los ya aprobados se respetan
    tal cual; los demás puestos con premio se llenan con los siguientes del
    ranking. Así, si se rechaza al 1ro, el 2do pasa a 1ro, el 4to entra
    como 3ro, etc. No entrega nada: el premio sale al aprobar."""
    prizes = dict(config.place_packages()) or {1: None}
    winners = PromoHordeWinner.query.filter_by(game_id=config.game_id, week_key=week_key).all()
    approved = [w for w in winners if w.status == 'approved']
    for w in winners:
        if w.status != 'approved':
            db.session.delete(w)
    db.session.flush()

    # Cada jugador ocupa su puesto real del ranking (aunque ese puesto no
    # tenga premio): si el 2do no tiene premio, el premio del 3ro es para el
    # 3ro, no para el 2do. Los puestos ya aprobados quedan fijos.
    taken_places = {w.place for w in approved}
    taken_players = {w.player_id for w in approved}
    last_place = max(prizes)
    place = 1
    for pid, nick, total in get_horde_week_ranking(config.game_id, week_key, limit=last_place + len(approved)):
        if pid in taken_players:
            continue
        while place in taken_places:
            place += 1
        if place > last_place:
            break
        if place in prizes:
            package = prizes[place]
            db.session.add(PromoHordeWinner(
                game_id=config.game_id, week_key=week_key, place=place,
                player_id=pid, player_nick=nick, diamonds=total,
                status='pending', package_id=package.id if package else None,
            ))
        place += 1


def run_weekly_horde_awards():
    """Cierra la semana que acaba de terminar (lunes a domingo, hora
    Venezuela) en cada juego activo: deja a los ganadores PENDIENTES DE
    APROBACIÓN. El premio no se entrega hasta que el admin revise las
    repeticiones y apruebe. Idempotente: si esa semana ya tiene ganadores,
    no hace nada; el lock evita dos cierres simultáneos."""
    previous_week_key = (_horde_week_start() - timedelta(days=7)).strftime('%Y-%m-%d')
    for config in PromoHordeConfig.query.filter_by(is_active=True).all():
        lock_key = f'horde_award:{config.game_id}:{previous_week_key}'
        lock_holder = uuid4().hex
        if not acquire_lock(lock_key, 120, lock_holder):
            continue
        try:
            if PromoHordeWinner.query.filter_by(game_id=config.game_id, week_key=previous_week_key).first():
                continue
            _fill_pending_horde_winners(config, previous_week_key)
            db.session.commit()
        finally:
            release_lock(lock_key, lock_holder)


def approve_horde_winner(winner_id):
    """Aprueba un ganador pendiente y le entrega el premio en ese momento.
    El cambio de estado es condicional: aunque se toque 'Aprobar' dos veces
    seguidas, el premio sale una sola vez."""
    from .order_processing import deliver_prize_to_player

    winner = PromoHordeWinner.query.get(winner_id)
    if not winner:
        raise ValueError('Ganador no encontrado.')
    claimed = (
        PromoHordeWinner.query
        .filter(PromoHordeWinner.id == winner.id, PromoHordeWinner.status == 'pending')
        .update({'status': 'approving'}, synchronize_session=False)
    )
    db.session.commit()
    if not claimed:
        raise ValueError('Este premio ya fue revisado.')
    try:
        prize_order = None
        package = Package.query.get(winner.package_id) if winner.package_id else None
        if package:
            prize_order, _approval = deliver_prize_to_player(
                Game.query.get(winner.game_id), package, winner.player_id,
                note=f'Premio Hordas de Diamantes — puesto #{winner.place} semana del {winner.week_key} ({winner.diamonds} diamantes). Aprobado tras revisar repeticiones.',
                reference_prefix='HORDAS',
            )
        winner = PromoHordeWinner.query.get(winner_id)
        winner.status = 'approved'
        winner.reviewed_at = datetime.utcnow()
        winner.prize_order_id = prize_order.id if prize_order else None
        db.session.commit()
        return winner
    except Exception:
        db.session.rollback()
        PromoHordeWinner.query.filter_by(id=winner_id).update({'status': 'pending'}, synchronize_session=False)
        db.session.commit()
        raise


def reject_horde_winner(winner_id, reason=''):
    """Rechaza un ganador pendiente por trampa: se bloquea su ID en este
    juego (sale del ranking) y los siguientes suben de puesto."""
    winner = PromoHordeWinner.query.get(winner_id)
    if not winner or winner.status != 'pending':
        raise ValueError('Este premio ya fue revisado.')
    config = PromoHordeConfig.query.filter_by(game_id=winner.game_id).first()
    lock_key = f'horde_award:{winner.game_id}:{winner.week_key}'
    lock_holder = uuid4().hex
    if not acquire_lock(lock_key, 60, lock_holder):
        raise ValueError('Se está revisando este premio en otra pestaña. Intenta de nuevo.')
    try:
        if not is_horde_banned(winner.game_id, winner.player_id):
            db.session.add(PromoHordeBan(
                game_id=winner.game_id, player_id=winner.player_id,
                reason=(reason or f'Premio rechazado (semana {winner.week_key})')[:200],
            ))
            db.session.flush()
        if config:
            _fill_pending_horde_winners(config, winner.week_key)
        else:
            db.session.delete(winner)
        db.session.commit()
    finally:
        release_lock(lock_key, lock_holder)


def get_pending_horde_winners(game_id=None):
    query = PromoHordeWinner.query.filter(PromoHordeWinner.status == 'pending')
    if game_id:
        query = query.filter(PromoHordeWinner.game_id == game_id)
    return query.order_by(PromoHordeWinner.week_key.desc(), PromoHordeWinner.place.asc()).all()
