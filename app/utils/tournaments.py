"""Torneos de 3S Recargas (gratis, por orden de llegada, con salas).

Cómo funciona (pedido del dueño, 2026-10-13):
  * El dueño publica el link del torneo en su canal de WhatsApp y, a la hora, abre las inscripciones desde el admin.
  * Un torneo tiene SALAS (nombre, ID y clave de la sala del juego y cuántos jugadores caben). Para entrar solo hace falta verificar el ID.
    La web mete a cada persona en la PRIMERA sala que todavía tenga lugar: la siguiente sala "se desbloquea" cuando la anterior se llena,
    para que no todos se repartan entre salas distintas.
  * Los datos de la sala (nombre, ID y clave) solo los ve quien ya se inscribió. La web NO puede sacar a nadie de la sala del juego: el
    admin tiene la lista de inscritos de cada sala (ID y nombre del juego) para expulsar a mano a quien no esté en ella.
  * Los premios se definen por puesto (un paquete del juego). Al elegir al ganador de un puesto en una sala y tocar "Entregar", el premio
    sale solo por el mismo camino que los premios del Sorteo y de Hordas (orden interna de $0) y queda en el historial de ganadores.
  * Un ID solo puede estar en UNA sala a la vez. Mientras la sala donde se inscribió no termine, no puede entrar a otra; cuando la sala termina
    (el dueño la termina o se entregaron todos sus premios) el ID queda libre. Quien se sale puede volver tocando su sala y escribiendo su ID.
"""
import math
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.exc import ObjectDeletedError

from ..models import (
    Game, Order, Package, Tournament, TournamentEntry, TournamentPrize, TournamentRoom, TournamentWinner, db,
)
from .locks import acquire_lock, release_lock
from .timezone import VENEZUELA_TIMEZONE, format_ve

STATUS_LABELS = {'draft': 'Preparando', 'open': 'Inscripciones abiertas', 'closed': 'Inscripciones cerradas', 'finished': 'Terminado'}
VISIBLE_STATUSES = ('open', 'closed')       # lo que ven los clientes (preparando y terminado no se muestran)
TITLE_MAX, DESCRIPTION_MAX = 120, 300
ROOM_NAME_MAX, ROOM_CODE_MAX, ROOM_PASSWORD_MAX = 80, 60, 60
MAX_ROOMS, MAX_PLAYERS_LIMIT, MAX_PRIZES = 40, 1000, 20
MAX_PER_IP_LIMIT = 50
JOIN_RATE_PER_MINUTE = 15       # inscripciones por minuto desde una misma conexión (frena a un programa que pruebe IDs)
STATE_RATE_PER_MINUTE = 90      # consultas de la sala de un jugador por minuto desde una misma conexión
PLAYER_ID_RE = re.compile(r'^[A-Za-z0-9._@-]{1,40}$')


class TournamentError(ValueError):
    """Mensaje listo para mostrarle a la persona (cliente o administrador)."""


def _clean(value, limit):
    return ' '.join(str(value or '').split())[:limit]


def _canonical_player_id(raw):
    """El ID sin espacios y, si es solo dígitos, sin ceros a la izquierda: '0123' y '123' son la misma persona (si no, cada escritura
    distinta abría un lugar nuevo, saltándose "un ID por sala" y los quitados del torneo)."""
    player_id = ''.join(str(raw or '').split())
    if player_id.isascii() and player_id.isdigit():
        player_id = player_id.lstrip('0') or '0'
    return player_id


def _clean_player_id(raw):
    player_id = ''.join(str(raw or '').split())
    if not player_id:
        raise TournamentError('Ingresa tu ID de juego.')
    if not PLAYER_ID_RE.match(player_id):
        raise TournamentError('Ese ID no es válido. Revísalo e inténtalo de nuevo.')
    return _canonical_player_id(player_id)


def _int(value, lo, hi, label):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise TournamentError(f'{label}: escribe un número entero.')
    if number < lo or number > hi:
        raise TournamentError(f'{label}: debe estar entre {lo} y {hi}.')
    return number


# ───────────────────────── Verificación de ID (obligatoria en TODOS los torneos) ─────────────────────────
# Regla del dueño: para entrar a un torneo el ID tiene que verificarse con el juego. Con el ID verificado y el nombre que devuelve el juego es que
# se sabe que quien entra a la sala es la misma persona que se inscribió. Si un juego no tiene verificación de ID activa, NO se puede abrir su torneo
# y nadie puede inscribirse (nunca se deja pasar un ID sin comprobar).
NO_VERIFICATION_MESSAGE = ('La verificación de ID no está activa para este juego, así que todavía no se puede inscribir nadie. '
                           'Inténtalo más tarde o escríbenos por soporte.')


