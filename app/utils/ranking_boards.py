"""Rankings mensuales editables: Admin → Dinámicas → Ranking.

El dueño crea los rankings que quiera (RankingBoard) de tres tipos y le pone premio a cada puesto (RankingBoardPrize):
  * 'game'     — los IDs de un juego que más recargaron en el mes. Premio: un paquete (se entrega al ID ganador), puntos de la tienda (se suman al ID
                 ganador) o un texto (lo entrega él).
  * 'accounts' — las CUENTAS que más recargaron en el mes (con su sesión abierta, a cualquier ID). Premio: puntos (a la bolsa del perfil) o texto.
  * 'levels'   — las CUENTAS con más nivel. Premio: puntos (a la bolsa del perfil) o texto.
Un perfil nunca gana puntos recargando: solo por el premio de un ranking o porque el administrador se los pone (ver utils/account_points.py).

Al empezar cada mes se cierra el anterior: se archivan los ganadores (RankingArchive) y se entregan solos los premios marcados "automático".
"""
import threading
import time
from datetime import date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from ..models import Game, Order, Package, RankingArchive, RankingBoard, RankingBoardPrize, Setting, db
from .locks import acquire_lock, release_lock
from .order_units import extract_order_units
from .timezone import now_ve, ve_day_start_utc_naive

KINDS = {
    'game': {'label': 'Por juego (los IDs que más recargan)', 'short': 'Juego', 'units': 'Diamantes recargados'},
    'accounts': {'label': 'Cuentas que más recargan', 'short': 'Cuentas', 'units': 'Diamantes recargados'},
    'levels': {'label': 'Perfiles por nivel', 'short': 'Niveles', 'units': 'Puntos de nivel'},
}
PRIZE_TYPES = {
    'game': ('none', 'package', 'points', 'text'),
    'accounts': ('none', 'points', 'text'),
    'levels': ('none', 'points', 'text'),
}
PRIZE_TYPE_LABELS = {'none': 'Sin premio', 'package': 'Paquete (se entrega al ID)', 'points': 'Puntos de la tienda', 'text': 'Otro (lo entrego yo)'}
MAX_PLACES = 10
DEFAULT_PLACES = 5
PUBLIC_SIZE = 10          # posiciones que se muestran en la tienda
NAME_MAX = 30
UNITS_MAX = 30
TEXT_MAX = 120
POINTS_MAX = 1_000_000
MAX_BOARDS = 12

# Los dos de siempre (Free Fire y Blood Strike) y los de Cuentas y Niveles: se crean una sola vez a partir de lo que ya había guardado.
LEGACY_BOARDS = (
    {'key': 'free_fire', 'name': 'Free Fire', 'kind': 'game', 'units': 'Diamantes recargados', 'aliases': ['free fire', 'freefire', 'ff']},
    {'key': 'blood_strike', 'name': 'Blood Strike', 'kind': 'game', 'units': 'Oro recargado', 'aliases': ['blood strike', 'bloodstrike']},
)
SEEDED_FLAG = 'ranking_boards_seeded'


def _setting(key, default=''):
    row = Setting.query.filter_by(key=key).first()
    return row.value if row is not None and row.value is not None else default


def _set_setting(key, value, description=''):
    row = Setting.query.filter_by(key=key).first()
    if row is None:
        db.session.add(Setting(key=key, value=str(value), description=description))
    else:
        row.value = str(value)


# ───────────────────────── Crear los rankings de siempre (una sola vez) ─────────────────────────
def _alias_game(aliases):
    for game in Game.query.order_by(Game.is_active.desc(), Game.position.asc(), Game.name.asc()).all():
        haystack = ' '.join([str(game.name or '').lower(), str(game.slug or '').lower(), str(game.description or '').lower()])
        if any(alias in haystack for alias in aliases):
            return game
    return None


