"""Lógica de las 3 promociones de engagement: Recarga Acumulada (barra por
niveles), Sorteo Diario (rifa con ticket por ID) y Adivina el Número.

Los tres comparten el mismo mecanismo de entrega de premio que el resto de
la tienda (deliver_prize_to_player: orden interna de $0 por el bot/stock
configurado) y, cuando el juego tiene verificación de ID configurada, la
misma verificación real que usa la tienda antes de aceptar un registro o
una jugada — así no se le puede regalar un premio a un ID inventado.
"""
import random
from datetime import datetime, time, timedelta

from .order_units import extract_order_units
from .timezone import now_ve, today_ve_str, format_ve
from ..models import (
    Game, Order, Package,
    PromoAccumulatedAward, PromoAccumulatedLevel, PromoAccumulatedOrderLog, PromoAccumulatedProgress,
    PromoGuessAttempt, PromoGuessConfig, PromoGuessRound, PromoGuessWinner,
    PromoRaffleConfig, PromoRaffleEntry, PromoRaffleWinner,
    db,
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


def _raffle_open_day_key(game_id):
    """A qué día se apunta un registro nuevo en este momento.

    Mientras el sorteo de hoy no se haya corrido, el registro entra al
    pool de hoy. En cuanto ya hay ganadores de hoy, un registro nuevo NO
    puede colarse en un sorteo que ya se resolvió — pasa a contar para el
    sorteo de mañana, tal como se pidió ("Asegura tu Ticket Gratis para el
    Siguiente Sorteo"). Sin esto, cualquiera que se registrara después de
    la hora del sorteo se quedaba con un ticket que nunca se iba a sortear."""
    day_key = today_ve_str()
    already_drawn = PromoRaffleWinner.query.filter_by(game_id=game_id, day_key=day_key).first()
    if already_drawn:
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

    day_key = _raffle_open_day_key(game_id)
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
    sorteo y todavía no tiene ganadores hoy. Pensado para llamarse desde el
    scheduler en segundo plano (igual que la recuperación de órdenes)."""
    configs = PromoRaffleConfig.query.filter_by(is_active=True).all()
    current_time = now_ve().time().replace(second=0, microsecond=0)
    day_key = today_ve_str()

    from .order_processing import deliver_prize_to_player

    for config in configs:
        draw_time = time(config.draw_hour or 21, config.draw_minute or 0)
        if current_time < draw_time:
            continue
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
    history = (
        PromoGuessWinner.query
        .filter(PromoGuessWinner.game_id == game_id, PromoGuessWinner.day_key < round_row.day_key)
        .order_by(PromoGuessWinner.day_key.desc(), PromoGuessWinner.slot_index.asc())
        .limit(30)
        .all()
    )
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