def verification_available(game_id):
    """True si el juego tiene verificación de ID configurada y encendida (la misma que usa la tienda)."""
    from ..routes.verify import verifiable_game_ids

    try:
        return int(game_id) in verifiable_game_ids()
    except (TypeError, ValueError):
        return False


def verify_player_strict(game_id, player_id):
    """Verifica el ID con el juego y devuelve el NOMBRE de la cuenta. Sin verificación posible o sin nombre -> TournamentError (no se deja pasar)."""
    from ..routes.verify import verify_player_nick

    if not verification_available(game_id):
        raise TournamentError(NO_VERIFICATION_MESSAGE)
    payload, _status = verify_player_nick(str(player_id), str(game_id), mode='auto')
    if not payload.get('ok'):
        raise TournamentError(payload.get('error') or 'No se pudo verificar tu ID. Revísalo e inténtalo de nuevo.')
    nick = _clean(payload.get('nick'), 150)
    if not nick:
        raise TournamentError('No pudimos confirmar el nombre de tu cuenta. Inténtalo de nuevo en unos segundos.')
    return nick


# ───────────────────────── Horario de apertura (cuenta regresiva) ─────────────────────────
def parse_opens_at(raw):
    """'2026-10-14T20:00' (hora de VENEZUELA, como la manda un campo de fecha y hora) -> datetime en UTC sin zona; vacío -> None."""
    raw = str(raw or '').strip()
    if not raw:
        return None
    local = None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S', '%d/%m/%Y %H:%M'):
        try:
            local = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if local is None or not (2020 <= local.year <= 2100):
        raise TournamentError('La fecha y hora de apertura no es válida. Elige el día y la hora en el calendario.')
    return local.replace(tzinfo=VENEZUELA_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None)


def opens_at_input_value(tournament):
    """El valor para el campo de fecha y hora del admin (hora de Venezuela), o ''."""
    return format_ve(tournament.opens_at, '%Y-%m-%dT%H:%M') if tournament.opens_at else ''


def opens_label(tournament):
    """'14/10/2026 a las 08:00 PM' (hora de Venezuela) o ''."""
    return format_ve(tournament.opens_at, '%d/%m/%Y a las %I:%M %p') if tournament.opens_at else ''


def seconds_until_open(tournament):
    """Segundos que faltan para que se abra (redondeado hacia arriba), o None si no hay hora o ya pasó."""
    if not tournament.opens_at:
        return None
    left = (tournament.opens_at - datetime.utcnow()).total_seconds()
    return int(math.ceil(left)) if left > 0 else None


def is_scheduled(tournament):
    """Publicado ('open') pero todavía no llega su hora: se ve con cuenta regresiva y nadie puede inscribirse aún."""
    return tournament.status == 'open' and seconds_until_open(tournament) is not None


def display_status(tournament):
    """(clave, texto) para mostrarle al dueño: igual que el estado, pero 'Programado' cuando espera su hora."""
    if is_scheduled(tournament):
        return 'scheduled', f'Programado · abre {format_ve(tournament.opens_at, "%d/%m %I:%M %p")}'
    return tournament.status, STATUS_LABELS.get(tournament.status, tournament.status)


def readiness(tournament):
    """Lista de "antes de abrir" para el dueño: [(nivel, texto)], nivel = 'ok' | 'warn' | 'bad'. Los datos de las salas se piden ANTES del torneo
    porque salen de crear la sala en el juego."""
    items = []
    if verification_available(tournament.game_id):
        items.append(('ok', 'La verificación de ID está activa para este juego (todos se inscriben con su ID verificado).'))
    else:
        items.append(('bad', 'La verificación de ID NO está activa para este juego: no se puede abrir el torneo hasta activarla (Configuración → Verificación de ID).'))
    rooms = sorted(tournament.rooms, key=lambda r: (r.position, r.id))
    if not rooms:
        items.append(('bad', 'Falta crear al menos una sala (nombre, ID y clave que te da el juego al crearla).'))
    else:
        items.append(('ok', f'{len(rooms)} sala(s) con {sum(r.max_players for r in rooms)} lugares en total.'))
        for room in rooms:
            if not room.room_password:
                items.append(('warn', f'«{room.name}» no tiene clave. Si la sala del juego sí tiene clave, agrégala antes de abrir.'))
    if tournament.prizes:
        items.append(('ok', f'{len(tournament.prizes)} premio(s) por puesto configurado(s).'))
    else:
        items.append(('warn', 'Todavía no hay premios (puedes agregarlos antes de entregarlos).'))
    if tournament.opens_at:
        if seconds_until_open(tournament) is not None:
            items.append(('ok', f'Se abre solo el {opens_label(tournament)} (hora de Venezuela).'))
        elif tournament.status == 'draft':
            items.append(('warn', f'La hora de apertura ({opens_label(tournament)}) ya pasó: al publicarlo se abre de inmediato.'))
    elif tournament.status == 'draft':
        items.append(('warn', 'No tiene hora de apertura: se abre cuando tú toques «Abrir inscripciones».'))
    return items