def ensure_default_boards():
    """Primera vez: crea Free Fire y Blood Strike con lo que ya estaba configurado (activo, juego, premios), más "Cuentas" y "Niveles" apagados.
    Una marca evita que, si el dueño los borra, vuelvan a aparecer."""
    if _setting(SEEDED_FLAG) == '1':
        return
    try:
        if RankingBoard.query.count() == 0:
            for legacy in LEGACY_BOARDS:
                key = legacy['key']
                game_raw = _setting(f'ranking_{key}_game_id')
                game = db.session.get(Game, int(game_raw)) if str(game_raw).isdigit() else None
                game = game or _alias_game(legacy['aliases'])
                board = RankingBoard(
                    key=key, name=legacy['name'], kind='game', game_id=game.id if game else None, units_label=legacy['units'],
                    enabled=_setting(f'ranking_{key}_enabled', '0') == '1' and game is not None, prize_places=DEFAULT_PLACES,
                )
                db.session.add(board)
                db.session.flush()
                for position in range(1, DEFAULT_PLACES + 1):
                    package_raw = _setting(f'ranking_{key}_prize_package_{position}')
                    package = db.session.get(Package, int(package_raw)) if str(package_raw).isdigit() else None
                    db.session.add(RankingBoardPrize(
                        board_id=board.id, position=position, prize_type='package' if package else 'none',
                        package_id=package.id if package else None, points=0, text='',
                        auto=bool(package) and _setting(f'ranking_{key}_prize_auto_{position}', '0') == '1',
                    ))
            accounts = RankingBoard(
                key='cuentas', name='Cuentas', kind='accounts', units_label=KINDS['accounts']['units'],
                enabled=_setting('account_ranking_enabled', '0') == '1', prize_places=DEFAULT_PLACES,
            )
            db.session.add(accounts)
            db.session.flush()
            old_prize = ''.join(ch for ch in _setting('account_ranking_prize') if ch != '\n' and ord(ch) >= 32).strip()[:TEXT_MAX]
            if old_prize:
                db.session.add(RankingBoardPrize(board_id=accounts.id, position=1, prize_type='text', text=old_prize))
            db.session.add(RankingBoard(
                key='niveles', name='Niveles', kind='levels', units_label=KINDS['levels']['units'], enabled=False, prize_places=DEFAULT_PLACES,
            ))
        _set_setting(SEEDED_FLAG, '1', 'Los rankings de siempre ya se pasaron a Dinámicas → Ranking')
        db.session.commit()
    except Exception:
        db.session.rollback()


# ───────────────────────── Lectura ─────────────────────────
def all_boards():
    return RankingBoard.query.order_by(RankingBoard.id.asc()).all()


def enabled_boards():
    return RankingBoard.query.filter_by(enabled=True).order_by(RankingBoard.id.asc()).all()


def visible_boards_exist():
    """Barato (corre en cada página): ¿hay algún ranking encendido que se pueda mostrar? Uno de juego necesita su juego."""
    return db.session.query(RankingBoard.id).filter(
        RankingBoard.enabled.is_(True), or_(RankingBoard.kind != 'game', RankingBoard.game_id.isnot(None)),
    ).first() is not None


def units_label(board):
    return (board.units_label or '').strip() or KINDS.get(board.kind, KINDS['game'])['units']


def places(board):
    try:
        return max(1, min(MAX_PLACES, int(board.prize_places or DEFAULT_PLACES)))
    except (TypeError, ValueError):
        return DEFAULT_PLACES


def prize_map(board):
    """{puesto: RankingBoardPrize} de los puestos que tienen fila."""
    return {prize.position: prize for prize in board.prizes}


def prize_label(prize):
    """Lo que se le muestra a la gente como premio de ese puesto ('' si no hay)."""
    if prize is None:
        return ''
    if prize.prize_type == 'package':
        return prize.package.name if prize.package is not None else ''
    if prize.prize_type == 'points':
        points = int(prize.points or 0)
        return f'{points:,}'.replace(',', '.') + ' puntos' if points > 0 else ''
    if prize.prize_type == 'text':
        return (prize.text or '').strip()
    return ''


