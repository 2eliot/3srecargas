"""Hordas de Diamantes por CUENTA: un solo juego para todos los juegos de la tienda.

Desde la semana del 19/10/2026 (decisión del dueño) Hordas ya no se juega con un ID ni por juego: se juega con una CUENTA (la de Mi cuenta), el
ranking semanal es de cuentas (nombre de usuario y foto, nunca un ID) y el premio son PUNTOS DE LA TIENDA que caen en la cuenta del ganador
(`utils/account_points.py`) y se gastan en `/tiendapuntos`. Las partidas extra, cuando se acaban las gratis del día, también se pagan con puntos de la cuenta.

Todo lo configurable vive en Admin → Dinámicas → Hordas (tabla `settings`): si está activo, partidas gratis por día, costo de la partida extra y los
puntos de cada puesto del Top (cuántos puestos reciben premio lo decide el dueño).

El premio no sale solo: al cerrar la semana los ganadores quedan PENDIENTES y el dueño los aprueba después de revisar las repeticiones (entonces
caen los puntos). Un tramposo se bloquea (cuenta) y los siguientes suben de puesto.

Las reglas del juego y los topes contra trampa (cristales por segundo / por enemigo / por oleada) son los de siempre y viven en `utils/promos.py`.
"""
import json
import secrets
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_

from ..models import (
    AccountNotice, HordeAccountBan, HordeAccountReplay, HordeAccountRun, HordeAccountWinner, Setting, User, UserProfile, db,
)
from . import account_points
from .auth_accounts import is_account_user
from .locks import acquire_lock, check_rate_limit, release_lock
from .promos import (
    ACCOUNT_MODE_FIRST_WEEK, HORDE_FIELD_H, HORDE_FIELD_W, HORDE_MAX_DIAMONDS_PER_KILL, HORDE_MAX_DIAMONDS_PER_SECOND, HORDE_MAX_RUN_SECONDS,
    HORDE_MAX_WAVE, HORDE_RANKING_SIZE, HORDE_REPLAY_KEEP_TOP, HORDE_REPLAY_MAX_BYTES, HORDE_REPLAY_SAFETY_TOP, HORDE_REPLAY_WEEK_MAX_BYTES,
    HORDE_RULES_VERSION, _horde_week_key, _horde_week_start, _replay_field_size, _replay_rules, horde_max_diamonds_by_wave, horde_min_seconds_by_wave,
)
from .timezone import now_ve, today_ve_str

SETTING_ENABLED = 'hordas_enabled'
SETTING_RUNS = 'hordas_runs_per_day'
SETTING_EXTRA = 'hordas_extra_cost'
SETTING_PRIZES = 'hordas_prizes'          # lista JSON: puntos del puesto 1, 2, 3... (0 = ese puesto no tiene premio)
SETTINGS_KEYS = (SETTING_ENABLED, SETTING_RUNS, SETTING_EXTRA, SETTING_PRIZES)

DEFAULT_RUNS_PER_DAY = 3
MAX_RUNS_PER_DAY = 100
DEFAULT_EXTRA_COST = 10
MAX_EXTRA_COST = 100_000
MAX_PRIZE_PLACES = 10                     # el Top público muestra 10
# Tope de partidas EXTRA (pagadas con puntos) por cuenta y por día: ya no protege los puntos de nadie (son de la propia cuenta), solo evita que una
# cuenta llene la base de repeticiones.
MAX_PAID_RUNS_PER_DAY = 50
CANCEL_GRACE_SECONDS = 30
ADMIN_REPLAY_TOP = 5
NOTICE_MAX = 300


class HordeOutdatedPage(ValueError):
    """La página abierta es de una versión vieja (se actualizó el juego mientras la tenía abierta): se recarga antes de jugar."""


class HordeNotEnoughPoints(ValueError):
    """Faltan puntos en la cuenta para la partida extra (la página muestra un aviso)."""

    def __init__(self, message, balance):
        super().__init__(message)
        self.balance = balance