# ───────────────────────── Lo que ven los clientes ─────────────────────────
def visible_tournaments():
    return (Tournament.query.filter(Tournament.status.in_(VISIBLE_STATUSES))
            .order_by(Tournament.opened_at.desc(), Tournament.id.desc()).all())


def active_counts(tournament_id):
    """{id de sala: inscritos activos}."""
    rows = (db.session.query(TournamentEntry.room_id, func.count(TournamentEntry.id))
            .filter(TournamentEntry.tournament_id == tournament_id, TournamentEntry.status == 'active')
            .group_by(TournamentEntry.room_id).all())
    return {room_id: count for room_id, count in rows}


def rooms_overview(tournament, counts=None):
    """Las salas en orden con su estado: 'finished' (terminó: ya no recibe gente y no frena a la siguiente), 'full' (llena), 'open' (recibe gente ahora:
    la primera con lugar) o 'locked' (espera a que se llene la anterior)."""
    counts = counts if counts is not None else active_counts(tournament.id)
    rooms, found_open = [], False
    for room in sorted(tournament.rooms, key=lambda r: (r.position, r.id)):
        count = counts.get(room.id, 0)
        if room.finished_at:
            state = 'finished'
        elif count >= room.max_players:
            state = 'full'
        elif not found_open:
            state, found_open = 'open', True
        else:
            state = 'locked'
        rooms.append({'room': room, 'id': room.id, 'position': room.position, 'name': room.name, 'max': room.max_players, 'count': count, 'state': state})
    return rooms


def live_entry(game_id, player_id):
    """La inscripción VIGENTE de ese ID (activa y en una sala que todavía no terminó), en cualquier torneo del mismo juego; None si está libre."""
    player_id = _canonical_player_id(player_id)
    if not player_id:
        return None
    return TournamentEntry.query.filter_by(game_id=game_id, player_id=player_id, status='active', released_at=None).first()


def player_entry(tournament_id, player_id):
    """Lo que ese ID tiene en ESTE torneo: su inscripción vigente; si no hay, la que lo sacaron ('removed'); si no, None (nunca entró o su sala ya terminó)."""
    player_id = _canonical_player_id(player_id)
    if not player_id:
        return None
    entries = TournamentEntry.query.filter_by(tournament_id=tournament_id, player_id=player_id).order_by(TournamentEntry.id.desc()).all()
    live = next((e for e in entries if e.status == 'active' and e.released_at is None), None)
    return live or next((e for e in entries if e.status == 'removed'), None)


def public_state(tournament, player_id='', room_position=None):
    """Estado para la página pública (JSON). Los datos de la sala (ID y clave) SOLO salen si ese jugador ya está inscrito y activo.
    Con `room_position` (la sala que la persona tocó para volver a entrar) los datos solo salen si está inscrito EN ESA sala; si no, `not_in_room`."""
    overview = rooms_overview(tournament)
    total = sum(r['count'] for r in overview)
    capacity = sum(r['max'] for r in overview)
    scheduled = is_scheduled(tournament)
    for r in overview:
        r['base'] = r['state']      # el estado real de cada sala (la página lo usa para saber a cuál se puede entrar)
        if scheduled:               # antes de su hora ninguna sala recibe gente todavía
            r['state'] = 'soon'
    verification_ok = verification_available(tournament.game_id)
    accepting = tournament.status == 'open' and not scheduled and verification_ok and any(r['base'] == 'open' for r in overview)
    state = {
        'id': tournament.id, 'title': tournament.title, 'description': tournament.description or '', 'status': tournament.status,
        'status_label': STATUS_LABELS.get(tournament.status, ''), 'game_id': tournament.game_id, 'game_name': tournament.game.name if tournament.game else '',
        'accepting': accepting, 'all_full': bool(overview) and all(r['base'] in ('full', 'finished') for r in overview),
        'scheduled': scheduled, 'opens_in': seconds_until_open(tournament) if scheduled else None, 'opens_label': opens_label(tournament) if scheduled else '',
        'verification_ok': verification_ok,
        # Cuántas salas hay: en total, cuántas todavía tienen lugar (disponibles), cuántas ya se llenaron y cuántas terminaron
        'rooms_summary': {
            'total': len(overview), 'available': sum(1 for r in overview if r['base'] in ('open', 'locked')),
            'full': sum(1 for r in overview if r['base'] == 'full'), 'finished': sum(1 for r in overview if r['base'] == 'finished'),
        },
        'total': total, 'capacity': capacity,
        'rooms': [{'position': r['position'], 'name': r['name'], 'max': r['max'], 'count': r['count'], 'state': r['state'], 'base': r['base']} for r in overview],
        'prizes': [{'place': p.place, 'name': p.package.name if p.package else ''} for p in tournament.prizes],
        'entry': None,
    }
    entry = player_entry(tournament.id, player_id) if player_id else None
    if entry is not None:
        room = db.session.get(TournamentRoom, entry.room_id) if entry.status != 'removed' else None
        if entry.status == 'removed':
            state['entry'] = {'removed': True}
        elif room_position not in (None, '') and str(room_position) != str(room.position):
            state['not_in_room'] = True      # está inscrito, pero en otra sala: aquí no se le muestra nada
        else:
            state['entry'] = {
                'removed': False, 'player_id': entry.player_id, 'nick': entry.player_nick or '', 'slot': entry.slot_number,
                'room': {'position': room.position, 'name': room.name, 'code': room.room_code, 'password': room.room_password, 'max': room.max_players},
            }
    return state