def ladder(board):
    """Premios por puesto para mostrar en la tienda: [{'position', 'reward_label'}] (vacío si el ranking no tiene ningún premio)."""
    prizes = prize_map(board)
    rows = [{'position': position, 'reward_label': prize_label(prizes.get(position)) or None} for position in range(1, places(board) + 1)]
    if not any(row['reward_label'] for row in rows):
        return []
    return [{'position': row['position'], 'reward_value': row['reward_label'], 'reward_label': row['reward_label'] or 'Sin premio'} for row in rows]


# ───────────────────────── Crear, guardar, borrar ─────────────────────────
def _clean_line(value, limit):
    return ''.join(ch for ch in str(value or '') if ch >= ' ' and ch != '\x7f').strip()[:limit]


def create_board(name, kind, game_id):
    """Crea un ranking apagado. Devuelve (ranking, error)."""
    name = _clean_line(name, NAME_MAX)
    if not name:
        return None, 'Escribe el nombre del ranking (por ejemplo: "Mobile Legends").'
    if kind not in KINDS:
        return None, 'Elige qué tipo de ranking quieres.'
    if RankingBoard.query.count() >= MAX_BOARDS:
        return None, f'Ya tienes {MAX_BOARDS} rankings: borra alguno antes de crear otro.'
    game = None
    if kind == 'game':
        game = db.session.get(Game, int(game_id)) if str(game_id or '').isdigit() else None
        if game is None:
            return None, 'Elige el juego de este ranking.'
    board = RankingBoard(
        key=f'r{uuid4().hex[:10]}', name=name, kind=kind, game_id=game.id if game else None, units_label=KINDS[kind]['units'],
        enabled=False, prize_places=DEFAULT_PLACES,
    )
    db.session.add(board)
    db.session.commit()
    return board, ''


def save_board(board, form):
    """Guarda lo que el dueño editó de un ranking (nombre, juego, encendido, puestos y premios). Devuelve '' o el mensaje de error; no hace commit."""
    name = _clean_line(form.get('name'), NAME_MAX)
    if not name:
        return 'Escribe el nombre del ranking.'
    try:
        count = int(form.get('prize_places') or DEFAULT_PLACES)
    except (TypeError, ValueError):
        return 'Elige cuántos puestos reciben premio.'
    if not 1 <= count <= MAX_PLACES:
        return f'Los puestos con premio deben ser de 1 a {MAX_PLACES}.'
    enabled = form.get('enabled') in ('1', 'on', 'true')
    game = None
    if board.kind == 'game':
        game_raw = (form.get('game_id') or '').strip()
        game = db.session.get(Game, int(game_raw)) if game_raw.isdigit() else None
        if game is None:
            return 'Elige el juego de este ranking.'

    allowed = PRIZE_TYPES[board.kind]
    parsed = []
    for position in range(1, count + 1):
        ptype = (form.get(f'prize_type_{position}') or 'none').strip()
        if ptype not in allowed:
            ptype = 'none'
        package_id, points, text = None, 0, ''
        auto = form.get(f'prize_auto_{position}') in ('1', 'on', 'true')
        if ptype == 'package':
            raw = (form.get(f'prize_package_{position}') or '').strip()
            package = Package.query.filter_by(id=int(raw), game_id=game.id, is_active=True).first() if raw.isdigit() else None
            if package is None:
                return f'Puesto #{position}: elige un paquete de {game.name}.'
            package_id = package.id
            if auto:
                package.is_automated = True
        elif ptype == 'points':
            raw = (form.get(f'prize_points_{position}') or '').strip()
            if not raw.isdigit() or not 1 <= int(raw) <= POINTS_MAX:
                return f'Puesto #{position}: escribe cuántos puntos (un número entre 1 y {POINTS_MAX:,}).'.replace(',', '.')
            points = int(raw)
        elif ptype == 'text':
            text = _clean_line(form.get(f'prize_text_{position}'), TEXT_MAX)
            if not text:
                return f'Puesto #{position}: escribe el premio.'
            auto = False
        else:
            auto = False
        parsed.append((position, ptype, package_id, points, text, auto))

    board.name = name
    board.units_label = _clean_line(form.get('units_label'), UNITS_MAX) or KINDS[board.kind]['units']
    if enabled and not board.enabled:
        board.pays_from = now_ve().strftime('%Y-%m')   # recién encendido: paga desde el cierre de ESTE mes, no el mes anterior
    board.enabled = enabled
    board.prize_places = count
    if game is not None:
        board.game_id = game.id
    existing = {prize.position: prize for prize in board.prizes}
    for position, ptype, package_id, points, text, auto in parsed:
        row = existing.get(position)
        if row is None:
            row = RankingBoardPrize(board_id=board.id, position=position)
            db.session.add(row)
        row.prize_type, row.package_id, row.points, row.text, row.auto = ptype, package_id, points, text, auto
    for position, row in existing.items():
        if position > count:
            db.session.delete(row)
    return ''