class HordeBlocked(ValueError):
    """La cuenta no puede jugar ahora. `code`: 'login' | 'username' | 'hidden' | 'banned' | 'off'."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


# ───────────────────────── Ajustes del admin ─────────────────────────
def _clamp(raw, default, minimum, maximum):
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _parse_prizes(raw):
    try:
        data = json.loads(raw or '[]')
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    prizes = []
    for item in data[:MAX_PRIZE_PLACES]:
        try:
            prizes.append(max(0, min(account_points.MAX_POINTS_PER_MOVE, int(item))))
        except (TypeError, ValueError):
            prizes.append(0)
    return prizes


def get_settings():
    """{'enabled', 'runs_per_day', 'extra_cost', 'prizes': [puntos del puesto 1, 2, ...]} (una sola consulta)."""
    values = {row.key: row.value for row in Setting.query.filter(Setting.key.in_(SETTINGS_KEYS)).all()}
    return {
        'enabled': (values.get(SETTING_ENABLED) or '1').strip() != '0',
        'runs_per_day': _clamp(values.get(SETTING_RUNS), DEFAULT_RUNS_PER_DAY, 1, MAX_RUNS_PER_DAY),
        'extra_cost': _clamp(values.get(SETTING_EXTRA), DEFAULT_EXTRA_COST, 0, MAX_EXTRA_COST),
        'prizes': _parse_prizes(values.get(SETTING_PRIZES)),
    }


def is_enabled():
    row = Setting.query.filter_by(key=SETTING_ENABLED).first()
    return (row.value if row else '1').strip() != '0'


def _put_setting(key, value, description):
    row = Setting.query.filter_by(key=key).first()
    if row is None:
        db.session.add(Setting(key=key, value=str(value), description=description))
    else:
        row.value = str(value)


def save_settings(form):
    """Guarda lo que el dueño editó en Admin → Dinámicas → Hordas. Devuelve '' o el mensaje de error; no hace commit."""
    raw_runs = (form.get('runs_per_day') or '').strip()
    if not raw_runs.isdigit() or not 1 <= int(raw_runs) <= MAX_RUNS_PER_DAY:
        return f'Las partidas gratis por día deben ser un número de 1 a {MAX_RUNS_PER_DAY}.'
    raw_extra = (form.get('extra_cost') or '0').strip() or '0'
    if not raw_extra.isdigit() or int(raw_extra) > MAX_EXTRA_COST:
        return f'El costo de la partida extra debe ser un número de 0 a {MAX_EXTRA_COST} (0 = no se venden partidas extra).'
    raw_places = (form.get('prize_places') or '').strip()
    if not raw_places.isdigit() or not 1 <= int(raw_places) <= MAX_PRIZE_PLACES:
        return f'Los puestos con premio deben ser de 1 a {MAX_PRIZE_PLACES}.'
    prizes = []
    for place in range(1, int(raw_places) + 1):
        raw = (form.get(f'prize_points_{place}') or '0').strip() or '0'
        if not raw.isdigit() or int(raw) > account_points.MAX_POINTS_PER_MOVE:
            return f'Puesto #{place}: escribe cuántos puntos gana (un número de 0 a {account_points.MAX_POINTS_PER_MOVE:,}; 0 = sin premio).'.replace(',', '.')
        prizes.append(int(raw))
    _put_setting(SETTING_ENABLED, '1' if form.get('enabled') in ('1', 'on', 'true') else '0', 'Hordas de Diamantes activo')
    _put_setting(SETTING_RUNS, int(raw_runs), 'Hordas: partidas gratis por día')
    _put_setting(SETTING_EXTRA, int(raw_extra), 'Hordas: puntos de la cuenta que cuesta cada partida extra (0 = no hay)')
    _put_setting(SETTING_PRIZES, json.dumps(prizes), 'Hordas: puntos de premio por puesto')
    return ''


def prize_list(prizes):
    """[{'place', 'points'}] solo de los puestos que tienen premio."""
    return [{'place': index, 'points': points} for index, points in enumerate(prizes, start=1) if points > 0]


# ───────────────────────── Quién puede jugar ─────────────────────────
def is_banned(user_id):
    return bool(user_id) and HordeAccountBan.query.filter_by(user_id=user_id).first() is not None


def eligibility(user):
    """(puede, código, mensaje). Código: 'login' (sin cuenta), 'username' (falta elegir usuario), 'hidden' (perfil privado u oculto de rankings),
    'banned' (bloqueada en Hordas) o ''."""
    if not is_account_user(user) or not user.is_active:
        return False, 'login', 'Inicia sesión o crea tu cuenta para jugar.'
    if not user.has_chosen_username:
        return False, 'username', 'Elige tu nombre de usuario en tu perfil para jugar: así apareces en el Top.'
    if is_banned(user.id):
        return False, 'banned', 'Tu cuenta está bloqueada en Hordas de Diamantes.'
    profile = UserProfile.query.filter_by(user_id=user.id).first()
    if profile is not None:
        if profile.moderation_hidden:
            return False, 'banned', 'Tu perfil no puede participar en Hordas de Diamantes.'
        if profile.is_public is False or profile.in_rankings is False:
            return False, 'hidden', 'Para jugar Hordas tu perfil debe ser público y salir en los rankings. Cámbialo en tu perfil.'
    return True, '', ''


def _eligible_profile_filter():
    """Misma regla del Top de perfiles: cuenta activa con perfil público, no oculto por el admin y que acepta salir en rankings."""
    return or_(
        UserProfile.user_id.is_(None),
        and_(
            or_(UserProfile.is_public.is_(None), UserProfile.is_public.is_(True)),
            or_(UserProfile.moderation_hidden.is_(None), UserProfile.moderation_hidden.is_(False)),
            or_(UserProfile.in_rankings.is_(None), UserProfile.in_rankings.is_(True)),
        ),
    )


def identities(user_ids):
    """{user_id: {'name', 'handle', 'avatar'}} de cuentas (lo único que se muestra de ellas: nunca IDs ni compras)."""
    from flask import url_for

    from . import profile_social as PS
    from .profile_shop import effective_avatar_path
    from .profiles import media_url_path

    def avatar_src(profile):
        # `media_url_path` devuelve 'uploads/...' (el nombre dentro de /static): como `src` suelto la página lo pedía a /promos/uploads/... y la foto salía rota.
        path = (media_url_path(effective_avatar_path(profile)) or '') if profile else ''
        if not path:
            return ''
        try:
            return url_for('static', filename=path)
        except RuntimeError:
            return '/static/' + path

    ids = list({int(i) for i in user_ids if i})
    if not ids:
        return {}
    profiles = {p.user_id: p for p in UserProfile.query.filter(UserProfile.user_id.in_(ids)).all()}
    out = {}
    for user in User.query.filter(User.id.in_(ids)).all():
        profile = profiles.get(user.id)
        out[user.id] = {'name': PS._shown_name(user, profile), 'handle': user.username, 'avatar': avatar_src(profile)}
    return out


# ───────────────────────── Ranking semanal ─────────────────────────
def week_ranking(week_key, limit=HORDE_RANKING_SIZE):
    """[(user_id, total_cristales)] de la semana, de mayor a menor. Empate: gana quien llegó primero a ese total (su última partida terminó antes).
    Las cuentas bloqueadas, inactivas u ocultas de los rankings no aparecen (ni ganan premio)."""
    total = func.sum(HordeAccountRun.diamonds)
    last_finish = func.max(HordeAccountRun.finished_at)
    banned = db.session.query(HordeAccountBan.user_id)
    query = (
        db.session.query(HordeAccountRun.user_id, total)
        .join(User, User.id == HordeAccountRun.user_id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .filter(
            HordeAccountRun.week_key == week_key,
            HordeAccountRun.finished_at.isnot(None),
            User.is_active.is_(True),
            ~HordeAccountRun.user_id.in_(banned),
            _eligible_profile_filter(),
        )
        .group_by(HordeAccountRun.user_id)
        .having(total > 0)
        .order_by(total.desc(), last_finish.asc())
    )
    if limit:
        query = query.limit(limit)
    return [(uid, int(t or 0)) for uid, t in query.all()]


def _my_position(full_ranking, user_id):
    """Puesto de la cuenta con lo que le falta para pasar a la de arriba: {'place', 'diamonds', 'missing', 'progress_percent', 'total_players'}."""
    for index, (uid, total) in enumerate(full_ranking, start=1):
        if uid != user_id:
            continue
        above_total = full_ranking[index - 2][1] if index > 1 else None
        # En empate gana quien llegó primero, así que hay que superarlo por 1.
        missing = (above_total - total + 1) if above_total is not None else 0
        progress = 100 if above_total is None else int(min(100, total * 100 / max(1, above_total + 1)))
        return {'place': index, 'diamonds': total, 'missing': missing, 'progress_percent': progress, 'total_players': len(full_ranking)}
    return None


def runs_today(user_id, day_key, paid=False):
    query = HordeAccountRun.query.filter_by(user_id=user_id, day_key=day_key)
    return query.filter(HordeAccountRun.points_spent > 0).count() if paid else query.filter(HordeAccountRun.points_spent == 0).count()


def public_state(user):
    """Todo lo que la página necesita: premios, ranking (cuentas), tu puesto, tus partidas de hoy y tus puntos."""
    settings = get_settings()
    if not settings['enabled']:
        return {'enabled': False}

    week_key = _horde_week_key()
    week_start = _horde_week_start()
    now = now_ve()
    week_ends_at = now.replace(year=week_start.year, month=week_start.month, day=week_start.day, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=7)

    ok, code, message = eligibility(user)
    logged = is_account_user(user)
    user_id = user.id if ok else None

    full = week_ranking(week_key, limit=None)
    top = full[:HORDE_RANKING_SIZE]
    past = (
        HordeAccountWinner.query.filter_by(status='approved')
        .order_by(HordeAccountWinner.week_key.desc(), HordeAccountWinner.place.asc()).limit(9).all()
    )
    who_by_id = identities([uid for uid, _t in top] + [w.user_id for w in past] + ([user.id] if logged else []))
    ranking = []
    for index, (uid, total) in enumerate(top, start=1):
        who = who_by_id.get(uid)
        if who is None:
            continue
        ranking.append({'place': index, 'name': who['name'], 'handle': who['handle'], 'avatar': who['avatar'], 'diamonds': total, 'is_me': uid == user_id})

    me = _my_position(full, user_id) if user_id else None
    left = None
    if user_id:
        left = max(0, settings['runs_per_day'] - runs_today(user_id, today_ve_str()))
    mine = who_by_id.get(user.id, {}) if logged else {}
    return {
        'enabled': True,
        'prizes': prize_list(settings['prizes']),
        'runs_per_day': settings['runs_per_day'],
        'runs_left_today': left,
        'extra_run_cost': settings['extra_cost'],
        'week_key': week_key,
        'week_ends_at': week_ends_at.isoformat(),
        'account': {
            'logged_in': logged, 'ready': ok, 'code': code, 'message': message,
            'name': mine.get('name', ''), 'handle': mine.get('handle', '') if (logged and user.has_chosen_username) else '',
            'balance': account_points.balance(user.id) if ok else None,
        },
        'ranking': ranking,
        'total_players': len(full),
        'my_total': me['diamonds'] if me else 0,
        'my_rank': me['place'] if me else None,
        'me': me,
        'past_winners': [{
            'week_key': w.week_key, 'place': w.place, 'name': who_by_id[w.user_id]['name'], 'handle': who_by_id[w.user_id]['handle'],
            'avatar': who_by_id[w.user_id]['avatar'], 'diamonds': w.diamonds, 'points': w.points,
        } for w in past if w.user_id in who_by_id],
    }


# ───────────────────────── Partidas ─────────────────────────
def start_run(user, ip='', use_points=False, rules=None):
    """Abre una partida de la cuenta y devuelve su token. Lanza ValueError con un mensaje listo para mostrar si no se puede jugar.

    Al acabarse las partidas gratis del día se puede abrir una extra con puntos de la cuenta (si el dueño puso un costo). El descuento de puntos y la
    partida se guardan en la misma transacción: nunca se cobran puntos sin que se abra la partida."""
    settings = get_settings()
    if not settings['enabled']:
        raise HordeBlocked('Hordas de Diamantes no está activo en este momento.', 'off')
    ok, code, message = eligibility(user)
    if not ok:
        raise HordeBlocked(message, code)
    try:
        page_rules = int(rules) if rules is not None else None
    except (TypeError, ValueError):
        page_rules = None
    if page_rules != HORDE_RULES_VERSION:
        raise HordeOutdatedPage('El juego se actualizó. Recargando para jugar con la versión nueva…')

    user_id = user.id
    day_key = today_ve_str()
    week_key = _horde_week_key()
    runs_per_day = settings['runs_per_day']
    extra_cost = settings['extra_cost']

    lock_key = f'horde_start:u{user_id}'
    lock_holder = uuid4().hex
    if not acquire_lock(lock_key, 30, lock_holder):
        raise ValueError('Tu partida anterior todavía se está abriendo. Espera unos segundos.')
    try:
        used = runs_today(user_id, day_key)
        cost = 0
        if used >= runs_per_day:
            if extra_cost <= 0:
                raise ValueError(f'Ya usaste tus {runs_per_day} partidas de hoy. Vuelve mañana.')
            if not use_points:
                raise ValueError(f'Ya usaste tus {runs_per_day} partidas gratis de hoy. Puedes jugar otra con {extra_cost} puntos de tu cuenta.')
            if runs_today(user_id, day_key, paid=True) >= MAX_PAID_RUNS_PER_DAY:
                raise ValueError(f'Llegaste al máximo de {MAX_PAID_RUNS_PER_DAY} partidas extra de hoy. Vuelve mañana.')
            cost = extra_cost

        points_balance = None
        if cost:
            # Descuento atómico: solo pasa si el saldo alcanza en ese instante, aunque la cuenta esté gastando puntos en otra parte a la vez.
            if not account_points.debit(user_id, cost, kind='hordas', note='Partida extra de Hordas de Diamantes'):
                db.session.rollback()
                balance = account_points.balance(user_id)
                raise HordeNotEnoughPoints(
                    f'No tienes puntos suficientes: cada partida extra cuesta {cost} puntos y tu cuenta tiene {balance}.', balance,
                )
            points_balance = account_points.balance(user_id)

        run = HordeAccountRun(
            token=uuid4().hex, user_id=user_id, week_key=week_key, day_key=day_key, ip=(ip or '')[:64],
            started_at=datetime.utcnow(), points_spent=cost, seed=secrets.randbelow(2 ** 31),
        )
        db.session.add(run)
        db.session.commit()
        return {
            'token': run.token, 'seed': run.seed,
            'runs_left_today': max(0, runs_per_day - used - (0 if cost else 1)),
            'points_spent': cost, 'points_balance': points_balance,
        }
    finally:
        # Si algo falló a medias, la sesión queda sucia y soltar el bloqueo fallaría en silencio (el cliente no podría reintentar por unos segundos).
        db.session.rollback()
        release_lock(lock_key, lock_holder)


def _store_replay(run, replay):
    """Guarda la grabación de la partida si viene bien formada. Nunca hace fallar el cierre de la partida: si algo no cuadra, simplemente no se guarda
    (en el admin se verá 'sin repetición')."""
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

    # Freno de espacio: si esta semana ya se guardó demasiado, solo se guardan las de quienes están cerca del top (los demás no pueden ganar).
    week_bytes = (
        db.session.query(func.coalesce(func.sum(HordeAccountReplay.size), 0)).filter(HordeAccountReplay.week_key == run.week_key).scalar()
    )
    if week_bytes + len(data) > HORDE_REPLAY_WEEK_MAX_BYTES:
        top = week_ranking(run.week_key, limit=HORDE_REPLAY_SAFETY_TOP)
        if run.user_id not in {uid for uid, _total in top}:
            return

    db.session.add(HordeAccountReplay(
        run_id=run.id, user_id=run.user_id, week_key=run.week_key,
        width=width, height=height, character_slot=slot, rules=rules, data=data, size=len(data),
    ))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


def cleanup_replays():
    """Al cerrar cada semana deja solo las grabaciones del top 5 (por si alguno del top 3 hizo trampa y hay que correr los puestos) y borra el resto.
    Se marca cada semana ya limpia para no repetir el trabajo."""
    current_week = _horde_week_key()
    pending = [
        w for (w,) in db.session.query(HordeAccountReplay.week_key)
        .filter(HordeAccountReplay.week_key < current_week).distinct().all()
    ]
    for week_key in pending:
        marker = f'hordas_cuenta_replays_clean:{week_key}'
        if Setting.query.filter_by(key=marker).first():
            continue
        keep = {uid for uid, _total in week_ranking(week_key, limit=HORDE_REPLAY_KEEP_TOP)}
        query = HordeAccountReplay.query.filter(HordeAccountReplay.week_key == week_key)
        if keep:
            query = query.filter(~HordeAccountReplay.user_id.in_(keep))
        query.delete(synchronize_session=False)
        db.session.add(Setting(key=marker, value=datetime.utcnow().isoformat()))
        db.session.commit()


def finish_run(token, diamonds, kills, wave=None, final_cleared=False, replay=None):
    """Cierra una partida una sola vez y guarda los cristales aceptados.

    Topes, del más fácil de pasar al más estricto:
    - por tiempo (7/s) y por enemigo (4 c/u), como antes;
    - por oleada: nunca más cristales de los que existen en las oleadas que alcanzó (según la semilla de la partida);
    - por velocidad: si dice ir en una oleada a la que no se puede llegar en el tiempo que duró, se toma la que sí era posible y se marca;
    - ganar la oleada final (imposible) o pasar de la 10 = trampa: 0 cristales."""
    token = str(token or '').strip()
    run = HordeAccountRun.query.filter_by(token=token).first() if token else None
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
    cap = min(int(max(0.0, elapsed) * HORDE_MAX_DIAMONDS_PER_SECOND), kills * HORDE_MAX_DIAMONDS_PER_KILL)

    flag = None
    if final_cleared or wave > HORDE_MAX_WAVE:
        flag = 'gano_oleada_final'
        cap = 0
    elif isinstance(replay, dict) and _replay_field_size(replay) != (HORDE_FIELD_W, HORDE_FIELD_H):
        # Campo de otro tamaño = juego modificado (o una versión vieja de la página abierta desde antes del cambio): no suma.
        flag = 'campo_distinto'
        cap = 0
    elif isinstance(replay, dict) and _replay_rules(replay) != HORDE_RULES_VERSION:
        flag = 'reglas_viejas'
        cap = 0
    elif run.seed is not None:
        # Oleada a la que de verdad se puede llegar en el tiempo que duró: la que está en curso cuenta completa (hasta la 9; la 10 no da cristales).
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

    # UPDATE condicional: si llegan dos cierres a la vez (doble envío, sendBeacon al salir + el normal), solo uno cuenta.
    updated = (
        HordeAccountRun.query
        .filter(HordeAccountRun.id == run.id, HordeAccountRun.finished_at.is_(None))
        .update({
            'finished_at': now, 'diamonds': accepted, 'claimed_diamonds': diamonds, 'kills': kills,
            'wave': min(max(0, wave), 99), 'flag': flag,
        }, synchronize_session=False)
    )
    db.session.commit()
    if not updated:
        raise ValueError('Esta partida ya fue registrada.')
    _store_replay(run, replay)

    # Los cristales ya quedaron guardados: si calcular el puesto falla, igual se responde que se guardó (sin el puesto) en vez de un error.
    my_total = 0
    my_rank = None
    try:
        for index, (uid, total) in enumerate(week_ranking(run.week_key, limit=None), start=1):
            if uid == run.user_id:
                my_total, my_rank = total, index
                break
    except Exception:
        db.session.rollback()
    return {'diamonds': accepted, 'capped': accepted < diamonds, 'flagged': bool(flag), 'week_total': my_total, 'week_rank': my_rank}


def cancel_run(token):
    """Anula una partida sin terminar abierta hace menos de 30 s (el teléfono no pudo armar el juego): se borra (deja de contar como partida del día) y
    se devuelven los puntos si se había pagado con puntos. Solo vale justo después de abrirla: si no, se podría "anular" una partida mala para repetirla
    y quedarse solo con las buenas."""
    token = str(token or '').strip()
    run = HordeAccountRun.query.filter_by(token=token).first() if token else None
    if not run or run.finished_at:
        raise ValueError('Partida no encontrada.')
    if (datetime.utcnow() - run.started_at).total_seconds() > CANCEL_GRACE_SECONDS:
        raise ValueError('Esa partida ya no se puede anular.')
    refunded = int(run.points_spent or 0)
    run_id, user_id = run.id, run.user_id   # se leen ANTES del contador: al guardarlo la sesión "vence" el objeto
    # Por hora y no por día: los contadores se borran a las 6 horas (locks.purge_stale_counters), una ventana de 24 h se perdería a media mañana.
    if not check_rate_limit(f'hordas_cancel_user:{user_id}', 5, 3600):
        raise ValueError('Ya anulaste varias partidas en la última hora. Espera un rato.')
    # Borrado condicional: si llegan dos anulaciones a la vez, solo una devuelve los puntos.
    deleted = HordeAccountRun.query.filter(HordeAccountRun.id == run_id, HordeAccountRun.finished_at.is_(None)).delete(synchronize_session=False)
    if not deleted:
        db.session.rollback()
        raise ValueError('Partida no encontrada.')
    if refunded > 0:
        account_points.credit(user_id, refunded, kind='hordas', note='Devolución de partida extra de Hordas de Diamantes')
    db.session.commit()
    return {'refunded_points': refunded, 'user_id': user_id}


# ───────────────────────── Ganadores y premios ─────────────────────────
def _fill_pending_winners(week_key):
    """Arma (o rearma) los ganadores PENDIENTES de una semana según el ranking, que ya excluye a los bloqueados. Los ya aprobados se respetan tal cual;
    los demás puestos con premio se llenan con los siguientes del ranking: si se rechaza al 1.º, el 2.º pasa a 1.º, el 4.º entra como 3.º, etc.
    No entrega nada: los puntos caen al aprobar."""
    prizes = get_settings()['prizes']
    paying = {place: points for place, points in enumerate(prizes, start=1) if points > 0}
    winners = HordeAccountWinner.query.filter_by(week_key=week_key).all()
    # Se respetan los aprobados Y los que se están aprobando ahora mismo ('approving': los puntos van en camino): borrarlos haría que esa cuenta entrara
    # de nuevo como pendiente y cobrara dos veces.
    approved = [w for w in winners if w.status != 'pending']
    for w in winners:
        if w.status == 'pending':
            db.session.delete(w)
    db.session.flush()
    if not paying:
        return

    # Cada cuenta ocupa su puesto real del ranking (aunque ese puesto no tenga premio). Los puestos ya aprobados quedan fijos.
    taken_places = {w.place for w in approved}
    taken_users = {w.user_id for w in approved}
    last_place = max(paying)
    place = 1
    for uid, total in week_ranking(week_key, limit=last_place + len(approved)):
        if uid in taken_users:
            continue
        while place in taken_places:
            place += 1
        if place > last_place:
            break
        if place in paying:
            db.session.add(HordeAccountWinner(week_key=week_key, place=place, user_id=uid, diamonds=total, points=paying[place], status='pending'))
        place += 1


def run_weekly_awards():
    """Cierra la semana que acaba de terminar (lunes a domingo, hora Venezuela): deja a los ganadores PENDIENTES DE APROBACIÓN. Los puntos no caen hasta
    que el admin revisa las repeticiones y aprueba. Idempotente: se marca cada semana ya cerrada; el lock evita dos cierres simultáneos."""
    previous_week_key = (_horde_week_start() - timedelta(days=7)).strftime('%Y-%m-%d')
    if previous_week_key < ACCOUNT_MODE_FIRST_WEEK:
        return
    marker = f'hordas_cuenta_awards:{previous_week_key}'
    if Setting.query.filter_by(key=marker).first():
        return
    lock_key = f'horde_award:{previous_week_key}'
    lock_holder = uuid4().hex
    if not acquire_lock(lock_key, 120, lock_holder):
        return
    try:
        if Setting.query.filter_by(key=marker).first():
            return
        _fill_pending_winners(previous_week_key)
        db.session.add(Setting(key=marker, value=datetime.utcnow().isoformat(), description='Hordas: ganadores de la semana armados'))
        db.session.commit()
    finally:
        db.session.rollback()
        release_lock(lock_key, lock_holder)


def rebuild_pending_winners(week_key):
    """Para el admin: vuelve a armar los ganadores pendientes de una semana (p. ej. si puso los premios después del cierre)."""
    lock_key = f'horde_award:{week_key}'
    holder = uuid4().hex
    if not acquire_lock(lock_key, 60, holder):
        raise ValueError('Se está revisando esta semana en otra pestaña. Intenta de nuevo.')
    try:
        _fill_pending_winners(week_key)
        db.session.commit()
    finally:
        db.session.rollback()
        release_lock(lock_key, holder)


def approve_winner(winner_id):
    """Aprueba un ganador pendiente y le da sus puntos en ese momento. El cambio de estado es condicional: aunque se toque 'Aprobar' dos veces seguidas,
    los puntos caen una sola vez."""
    winner = db.session.get(HordeAccountWinner, winner_id)
    if not winner:
        raise ValueError('Ganador no encontrado.')
    if winner.status == 'pending' and is_banned(winner.user_id):
        raise ValueError('Esta cuenta está bloqueada en Hordas. Usa "Rechazar" para que el premio pase al siguiente del ranking.')
    claimed = (
        HordeAccountWinner.query
        .filter(HordeAccountWinner.id == winner.id, HordeAccountWinner.status == 'pending')
        .update({'status': 'approving'}, synchronize_session=False)
    )
    db.session.commit()
    if not claimed:
        raise ValueError('Este premio ya fue revisado.')
    try:
        winner = db.session.get(HordeAccountWinner, winner_id)
        if winner.points > 0:
            account_points.credit(
                winner.user_id, winner.points, kind='hordas',
                note=f'Premio de Hordas de Diamantes: puesto #{winner.place} semana del {winner.week_key} ({winner.diamonds} cristales)',
            )
            db.session.add(AccountNotice(
                user_id=winner.user_id,
                message=f'🏆 ¡Ganaste {winner.points} puntos en Hordas de Diamantes (puesto #{winner.place})! Ya están en tu cuenta: gástalos en la tienda de puntos.'[:NOTICE_MAX],
            ))
        winner.status = 'approved'
        winner.reviewed_at = datetime.utcnow()
        # Los puntos y el estado se guardan en una sola transacción: si algo falla antes de este commit no cae nada y el ganador vuelve a pendiente.
        db.session.commit()
        return winner
    except Exception:
        db.session.rollback()
        HordeAccountWinner.query.filter_by(id=winner_id).update({'status': 'pending'}, synchronize_session=False)
        db.session.commit()
        raise


def ban_user(user_id, reason=''):
    """Bloquea una cuenta en Hordas: no juega, sale del ranking y no recibe premio. Devuelve False si ya estaba bloqueada."""
    if is_banned(user_id):
        return False
    db.session.add(HordeAccountBan(user_id=user_id, reason=(reason or 'Trampa')[:200]))
    db.session.commit()
    return True


def reject_winner(winner_id, reason=''):
    """Rechaza un ganador pendiente por trampa: se bloquea su cuenta (sale del ranking) y los siguientes suben de puesto."""
    winner = db.session.get(HordeAccountWinner, winner_id)
    if not winner or winner.status != 'pending':
        raise ValueError('Este premio ya fue revisado.')
    week_key, user_id = winner.week_key, winner.user_id
    lock_key = f'horde_award:{week_key}'
    lock_holder = uuid4().hex
    if not acquire_lock(lock_key, 60, lock_holder):
        raise ValueError('Se está revisando este premio en otra pestaña. Intenta de nuevo.')
    try:
        if not is_banned(user_id):
            db.session.add(HordeAccountBan(user_id=user_id, reason=(reason or f'Premio rechazado (semana {week_key})')[:200]))
            db.session.flush()
        _fill_pending_winners(week_key)
        db.session.commit()
    finally:
        db.session.rollback()
        release_lock(lock_key, lock_holder)


def refresh_pending_winners():
    """Después de bloquear una cuenta: los ganadores PENDIENTES se rearman sin ella y los siguientes del ranking suben de puesto. Los ya aprobados no se
    tocan. Devuelve cuántas semanas se revisaron."""
    weeks = [w for (w,) in db.session.query(HordeAccountWinner.week_key).filter(HordeAccountWinner.status == 'pending').distinct().all()]
    done = 0
    for week_key in weeks:
        lock_key = f'horde_award:{week_key}'
        holder = uuid4().hex
        if not acquire_lock(lock_key, 60, holder):
            continue
        try:
            _fill_pending_winners(week_key)
            db.session.commit()
            done += 1
        finally:
            db.session.rollback()
            release_lock(lock_key, holder)
    return done


def pending_winners():
    return HordeAccountWinner.query.filter_by(status='pending').order_by(HordeAccountWinner.week_key.desc(), HordeAccountWinner.place.asc()).all()