def _check_chosen_room(overview, room_position):
    """La sala que la persona eligió antes de inscribirse (None = sin elegir: se le asigna la sala activa). Solo se puede entrar a la sala ACTIVA (la primera
    con lugar): si eligió una bloqueada o una llena se le explica por qué y cuál es la activa. Devuelve el dato de la sala elegida o None."""
    if room_position in (None, ''):
        return None
    try:
        position = int(room_position)
    except (TypeError, ValueError):
        raise TournamentError('Elige una sala válida.')
    target = next((r for r in overview if r['position'] == position), None)
    if target is None:
        raise TournamentError('Esa sala no existe.')
    active = next((r for r in overview if r['state'] == 'open'), None)
    if target['state'] == 'open':
        return target
    if target['state'] == 'finished':
        extra = f' La sala activa es {active["name"]}.' if active else ''
        raise TournamentError(f'{target["name"]} ya terminó.{extra}')
    if target['state'] == 'full':
        extra = f' Ahora la sala activa es {active["name"]}: tócala para inscribirte.' if active else ''
        raise TournamentError(f'{target["name"]} ya está llena.{extra}')
    if active is None:
        raise TournamentError(f'No puedes unirte a {target["name"]} todavía.')
    raise TournamentError(f'No puedes unirte a {target["name"]} porque {active["name"]} todavía no está llena. '
                          f'¡Apúrate a entrar a {active["name"]}, que es la sala activa!')


def _asked_room_matches(room, room_position):
    """True si la persona no eligió sala o eligió justo esta."""
    if room_position in (None, ''):
        return True
    try:
        return int(room_position) == room.position
    except (TypeError, ValueError):
        return False


def _busy_error(busy, tournament_id):
    """Mensaje para quien intenta inscribirse teniendo ya una sala vigente (un ID solo puede estar en una sala a la vez)."""
    room = db.session.get(TournamentRoom, busy.room_id)
    where = room.name
    if busy.tournament_id != tournament_id:
        other = db.session.get(Tournament, busy.tournament_id)
        where = f'{room.name} del torneo «{other.title}»' if other else room.name
    return TournamentError(f'Tu ID ya está inscrito en {where} y esa sala todavía no termina. Un ID solo puede estar en una sala a la vez: '
                           f'cuando termine, quedará libre para otra.')


def join_tournament(tournament_id, player_id, ip='', room_position=None):
    """Inscribe a un jugador por orden de llegada. Devuelve (entry, room, ya_estaba_inscrito). Lanza TournamentError con un mensaje claro.
    Si la base de datos está ocupada justo en ese instante (mucha gente a la vez) reintenta sola en vez de mostrarle un error a la persona."""
    for _try in range(4):
        try:
            return _join_once(tournament_id, player_id, ip, room_position)
        except (OperationalError, ObjectDeletedError):
            db.session.rollback()
            time.sleep(0.05)
    raise TournamentError('Hay mucha gente entrando al mismo tiempo. Intenta de nuevo en unos segundos.')