def delete_board(board):
    """Borra el ranking y sus premios. Lo que ya se archivó de meses anteriores se queda guardado."""
    db.session.delete(board)


# ───────────────────────── Ranking de IDs de un juego (el de siempre) ─────────────────────────
def month_range_for_ve(target_date=None):
    target_date = target_date or now_ve().date()
    month_start = date(target_date.year, target_date.month, 1)
    next_month = date(target_date.year + (1 if target_date.month == 12 else 0), 1 if target_date.month == 12 else target_date.month + 1, 1)
    return ve_day_start_utc_naive(month_start), ve_day_start_utc_naive(next_month)


def mask_player_id(player_id):
    raw = (player_id or '').strip()
    return raw[:4] + '****' if raw else '----'


def mask_nickname(nickname):
    raw = (nickname or '').strip()
    return raw[:3] + '***' if raw else 'Jugador***'


def game_entries(game_id, target_date=None):
    """IDs del juego ordenados por lo que recargaron en el mes (de más a menos)."""
    if not game_id:
        return []
    start_at, end_at = month_range_for_ve(target_date)
    orders = (
        Order.query
        .filter(Order.game_id == game_id)
        .filter(Order.status.in_(['approved', 'completed']))
        # Los premios ($0) no son compras: sin esto, alguien que reciba muchos premios sube al ranking y se lleva además el de fin de mes.
        .filter(or_(Order.payment_method.is_(None), Order.payment_method != 'prize'))
        .filter(Order.created_at >= start_at, Order.created_at < end_at)
        .order_by(Order.created_at.asc())
        .all()
    )
    grouped = {}
    for order in orders:
        player_id = (order.player_id or '').strip()
        if not player_id:
            continue
        bucket = grouped.setdefault(player_id, {
            'player_id': player_id, 'nickname': (order.player_nickname or '').strip(), 'total_units': 0, 'total_spent': 0.0,
            'last_seen_at': order.created_at,
        })
        if order.player_nickname:
            bucket['nickname'] = (order.player_nickname or '').strip()
        bucket['total_units'] += extract_order_units(order)
        try:
            bucket['total_spent'] += float(order.amount or 0)
        except (TypeError, ValueError):
            pass
        bucket['last_seen_at'] = order.created_at
    return sorted(grouped.values(), key=lambda item: (-item['total_units'], -item['total_spent'], item['player_id']))


# Con muchos pedidos en el mes, armar el ranking recorre todas las órdenes del mes. Esa lista se guarda unos segundos en memoria (por proceso)
# para que varias personas abriendo el ranking a la vez no repitan el trabajo; el cierre de mes y la entrega de premios SIEMPRE calculan en fresco.
ENTRIES_TTL_SECONDS = 30
_entries_cache = {}
_entries_lock = threading.Lock()


def game_entries_cached(game_id, target_date=None):
    key = (game_id, month_range_for_ve(target_date)[0])
    now = time.monotonic()
    with _entries_lock:
        hit = _entries_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    entries = game_entries(game_id, target_date=target_date)
    with _entries_lock:
        if len(_entries_cache) > 16:
            _entries_cache.clear()
        _entries_cache[key] = (now + ENTRIES_TTL_SECONDS, entries)
    return entries