def _join_once(tournament_id, player_id, ip='', room_position=None):
    tournament = db.session.get(Tournament, tournament_id)
    if tournament is None or tournament.status not in VISIBLE_STATUSES:
        raise TournamentError('Este torneo no está disponible.')
    player_id = _clean_player_id(player_id)

    if TournamentEntry.query.filter_by(tournament_id=tournament.id, player_id=player_id, status='removed').first():
        raise TournamentError('No puedes inscribirte en este torneo.')
    # Un ID solo puede estar en UNA sala a la vez (en este torneo o en otro del mismo juego) hasta que esa sala termine.
    busy = live_entry(tournament.game_id, player_id)
    if busy is not None:
        busy_room = db.session.get(TournamentRoom, busy.room_id)
        if busy.tournament_id == tournament.id and _asked_room_matches(busy_room, room_position):
            return busy, busy_room, True      # ya está inscrito en esa sala (o no eligió sala): se le vuelven a mostrar sus datos
        raise _busy_error(busy, tournament.id)
    if tournament.status != 'open':
        raise TournamentError('Las inscripciones de este torneo están cerradas.')
    if is_scheduled(tournament):
        raise TournamentError(f'Todavía no abre: las inscripciones empiezan el {opens_label(tournament)} (hora de Venezuela).')
    overview = rooms_overview(tournament)
    if not overview:
        raise TournamentError('Este torneo todavía no tiene salas. Vuelve en un momento.')
    if not any(r['state'] == 'open' for r in overview):
        raise TournamentError('Se llenaron todos los cupos de este torneo.')
    chosen = _check_chosen_room(overview, room_position)   # antes de verificar el ID: es barato y evita consultas al juego que no hacen falta

    if tournament.max_per_ip and ip:
        used = TournamentEntry.query.filter_by(tournament_id=tournament.id, ip=ip).count()
        if used >= tournament.max_per_ip:
            raise TournamentError('Ya se inscribieron varias personas desde esta conexión. Si es un error, escríbenos por soporte.')

    nick = verify_player_strict(tournament.game_id, player_id)
    game_id = tournament.game_id

    for _attempt in range(10):
        try:
            tournament = db.session.get(Tournament, tournament_id)
            if tournament is None or tournament.status != 'open':
                raise TournamentError('Las inscripciones de este torneo están cerradas.')
            if tournament.max_per_ip and ip:
                if TournamentEntry.query.filter_by(tournament_id=tournament.id, ip=ip).count() >= tournament.max_per_ip:
                    raise TournamentError('Ya se inscribieron varias personas desde esta conexión. Si es un error, escríbenos por soporte.')
            overview = rooms_overview(tournament)
            target = next((r for r in overview if r['state'] == 'open'), None)
            if target is None:
                raise TournamentError('Se llenaron todos los cupos de este torneo.')
            if chosen is not None and target['position'] != chosen['position']:
                raise TournamentError(f'{chosen["name"]} se acaba de llenar. Ahora la sala activa es {target["name"]}: tócala para inscribirte.')
            # Cada sala tiene lugares FIJOS del 1 al máximo y la base de datos no deja repetir uno (restricción única): aunque 50 personas lleguen
            # al mismo tiempo, solo una se queda con el último lugar; las demás vuelven a intentar y caen en la sala siguiente.
            taken = {n for (n,) in db.session.query(TournamentEntry.slot_number).filter(TournamentEntry.room_id == target['id'], TournamentEntry.slot_number > 0).all()}
            seat = next((n for n in range(1, target['max'] + 1) if n not in taken), None)
            if seat is None:
                continue   # justo se llenó: se vuelve a calcular y se pasa a la sala siguiente
            entry = TournamentEntry(
                tournament_id=tournament.id, room_id=target['id'], game_id=game_id, player_id=player_id, player_nick=_clean(nick, 150) or None,
                slot_number=seat, ip=(ip or '')[:64] or None, status='active',
            )
            db.session.add(entry)
            room = target['room']
            db.session.commit()
            return entry, room, False
        except TournamentError:
            db.session.rollback()
            raise
        except (IntegrityError, OperationalError, ObjectDeletedError):
            db.session.rollback()
            # ¿la misma persona tocó dos veces el botón (o se inscribió en otra sala al mismo tiempo)? entonces ya quedó inscrita
            again = live_entry(game_id, player_id)
            if again is not None:
                if again.tournament_id == tournament_id and _asked_room_matches(db.session.get(TournamentRoom, again.room_id), room_position):
                    return again, db.session.get(TournamentRoom, again.room_id), True
                raise _busy_error(again, tournament_id)
            time.sleep(0.02)   # otra persona ganó el lugar (o la base estaba ocupada): se reintenta enseguida
    raise TournamentError('Hay mucha gente entrando al mismo tiempo. Intenta de nuevo en unos segundos.')


# ───────────────────────── Administración ─────────────────────────
def get_game_for_tournament(game_id):
    game = db.session.get(Game, int(game_id)) if str(game_id or '').isdigit() else None
    if game is None:
        raise TournamentError('Elige el juego del torneo.')
    return game


def _max_per_ip(value):
    """Inscripciones por conexión: vacío (o sin mandar) = 5, 0 = sin límite."""
    return _int(5 if value is None or str(value).strip() == '' else value, 0, MAX_PER_IP_LIMIT, 'Inscripciones por conexión')


def create_tournament(title, game_id, description='', max_per_ip=5, opens_at_raw=''):
    title = _clean(title, TITLE_MAX)
    if len(title) < 3:
        raise TournamentError('Ponle un nombre al torneo (mínimo 3 letras).')
    game = get_game_for_tournament(game_id)
    tournament = Tournament(
        title=title, description=_clean(description, DESCRIPTION_MAX), game_id=game.id, status='draft',
        max_per_ip=_max_per_ip(max_per_ip),
        opens_at=parse_opens_at(opens_at_raw),
    )
    db.session.add(tournament)
    db.session.commit()
    return tournament


def update_tournament(tournament, title, description, max_per_ip, opens_at_raw=None):
    title = _clean(title, TITLE_MAX)
    if len(title) < 3:
        raise TournamentError('Ponle un nombre al torneo (mínimo 3 letras).')
    tournament.title = title
    tournament.description = _clean(description, DESCRIPTION_MAX)
    tournament.max_per_ip = _max_per_ip(max_per_ip)
    if opens_at_raw is not None:   # None = no tocar la hora; '' = quitarla
        tournament.opens_at = parse_opens_at(opens_at_raw)
    db.session.commit()


def change_status(tournament, action):
    """action: open | close | finish | draft. Abrir exige al menos una sala."""
    if action in ('open', 'open_now'):
        if not tournament.rooms:
            raise TournamentError('Antes de abrir las inscripciones crea al menos una sala.')
        if not verification_available(tournament.game_id):
            raise TournamentError('No se puede abrir: este juego no tiene la verificación de ID activa y en los torneos el ID siempre se verifica '
                                  '(es lo que permite saber que quien entra a la sala es quien se inscribió). Actívala en Configuración → Verificación de ID.')
        if action == 'open_now':
            tournament.opens_at = None   # abrir YA: se ignora la hora programada
        tournament.status = 'open'
        tournament.opened_at = tournament.opened_at or datetime.utcnow()
        tournament.finished_at = None
    elif action == 'close':
        tournament.status = 'closed'
    elif action == 'finish':
        tournament.status = 'finished'
        tournament.finished_at = datetime.utcnow()
        for room in tournament.rooms:     # al terminar el torneo terminan todas sus salas: los IDs quedan libres
            _finish_room_rows(room)
    elif action == 'draft':
        if TournamentEntry.query.filter_by(tournament_id=tournament.id).first():
            raise TournamentError('Ya hay inscritos: no se puede volver a "Preparando". Ciérralo o termínalo.')
        tournament.status = 'draft'
    else:
        raise TournamentError('Acción no válida.')
    db.session.commit()


def delete_tournament(tournament):
    if TournamentWinner.query.filter_by(tournament_id=tournament.id).first():
        raise TournamentError('Este torneo ya entregó premios: se queda en el historial y no se puede borrar.')
    TournamentEntry.query.filter_by(tournament_id=tournament.id).delete(synchronize_session=False)
    db.session.delete(tournament)
    db.session.commit()


def _room_fields(name, code, password, max_players):
    name = _clean(name, ROOM_NAME_MAX)
    if not name:
        raise TournamentError('Ponle un nombre a la sala.')
    return name, _clean(code, ROOM_CODE_MAX), _clean(password, ROOM_PASSWORD_MAX), _int(max_players, 1, MAX_PLAYERS_LIMIT, 'Máximo de jugadores')


def add_room(tournament, name, code, password, max_players):
    if len(tournament.rooms) >= MAX_ROOMS:
        raise TournamentError(f'Máximo {MAX_ROOMS} salas por torneo.')
    name, code, password, max_players = _room_fields(name, code, password, max_players)
    if not code:
        raise TournamentError('Escribe el ID de la sala (el que los jugadores usan para entrar en el juego).')
    position = (db.session.query(func.max(TournamentRoom.position)).filter(TournamentRoom.tournament_id == tournament.id).scalar() or 0) + 1
    room = TournamentRoom(tournament_id=tournament.id, position=position, name=name, room_code=code, room_password=password, max_players=max_players)
    db.session.add(room)
    db.session.commit()
    return room


def update_room(room, name, code, password, max_players):
    name, code, password, max_players = _room_fields(name, code, password, max_players)
    if not code:
        raise TournamentError('Escribe el ID de la sala.')
    inside = active_counts(room.tournament_id).get(room.id, 0)
    if max_players < inside:
        raise TournamentError(f'Esta sala ya tiene {inside} inscritos: el máximo no puede ser menor.')
    room.name, room.room_code, room.room_password, room.max_players = name, code, password, max_players
    db.session.commit()