def find_current_position(entries, lookup_identifier):
    lookup_identifier = (lookup_identifier or '').strip()
    if not lookup_identifier:
        return None
    current_index = None
    for index, entry in enumerate(entries):
        if (entry.get('player_id') or '').strip() == lookup_identifier:
            current_index = index
            break
    if current_index is None:
        return None
    entry = entries[current_index]
    current_units = int(entry.get('total_units') or 0)
    higher_entry = entries[current_index - 1] if current_index > 0 else None
    lower_entry = entries[current_index + 1] if current_index + 1 < len(entries) else None
    next_target_units = int(higher_entry.get('total_units') or 0) if higher_entry else current_units
    lower_units = int(lower_entry.get('total_units') or 0) if lower_entry else 0
    missing_units = max(next_target_units - current_units, 0) if higher_entry else 0
    progress_percent = 100
    if higher_entry:
        denominator = max(next_target_units - lower_units, 1)
        numerator = max(current_units - lower_units, 0)
        progress_percent = max(6, min(99, int(round((numerator / denominator) * 100))))
    return {
        'position': current_index + 1, 'masked_player_id': mask_player_id(entry.get('player_id')),
        'masked_nickname': mask_nickname(entry.get('nickname')), 'total_units': current_units, 'next_target_units': next_target_units,
        'missing_units': missing_units, 'progress_percent': progress_percent, 'is_top_ten': current_index < 10,
    }


# ───────────────────────── Cierre de mes: archivar ganadores y entregar premios ─────────────────────────
def _month_winners(board, year, month, target_date):
    """Los ganadores del mes que se cerró, de más a menos: [{'player_id', 'nickname', 'user', 'total_units'}]."""
    from . import profile_social as PS

    if board.kind == 'game':
        return [{'player_id': (e.get('player_id') or '').strip() or None, 'nickname': (e.get('nickname') or '').strip(), 'user': None,
                 'total_units': int(e.get('total_units') or 0)} for e in game_entries(board.game_id, target_date=target_date)]
    if board.kind == 'accounts':
        rows = [(total, user) for total, user in PS.month_candidates(f'{year:04d}-{month:02d}') if user.has_chosen_username]
        return [{'player_id': None, 'nickname': user.username, 'user': user, 'total_units': int(total)} for total, user in rows]
    return [{'player_id': None, 'nickname': user.username, 'user': user, 'total_units': int(xp)} for xp, user in PS.level_candidates()]


def _deliver(board, archive, prize, winner, month_text):
    """Entrega (o deja anotado para entregar a mano) el premio de un puesto. Hace commit con lo que anotó."""
    from . import account_points
    from .points import credit_player_points

    position = archive.position
    if prize is None or prize.prize_type == 'none':
        archive.prize_detail = ''
    elif prize.prize_type == 'text':
        archive.prize_detail = 'Entregar a mano: premio en texto.'
    elif not prize.auto:
        archive.prize_detail = 'Entregar a mano: no tiene entrega automática.'
    elif prize.prize_type == 'package' and board.kind == 'game' and winner['player_id'] and prize.package is not None:
        from .order_processing import deliver_prize_to_player
        game = board.game
        prize_order, _approval = deliver_prize_to_player(
            game, prize.package, winner['player_id'],
            note=f'Premio Ranking Mensual — puesto #{position} de {board.name} ({month_text}). [RK#{archive.id}]', reference_prefix='RANKING',
        )
        archive.prize_order_id = prize_order.id if prize_order else None
        archive.prize_detail = f'Paquete enviado (orden #{prize_order.order_number})' if prize_order else 'No se pudo enviar el paquete: entregar a mano.'
    elif prize.prize_type == 'points' and int(prize.points or 0) > 0:
        points = int(prize.points)
        if board.kind == 'game' and winner['player_id']:
            credit_player_points(board.game_id, winner['player_id'], points)
            archive.prize_detail = f'{points} puntos sumados al ID {winner["player_id"]}.'
        elif winner['user'] is not None:
            account_points.credit(winner['user'].id, points, kind='ranking', note=f'Premio de {board.name}: puesto #{position} ({month_text})')
            archive.prize_detail = f'{points} puntos sumados al perfil.'
        else:
            archive.prize_detail = 'Entregar a mano: no se encontró a quién sumarle los puntos.'
    else:
        archive.prize_detail = 'Entregar a mano.'
    db.session.commit()