def delete_room(room):
    if active_counts(room.tournament_id).get(room.id, 0):
        raise TournamentError('Esta sala ya tiene inscritos: no se puede borrar.')
    # Borrar la sala borraría también a los que se quitaron de ella, y con eso se les devolvería el permiso de volver a inscribirse.
    if TournamentEntry.query.filter_by(room_id=room.id).first():
        raise TournamentError('Esta sala ya tuvo inscritos (aunque ya no estén): no se puede borrar para no perder la lista de quitados. Termínala en su lugar.')
    if TournamentWinner.query.filter_by(room_id=room.id).first():
        raise TournamentError('Esta sala ya entregó premios: se queda en el historial.')
    db.session.delete(room)
    db.session.commit()


def move_room(room, direction):
    rooms = sorted(TournamentRoom.query.filter_by(tournament_id=room.tournament_id).all(), key=lambda r: (r.position, r.id))
    index = next(i for i, r in enumerate(rooms) if r.id == room.id)
    swap = index - 1 if direction == 'up' else index + 1
    if swap < 0 or swap >= len(rooms):
        return
    rooms[index], rooms[swap] = rooms[swap], rooms[index]
    for position, item in enumerate(rooms, start=1):
        item.position = position
    db.session.commit()


def _finish_room_rows(room):
    now = datetime.utcnow()
    if room.finished_at is None:
        room.finished_at = now
    TournamentEntry.query.filter_by(room_id=room.id, status='active', released_at=None).update({'released_at': now}, synchronize_session=False)


def finish_room(room):
    """Termina la sala: ya no recibe gente, la siguiente sala no la espera y los IDs que estaban en ella quedan libres para inscribirse en otra."""
    if room.finished_at:
        raise TournamentError('Esta sala ya está terminada.')
    _finish_room_rows(room)
    db.session.commit()


def reopen_room(room):
    """Deshace "Terminar sala". No se puede si alguno de sus jugadores ya se inscribió en otra sala (un ID no puede estar en dos salas a la vez)."""
    if not room.finished_at:
        raise TournamentError('Esta sala no está terminada.')
    entries = TournamentEntry.query.filter_by(room_id=room.id, status='active').all()
    for entry in entries:
        if entry.released_at is not None and live_entry(entry.game_id, entry.player_id) is not None:
            raise TournamentError(f'No se puede reabrir: {entry.player_nick or entry.player_id} ya se inscribió en otra sala.')
    try:
        room.finished_at = None
        TournamentEntry.query.filter_by(room_id=room.id, status='active').update({'released_at': None}, synchronize_session=False)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise TournamentError('No se puede reabrir: alguno de sus jugadores ya se inscribió en otra sala.')


def add_prize(tournament, place, package_id):
    if len(tournament.prizes) >= MAX_PRIZES:
        raise TournamentError(f'Máximo {MAX_PRIZES} premios por torneo.')
    place = _int(place, 1, 100, 'Puesto')
    package = db.session.get(Package, int(package_id)) if str(package_id or '').isdigit() else None
    if package is None or package.game_id != tournament.game_id:
        raise TournamentError('Elige un paquete del mismo juego del torneo.')
    if TournamentPrize.query.filter_by(tournament_id=tournament.id, place=place).first():
        raise TournamentError(f'El puesto {place} ya tiene premio. Bórralo primero si quieres cambiarlo.')
    db.session.add(TournamentPrize(tournament_id=tournament.id, place=place, package_id=package.id))
    db.session.commit()


def delete_prize(prize):
    if TournamentWinner.query.filter_by(tournament_id=prize.tournament_id, place=prize.place).first():
        raise TournamentError('Este premio ya se entregó en alguna sala: se queda en el historial.')
    db.session.delete(prize)
    db.session.commit()


def remove_entry(entry):
    """Saca a alguien de la lista de la sala (no podrá volver a inscribirse en este torneo y su lugar queda libre)."""
    if TournamentWinner.query.filter_by(room_id=entry.room_id, player_id=entry.player_id).first():
        raise TournamentError('Esta persona ya ganó un premio en la sala.')
    entry.status = 'removed'
    entry.slot_number = -entry.id   # libera su lugar (otra persona puede tomarlo); el número negativo es único y no choca con los lugares 1..máximo
    db.session.commit()


def roster(room):
    return (TournamentEntry.query.filter_by(room_id=room.id, status='active').order_by(TournamentEntry.slot_number.asc()).all())


def roster_text(room):
    """Lista lista para copiar y comparar con quienes están dentro de la sala del juego: 'ID - Nombre', uno por línea."""
    return '\n'.join(f'{e.player_id} - {e.player_nick or "(sin nombre)"}' for e in roster(room))


def winners_of_room(room):
    return {w.place: w for w in TournamentWinner.query.filter_by(room_id=room.id).all()}


def deliver_prize(tournament, room, place, entry_id):
    """Elige al ganador de un puesto en una sala y le entrega el premio en el momento. Devuelve (winner, mensaje). Una vez entregado no se repite."""
    from .order_processing import deliver_prize_to_player

    prize = TournamentPrize.query.filter_by(tournament_id=tournament.id, place=place).first()
    if prize is None or prize.package is None:
        raise TournamentError('Ese puesto no tiene premio configurado.')
    entry = db.session.get(TournamentEntry, int(entry_id)) if str(entry_id or '').isdigit() else None
    if entry is None or entry.room_id != room.id or entry.status != 'active':
        raise TournamentError('Elige a un jugador inscrito en esta sala.')

    lock_key = f'tournament_win:{room.id}'
    holder = uuid4().hex
    if not acquire_lock(lock_key, 120, holder):
        raise TournamentError('Se está entregando otro premio de esta sala. Espera unos segundos.')
    try:
        if TournamentWinner.query.filter_by(room_id=room.id, place=place).first():
            raise TournamentError(f'El puesto {place} de esta sala ya tiene ganador.')
        if TournamentWinner.query.filter_by(room_id=room.id, player_id=entry.player_id).first():
            raise TournamentError('Esa persona ya ganó otro puesto en esta sala.')
        winner = TournamentWinner(
            tournament_id=tournament.id, room_id=room.id, place=place, player_id=entry.player_id, player_nick=entry.player_nick,
            package_id=prize.package_id, status='delivering',
        )
        db.session.add(winner)
        db.session.commit()   # se guarda ANTES de entregar: si algo se corta, no se puede volver a elegir y pagar dos veces
        winner_id = winner.id
        # La marca [TW#id] hace única la nota de esta entrega: si algo falla, la orden se reconoce por ella y no por título y sala, que
        # el administrador puede repetir o renombrar (y entonces se confundiría con la orden de otro premio).
        note = f'Premio Torneo "{tournament.title}" — {room.name}, puesto {place}. [TW#{winner_id}]'
        try:
            order, approval = deliver_prize_to_player(tournament.game, prize.package, winner.player_id, note=note, reference_prefix='TORNEO')
        except Exception:
            db.session.rollback()
            # ¿llegó a crearse la orden antes de fallar? Entonces el premio sí salió (o quedó pendiente en Órdenes): se registra con esa orden
            # para no entregarlo dos veces. Si no hay orden, no se entregó nada y se deshace el registro para poder intentar de nuevo.
            # Por la marca [TW#id] con LIKE: la entrega agrega texto a las notas de la orden, y una búsqueda por nota exacta no la encontraba
            # (entonces se borraba al ganador y al reintentar se creaba un SEGUNDO pedido del mismo premio).
            order = Order.query.filter(
                Order.payment_method == 'prize', Order.player_id == winner.player_id, Order.game_id == tournament.game_id,
                Order.notes.like(f'%[TW#{winner_id}]%'),
            ).first()
            approval = {'message': 'La entrega se registró pero no se pudo confirmar: revisa la orden en Órdenes.'}
            if order is None:
                TournamentWinner.query.filter_by(id=winner_id).delete(synchronize_session=False)
                db.session.commit()
                raise TournamentError('No se pudo entregar el premio. No se registró al ganador: intenta de nuevo.')
        winner = db.session.get(TournamentWinner, winner_id)
        winner.status = 'delivered'
        winner.delivered_at = datetime.utcnow()
        winner.prize_order_id = order.id if order else None
        db.session.commit()
        message = (approval or {}).get('message') or 'Premio enviado.'
        # Con todos los premios de la sala entregados, la sala termina sola (sus jugadores quedan libres para otra sala)
        try:
            delivered = TournamentWinner.query.filter_by(room_id=room.id, status='delivered').count()
            room = db.session.get(TournamentRoom, room.id)
            if tournament.prizes and delivered >= len(tournament.prizes) and room.finished_at is None:
                _finish_room_rows(room)
                db.session.commit()
                message += ' Ya se entregaron todos los premios: la sala quedó terminada y sus jugadores ya pueden inscribirse en otra sala.'
        except Exception:
            db.session.rollback()
        return winner, message
    finally:
        release_lock(lock_key, holder)


def winners_history(tournament_id=None, limit=100):
    query = TournamentWinner.query.filter_by(status='delivered')
    if tournament_id:
        query = query.filter_by(tournament_id=tournament_id)
    return query.order_by(TournamentWinner.delivered_at.desc(), TournamentWinner.id.desc()).limit(limit).all()