def close_board_month(board, year, month, target_date):
    """Cierra el mes `year-month` de un ranking: archiva a los ganadores y entrega los premios automáticos. Una sola vez por ranking y mes."""
    if board.pays_from and f'{year:04d}-{month:02d}' < board.pays_from:
        return False   # el ranking se creó o se encendió después de ese mes: no existía, no paga
    if RankingArchive.query.filter_by(ranking_key=board.key, year=year, month=month).first():
        return False
    lock_key = f'ranking_archive:{board.key}:{year}-{month:02d}'
    holder = uuid4().hex
    # Esta función corre desde el proceso de fondo y desde el comando de línea: sin candado, dos a la vez entregarían los premios dos veces.
    if not acquire_lock(lock_key, 120, holder):
        return False
    try:
        if RankingArchive.query.filter_by(ranking_key=board.key, year=year, month=month).first():
            return False
        if board.kind == 'game' and board.game is None:
            return False
        winners = _month_winners(board, year, month, target_date)
        if not winners:
            return False
        prizes = prize_map(board)
        game_name = board.game.name if (board.kind == 'game' and board.game) else board.name
        pending = []
        for index, winner in enumerate(winners[:places(board)], start=1):
            prize = prizes.get(index)
            user = winner['user']
            archive = RankingArchive(
                ranking_key=board.key, year=year, month=month, position=index, game_name=game_name,
                player_id=winner['player_id'] or (f'@{user.username}' if user is not None else None),
                nickname=winner['nickname'] or None,
                masked_player_id=mask_player_id(winner['player_id']) if board.kind == 'game' else (f'@{user.username}' if user is not None else '----'),
                masked_nickname=mask_nickname(winner['nickname']) if board.kind == 'game' else (user.username if user is not None else 'Cuenta'),
                total_units=winner['total_units'], prize_label=(prize_label(prize) or 'Sin premio')[:100],
                user_id=user.id if user is not None else None,
                prize_detail='Entrega automática pendiente.' if (prize is not None and prize.auto and prize.prize_type in ('package', 'points')) else '',
            )
            db.session.add(archive)
            pending.append((archive, prize, winner))
        # El mes queda archivado ANTES de entregar: cada entrega puede tardar, y mientras tanto otra pasada que viera el mes sin archivar volvía a
        # pagar el top. El UNIQUE de (ranking, año, mes, puesto) es la última defensa.
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return False
        month_text = f'{year}-{month:02d}'
        for archive, prize, winner in pending:
            archive_id = archive.id
            try:
                _deliver(board, archive, prize, winner, month_text)
            except Exception:
                db.session.rollback()
                archive = db.session.get(RankingArchive, archive_id)
                if archive is not None:
                    # ¿llegó a crearse el pedido antes de fallar? Entonces NO hay que entregarlo a mano otra vez: se anota con ese pedido.
                    order = Order.query.filter(Order.payment_method == 'prize', Order.notes.like(f'%[RK#{archive_id}]%')).first()
                    if order is not None:
                        archive.prize_order_id = order.id
                        archive.prize_detail = f'Paquete enviado (orden #{order.order_number}); la entrega no se confirmó: revisa Órdenes. NO entregar a mano.'
                    else:
                        archive.prize_detail = 'Falló la entrega automática: entregar a mano.'
                    db.session.commit()
        return True
    finally:
        release_lock(lock_key, holder)


def close_previous_month_if_needed():
    """Corre en el proceso de fondo: si ya empezó otro mes, cierra el anterior de cada ranking encendido."""
    today = now_ve().date()
    previous_last_day = today.replace(day=1) - timedelta(days=1)
    for board in enabled_boards():
        close_board_month(board, previous_last_day.year, previous_last_day.month, previous_last_day)
    db.session.commit()
