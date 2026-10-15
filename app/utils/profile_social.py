"""Parte social del perfil público: estadísticas, nivel del mes, insignias, premios, me gusta, seguir y top de perfiles.

Reglas de la tienda (pedidas por el dueño y que NO se relajan aquí):
  * Nada de IDs, redes sociales ni comentarios en los perfiles. Aquí solo hay números agregados (diamantes, nivel, puestos).
  * Los números salen SOLO de las órdenes que la CUENTA compró con la sesión abierta (a cualquier ID). Las cuentas son independientes de los IDs
    (decisión del dueño, 2026-10-14): no hay IDs enlazados ni verificados, así nadie ve ni suma lo que compra otra persona.
  * Me gusta y seguir no dan diamantes ni descuentos: solo puntos de reputación.
"""
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from ..models import (
    ProfileAward, ProfileFollow, ProfileLike, ProfileStat, Order, Setting, User, UserBadge, UserProfile, db,
)
from .contact_filter import public_name
from .auth_accounts import ACCOUNT_KINDS, is_account_user
from .locks import acquire_lock, check_rate_limit, release_lock
from .official_profile import is_official_user
from .order_units import extract_order_units
from . import profile_xp as XP
from .timezone import now_ve, to_ve, ve_day_start_utc_naive

MONTHS_ES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']

# ───────────────────────── Niveles del mes ─────────────────────────
LEVELS = [
    # clave, nombre, color 1, color 2 del aro
    ('bronce', 'Bronce', '#cd7f32', '#8a4b14'),
    ('plata', 'Plata', '#e5e7eb', '#9ca3af'),
    ('oro', 'Oro', '#ffe08a', '#b8860b'),
    ('diamante', 'Diamante', '#67e8f9', '#3b82f6'),
    ('leyenda', 'Leyenda', '#ff1a2e', '#7a0010'),
]
LEVEL_KEYS = [level[0] for level in LEVELS]
DEFAULT_THRESHOLDS = {'bronce': 0, 'plata': 500, 'oro': 1500, 'diamante': 3000, 'leyenda': 6000}
SETTING_LEVELS = 'profile_level_thresholds'
SETTING_WHALE = 'profile_whale_threshold'
SETTING_LIKE_LIMIT = 'profile_daily_like_limit'
SETTING_REFRESHED = 'profile_stats_refreshed_at'
SETTING_MONTH_CLOSED = 'profile_month_closed'
DEFAULT_WHALE = 10000
DEFAULT_LIKE_LIMIT = 30
STATS_MAX_AGE_MINUTES = 15
# El recálculo normal lo hace el proceso de fondo cada STATS_MAX_AGE_MINUTES (utils/order_scheduler). Las páginas públicas solo lo
# disparan como red de seguridad si ese proceso lleva horas sin hacerlo, para que ninguna visita normal espere por él.
STATS_FALLBACK_MINUTES = 180
TOP_PAGE_SIZE = 25

# ───────────────────────── Insignias ─────────────────────────
BADGES = {
    'primera_recarga': {'name': 'Primera recarga', 'icon': '⚡', 'rarity': 'Común', 'desc': 'Completó su primera recarga.'},
    'recargas_10': {'name': '10 recargas', 'icon': '🔥', 'rarity': 'Común', 'desc': 'Llegó a 10 recargas completadas.'},
    'recargas_50': {'name': '50 recargas', 'icon': '💎', 'rarity': 'Rara', 'desc': 'Llegó a 50 recargas completadas.'},
    'fiel_3': {'name': 'Fiel', 'icon': '🛡️', 'rarity': 'Rara', 'desc': 'Recargó 3 meses seguidos.'},
    'veterano': {'name': 'Veterano', 'icon': '🎖️', 'rarity': 'Rara', 'desc': 'Lleva 1 año como cliente.'},
    'popular': {'name': 'Popular', 'icon': '❤️', 'rarity': 'Rara', 'desc': 'Recibió 50 me gusta en su perfil.'},
    'top3_mes': {'name': 'Top 3 del mes', 'icon': '🏆', 'rarity': 'Épica', 'desc': 'Quedó entre los 3 primeros del ranking de un mes.'},
    'ballena': {'name': 'Ballena', 'icon': '🐋', 'rarity': 'Legendaria', 'desc': 'Recargó muchísimo en un solo mes.'},
}
BADGE_ORDER = list(BADGES)
POPULAR_LIKES = 50


# ───────────────────────── Configuración editable (Admin → Perfiles) ─────────────────────────
def _get_setting(key, default=''):
    row = Setting.query.filter_by(key=key).first()
    return row.value if row is not None and row.value is not None else default


def _set_setting(key, value, description=''):
    row = Setting.query.filter_by(key=key).first()
    if row is None:
        db.session.add(Setting(key=key, value=str(value), description=description))
    else:
        row.value = str(value)


def get_thresholds():
    """Diamantes del mes que hacen falta para cada nivel (editable en el admin)."""
    try:
        data = json.loads(_get_setting(SETTING_LEVELS, '') or '{}')
    except (ValueError, TypeError):
        data = {}
    out = dict(DEFAULT_THRESHOLDS)
    if isinstance(data, dict):
        for key in LEVEL_KEYS:
            try:
                out[key] = max(0, int(data.get(key, out[key])))
            except (TypeError, ValueError):
                pass
    out['bronce'] = 0
    # deben ir de menor a mayor; si alguien los guardó desordenados se vuelve a los de fábrica
    values = [out[key] for key in LEVEL_KEYS]
    if any(values[i] >= values[i + 1] for i in range(len(values) - 1)):
        return dict(DEFAULT_THRESHOLDS)
    return out


def save_thresholds(raw):
    """Valida y guarda los umbrales. Devuelve el mensaje de error o '' si quedó guardado."""
    values = {}
    for key in LEVEL_KEYS:
        try:
            values[key] = int(str(raw.get(key, '')).strip())
        except (TypeError, ValueError):
            return 'Los niveles deben ser números enteros.'
        if values[key] < 0 or values[key] > 10_000_000:
            return 'Los niveles deben estar entre 0 y 10.000.000.'
    values['bronce'] = 0
    ordered = [values[key] for key in LEVEL_KEYS]
    if any(ordered[i] >= ordered[i + 1] for i in range(len(ordered) - 1)):
        return 'Cada nivel debe pedir más diamantes que el anterior (Bronce siempre es 0).'
    _set_setting(SETTING_LEVELS, json.dumps(values), 'Diamantes del mes para cada nivel de perfil')
    return ''


def get_int_setting(key, default, lo=1, hi=10_000_000):
    try:
        return max(lo, min(hi, int(_get_setting(key, default))))
    except (TypeError, ValueError):
        return default


def level_for(diamonds, thresholds=None):
    thresholds = thresholds or get_thresholds()
    current = LEVEL_KEYS[0]
    for key in LEVEL_KEYS:
        if int(diamonds or 0) >= thresholds[key]:
            current = key
    return current


def level_info(stat, thresholds=None):
    """Nivel del mes con su barra de progreso. None si la cuenta aún no tiene recargas verificadas."""
    if stat is None or not (stat.orders_count or 0):
        return None
    thresholds = thresholds or get_thresholds()
    key = stat.month_level or level_for(stat.month_diamonds, thresholds)
    index = LEVEL_KEYS.index(key) if key in LEVEL_KEYS else 0
    meta = LEVELS[index]
    month = int(stat.month_diamonds or 0)
    nxt = LEVELS[index + 1] if index + 1 < len(LEVELS) else None
    info = {
        'key': meta[0], 'name': meta[1], 'c1': meta[2], 'c2': meta[3], 'diamonds': month,
        'next_name': nxt[1] if nxt else None, 'next_target': thresholds[nxt[0]] if nxt else None,
        'remaining': (thresholds[nxt[0]] - month) if nxt else 0, 'percent': 100,
    }
    if nxt:
        base = thresholds[meta[0]]
        span = max(1, thresholds[nxt[0]] - base)
        info['percent'] = max(0, min(100, int((month - base) * 100 / span)))
    return info


def level_ring(level_key):
    """Aro automático según el nivel del mes (el dueño lo puede activar en el editor)."""
    for key, _name, c1, c2 in LEVELS:
        if key == level_key:
            return {
                'ring_style': 'gradient' if key != 'leyenda' else 'conic', 'ring_c1': c1, 'ring_c2': c2,
                'ring_width': 4 if key != 'leyenda' else 5, 'ring_glow': 8 + 2 * LEVEL_KEYS.index(key),
                'ring_anim': 'spin' if key == 'leyenda' else 'none',
            }
    return None


# ───────────────────────── Meses (hora de Venezuela) ─────────────────────────
def month_key_of(value):
    local = to_ve(value)
    return f'{local.year:04d}-{local.month:02d}' if local else ''


def month_key_now():
    now = now_ve()
    return f'{now.year:04d}-{now.month:02d}'


def prev_month_key(key):
    year, month = int(key[:4]), int(key[5:7])
    month -= 1
    if month == 0:
        year, month = year - 1, 12
    return f'{year:04d}-{month:02d}'


def next_month_key(key):
    year, month = int(key[:4]), int(key[5:7])
    month += 1
    if month == 13:
        year, month = year + 1, 1
    return f'{year:04d}-{month:02d}'


def month_bounds(key):
    from datetime import date
    start = date(int(key[:4]), int(key[5:7]), 1)
    nxt = next_month_key(key)
    return ve_day_start_utc_naive(start), ve_day_start_utc_naive(date(int(nxt[:4]), int(nxt[5:7]), 1))


def month_label(key):
    return f'{MONTHS_ES[int(key[5:7]) - 1].capitalize()} {key[:4]}'


# ───────────────────────── Órdenes verificadas ─────────────────────────
def verified_orders_query(user):
    """Órdenes completadas/aprobadas (y pagadas: no premios) que cuentan para la cuenta: las que ELLA compró con la sesión abierta, a cualquier
    ID (las pagó con su cuenta). Las cuentas son independientes de los IDs: ningún ID enlazado, verificado o con compras suma aquí."""
    return Order.query.filter(
        Order.account_user_id == user.id,
        Order.status.in_(['approved', 'completed']),
        or_(Order.payment_method.is_(None), Order.payment_method != 'prize'),
    )


def compute_numbers(user):
    numbers = {'total': 0, 'orders': 0, 'per_month': defaultdict(int), 'first_order_at': None, 'favorite': '', 'order_dates': []}
    per_product = {}   # nombre del paquete -> [cuántas órdenes, fecha de la última]
    query = verified_orders_query(user)
    if query is None:
        return numbers
    for order in query.order_by(Order.created_at.asc(), Order.id.asc()).all():
        units = extract_order_units(order)
        numbers['total'] += units
        numbers['orders'] += 1
        local = to_ve(order.created_at)
        if local is not None:
            numbers['order_dates'].append(local.date())   # día (hora de Venezuela) de cada recarga: lo usan las misiones del nivel por números
        numbers['per_month'][month_key_of(order.created_at)] += units
        if numbers['first_order_at'] is None:
            numbers['first_order_at'] = order.created_at
        name = ((order.package.name if order.package else '') or '').strip()
        if name:
            entry = per_product.setdefault(name, [0, order.created_at])
            entry[0] += 1
            entry[1] = order.created_at
    # Producto favorito: el que más compra; si empatan, el que compró más recientemente.
    if per_product:
        numbers['favorite'] = max(per_product.items(), key=lambda item: (item[1][0], item[1][1], item[0]))[0][:100]
    return numbers


def compute_streak(months, current_key):
    """Meses seguidos con al menos una recarga. El mes en curso no corta la racha mientras no termine."""
    key = current_key if current_key in months else prev_month_key(current_key)
    count = 0
    while key in months:
        count += 1
        key = prev_month_key(key)
    return count


# ───────────────────────── Cálculo y guardado ─────────────────────────
def social_counts(user_id):
    """Me gusta, seguidores, siguiendo y REPUTACIÓN (el puntaje social guardado al recalcular el perfil; se ve junto a los otros tres, como en el Top)."""
    stat = ProfileStat.query.filter_by(user_id=user_id).first()
    return {
        'likes': ProfileLike.query.filter_by(to_user_id=user_id).count(),
        'followers': ProfileFollow.query.filter_by(following_id=user_id).count(),
        'following': ProfileFollow.query.filter_by(follower_id=user_id).count(),
        'reputation': int((stat.reputation or 0) if stat is not None else 0),
    }


def _grant_badge(user_id, key):
    if UserBadge.query.filter_by(user_id=user_id, badge_key=key).first() is None:
        db.session.add(UserBadge(user_id=user_id, badge_key=key))
        return True
    return False


def profile_is_complete(profile):
    return bool(profile and profile.avatar_path and profile.banner_path and (profile.bio or profile.status_text))


def refresh_user(user, commit=True, xp_cfg=None):
    """Recalcula (y guarda) los números, las insignias, la reputación y los puntos del nivel por números de UNA cuenta."""
    now_key = month_key_now()
    xp_cfg = xp_cfg or XP.ensure_started()
    numbers = compute_numbers(user)
    stat = ProfileStat.query.filter_by(user_id=user.id).first()
    if stat is None:
        stat = ProfileStat(user_id=user.id)
        db.session.add(stat)
    thresholds = get_thresholds()
    stat.total_diamonds = numbers['total']
    stat.orders_count = numbers['orders']
    stat.month_key = now_key
    stat.month_diamonds = numbers['per_month'].get(now_key, 0)
    stat.best_month_diamonds = max(numbers['per_month'].values(), default=0)
    stat.streak_months = compute_streak(set(numbers['per_month']), now_key) if numbers['orders'] else 0
    stat.first_order_at = numbers['first_order_at']
    stat.favorite_product = numbers['favorite'] if numbers['orders'] else ''
    stat.month_level = level_for(stat.month_diamonds, thresholds) if numbers['orders'] else ''

    counts = social_counts(user.id)
    if numbers['orders'] >= 1:
        _grant_badge(user.id, 'primera_recarga')
    if numbers['orders'] >= 10:
        _grant_badge(user.id, 'recargas_10')
    if numbers['orders'] >= 50:
        _grant_badge(user.id, 'recargas_50')
    if stat.streak_months >= 3:
        _grant_badge(user.id, 'fiel_3')
    if numbers['first_order_at'] is not None and numbers['first_order_at'] <= datetime.utcnow() - timedelta(days=365):
        _grant_badge(user.id, 'veterano')
    if stat.best_month_diamonds >= get_int_setting(SETTING_WHALE, DEFAULT_WHALE):
        _grant_badge(user.id, 'ballena')
    if counts['likes'] >= POPULAR_LIKES:
        _grant_badge(user.id, 'popular')
    db.session.flush()

    badge_count = UserBadge.query.filter_by(user_id=user.id).count()
    top_months = ProfileAward.query.filter(ProfileAward.user_id == user.id, ProfileAward.kind.in_(['top1', 'top2', 'top3'])).count()
    profile = UserProfile.query.filter_by(user_id=user.id).first()
    stat.reputation = counts['likes'] * 1 + counts['followers'] * 2 + badge_count * 5 + top_months * 20 + (10 if profile_is_complete(profile) else 0)

    # Nivel por números (como Steam): los puntos se calculan desde los hechos, nunca se suman a mano (ver utils/profile_xp.py).
    old_level, had_xp = stat.xp_level or 1, (stat.xp_total or 0) > 0
    xp = XP.compute_xp(
        user.id, numbers['orders'], numbers['total'], numbers['order_dates'], stat.streak_months or 0, badge_count, top_months,
        profile_is_complete(profile), xp_cfg,
    )
    stat.xp_total = xp['total']
    stat.xp_level = XP.level_for_xp(stat.xp_total, xp_cfg)
    if had_xp:   # la primera vez no hay nivel anterior con el que comparar (y así nadie recibe un aviso falso al estrenar el sistema)
        XP.level_up_notice(user.id, old_level, stat.xp_level)
    stat.updated_at = datetime.utcnow()
    if commit:
        db.session.commit()
    return stat


def xp_details_for(user, cfg=None):
    """Solo para el dueño: de dónde salen sus puntos del nivel por números y sus misiones de hoy / esta semana (se calcula al abrir su perfil)."""
    cfg = cfg or XP.get_config()
    numbers = compute_numbers(user)
    stat = ProfileStat.query.filter_by(user_id=user.id).first()
    streak = (stat.streak_months or 0) if stat else 0
    badge_count = UserBadge.query.filter_by(user_id=user.id).count()
    top_months = ProfileAward.query.filter(ProfileAward.user_id == user.id, ProfileAward.kind.in_(['top1', 'top2', 'top3'])).count()
    profile = UserProfile.query.filter_by(user_id=user.id).first()
    result = XP.compute_xp(
        user.id, numbers['orders'], numbers['total'], numbers['order_dates'], streak, badge_count, top_months, profile_is_complete(profile), cfg,
    )
    return {
        'parts': [part for part in result['parts'] if part['xp'] or part['count']],
        'missions': XP.mission_overview(result['events'], cfg),
    }


def _eligible(profile, user):
    """Sale en los rankings: cuenta activa, perfil público, no oculto por el admin y con "aparecer en rankings" encendido."""
    if not user.is_active:
        return False
    if profile is None:
        return True
    return bool(profile.is_public is not False and not profile.moderation_hidden and profile.in_rankings is not False)


def recompute_ranks(commit=True):
    rows = (
        db.session.query(ProfileStat, UserProfile, User)
        .join(User, User.id == ProfileStat.user_id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .all()
    )
    items = [(stat, profile, user) for stat, profile, user in rows if _eligible(profile, user)]
    for stat, _profile, _user in rows:
        stat.month_rank = stat.all_time_rank = stat.reputation_rank = stat.xp_rank = None

    def assign(attr, candidates, keyfunc):
        for position, (stat, _p, _u) in enumerate(sorted(candidates, key=keyfunc), start=1):
            setattr(stat, attr, position)

    far = datetime(2100, 1, 1)
    assign('month_rank', [i for i in items if (i[0].month_diamonds or 0) > 0],
           lambda i: (-(i[0].month_diamonds or 0), -(i[0].total_diamonds or 0), i[0].first_order_at or far, i[0].user_id))
    assign('all_time_rank', [i for i in items if (i[0].total_diamonds or 0) > 0],
           lambda i: (-(i[0].total_diamonds or 0), i[0].first_order_at or far, i[0].user_id))
    assign('reputation_rank', [i for i in items if (i[0].reputation or 0) > 0],
           lambda i: (-(i[0].reputation or 0), -(i[0].total_diamonds or 0), i[0].user_id))
    assign('xp_rank', [i for i in items if (i[0].xp_total or 0) > 0],
           lambda i: (-(i[0].xp_total or 0), -(i[0].total_diamonds or 0), i[0].user_id))
    if commit:
        db.session.commit()


def _month_totals_for(user, start, end):
    query = verified_orders_query(user)
    if query is None:
        return 0
    total = 0
    for order in query.filter(Order.created_at >= start, Order.created_at < end).all():
        total += extract_order_units(order)
    return total


def month_candidates(key):
    """Cuentas que pueden salir en rankings y recargaron en el mes `key` ('AAAA-MM'), de más a menos: [(diamantes, cuenta)]."""
    start, end = month_bounds(key)
    candidates = []
    for user in User.query.filter(User.account_kind.in_(ACCOUNT_KINDS), User.is_active.is_(True)).all():
        profile = UserProfile.query.filter_by(user_id=user.id).first()
        if not _eligible(profile, user):
            continue
        total = _month_totals_for(user, start, end)
        if total > 0:
            candidates.append((total, user))
    candidates.sort(key=lambda item: (-item[0], item[1].id))
    return candidates


def award_month(key):
    """Cierra un mes: da la insignia "Top 3 del mes" y el premio de la vitrina (Campeón al primero) a los 3 mejores."""
    candidates = month_candidates(key)
    month_name = MONTHS_ES[int(key[5:7]) - 1].capitalize()
    for position, (total, user) in enumerate(candidates[:3], start=1):
        kind = f'top{position}'
        _grant_badge(user.id, 'top3_mes')
        exists = ProfileAward.query.filter_by(user_id=user.id, kind=kind, month_key=key).first()
        if exists is None:
            title = f'Campeón de {month_name}' if position == 1 else f'Top {position} de {month_name}'
            db.session.add(ProfileAward(
                user_id=user.id, title=title, description=f'Puesto {position} del ranking de {month_label(key)}.',
                kind=kind, month_key=key, is_title=(position == 1), awarded_at=datetime.utcnow(),
            ))
    db.session.flush()


def close_months():
    """Al empezar un mes se cierra el anterior (y los que falten si nadie entró en esos días). La primera vez solo fija el punto de partida."""
    target = prev_month_key(month_key_now())
    closed = _get_setting(SETTING_MONTH_CLOSED, '')
    if not closed:
        _set_setting(SETTING_MONTH_CLOSED, target, 'Último mes cerrado de los perfiles')
        db.session.flush()
        return
    key, guard = next_month_key(closed), 0
    while key <= target and guard < 12:
        award_month(key)
        _set_setting(SETTING_MONTH_CLOSED, key, 'Último mes cerrado de los perfiles')
        key, guard = next_month_key(key), guard + 1
    db.session.flush()


def refresh_all():
    """Recalcula TODO (cada cierto tiempo): cierre de mes, números de cada cuenta y puestos. Un solo proceso a la vez."""
    holder = uuid.uuid4().hex
    if not acquire_lock('profile_stats_refresh', 900, holder):
        return False
    try:
        close_months()
        xp_cfg = XP.ensure_started()
        for user in User.query.filter(User.account_kind.in_(ACCOUNT_KINDS), User.is_active.is_(True)).all():
            refresh_user(user, commit=False, xp_cfg=xp_cfg)
        db.session.flush()
        recompute_ranks(commit=False)
        _set_setting(SETTING_REFRESHED, datetime.utcnow().isoformat(), 'Última vez que se recalcularon los perfiles')
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        raise
    finally:
        release_lock('profile_stats_refresh', holder)


def ensure_fresh(max_age_minutes=STATS_MAX_AGE_MINUTES):
    """Recalcula si pasaron más de `max_age_minutes` desde la última vez. Barato cuando todavía está fresco."""
    raw = _get_setting(SETTING_REFRESHED, '')
    try:
        last = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        last = None
    if last is not None and datetime.utcnow() - last < timedelta(minutes=max_age_minutes):
        return False
    return refresh_all()


# ───────────────────────── Rankings de CUENTAS (las que más recargan) y de NIVELES ─────────────────────────
# Los crea y los premia el dueño en Admin → Dinámicas → Ranking (utils/ranking_boards.py); aquí solo están las listas. Salen en el Ranking de la
# página principal.
ACCOUNT_RANKING_SIZE = 10


def _account_ranking_query():
    """Cuentas del mes en curso con diamantes, activas y que permiten salir en rankings (misma regla que el Top de perfiles)."""
    return (
        db.session.query(ProfileStat, User, UserProfile)
        .join(User, User.id == ProfileStat.user_id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .filter(
            ProfileStat.month_key == month_key_now(), ProfileStat.month_diamonds > 0, ProfileStat.month_rank.isnot(None),
            User.is_active.is_(True),
            or_(
                UserProfile.user_id.is_(None),
                (or_(UserProfile.is_public.is_(None), UserProfile.is_public.is_(True))
                 & or_(UserProfile.moderation_hidden.is_(None), UserProfile.moderation_hidden.is_(False))
                 & or_(UserProfile.in_rankings.is_(None), UserProfile.in_rankings.is_(True))),
            ),
        )
    )


def _shown_name(user, profile):
    return public_name(profile.display_name, user.username) if profile and profile.display_name else user.username


def account_month_ranking(limit=ACCOUNT_RANKING_SIZE, reveal=False):
    """Top del mes de cuentas: [{'position', 'user_id', 'name', 'handle', 'avatar', 'total_units'}]. `total_units` es None si la cuenta apagó
    "mostrar mis estadísticas" (salvo con `reveal=True`, que es solo para el administrador). Solo cuentas con nombre de usuario elegido (las demás
    no tienen página pública)."""
    from .profiles import media_url_path

    rows = []
    for stat, user, profile in _account_ranking_query().order_by(ProfileStat.month_rank.asc(), ProfileStat.user_id.asc()).limit(limit * 3).all():
        if not user.has_chosen_username:
            continue
        shows = reveal or profile is None or profile.show_stats is not False
        rows.append({
            'position': len(rows) + 1, 'user_id': user.id, 'name': _shown_name(user, profile), 'handle': user.username,
            'avatar': (media_url_path(profile.avatar_path) or '') if (profile and profile.avatar_path) else '',
            'total_units': stat.month_diamonds if shows else None,
        })
        if len(rows) >= limit:
            break
    return rows


def account_my_position(user):
    """{'position', 'total_units'} de la cuenta en el ranking del mes, o None si todavía no entra (sin compras este mes, perfil privado...)."""
    if not is_account_user(user):
        return None
    stat = ProfileStat.query.filter_by(user_id=user.id).first()
    if stat is None or stat.month_key != month_key_now() or not (stat.month_diamonds or 0) > 0 or stat.month_rank is None:
        return None
    return {'position': stat.month_rank, 'total_units': stat.month_diamonds}


def _account_ranking_query_any_month():
    """Cuentas con puntos de nivel, activas y que permiten salir en rankings (misma regla que el Top de perfiles); no piden recargas este mes."""
    return (
        db.session.query(ProfileStat, User, UserProfile)
        .join(User, User.id == ProfileStat.user_id)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .filter(
            User.is_active.is_(True), ProfileStat.xp_rank.isnot(None), ProfileStat.xp_total > 0,
            or_(
                UserProfile.user_id.is_(None),
                (or_(UserProfile.is_public.is_(None), UserProfile.is_public.is_(True))
                 & or_(UserProfile.moderation_hidden.is_(None), UserProfile.moderation_hidden.is_(False))
                 & or_(UserProfile.in_rankings.is_(None), UserProfile.in_rankings.is_(True))),
            ),
        )
    )


def level_ranking(limit=ACCOUNT_RANKING_SIZE, reveal=False):
    """Top de cuentas por nivel: [{'position', 'user_id', 'name', 'handle', 'avatar', 'total_units' (puntos de nivel), 'level'}]. `total_units` y
    `level` son None si la cuenta apagó "mostrar mis estadísticas" (salvo con `reveal=True`, solo para el administrador). Solo cuentas con
    nombre de usuario elegido."""
    from .profiles import media_url_path

    rows = []
    for stat, user, profile in _account_ranking_query_any_month().order_by(ProfileStat.xp_rank.asc(), ProfileStat.user_id.asc()).limit(limit * 3).all():
        if not user.has_chosen_username:
            continue
        shows = reveal or profile is None or profile.show_stats is not False
        rows.append({
            'position': len(rows) + 1, 'user_id': user.id, 'name': _shown_name(user, profile), 'handle': user.username,
            'avatar': (media_url_path(profile.avatar_path) or '') if (profile and profile.avatar_path) else '',
            'total_units': stat.xp_total if shows else None, 'level': (stat.xp_level or 1) if shows else None,
        })
        if len(rows) >= limit:
            break
    return rows


def level_my_position(user):
    """{'position', 'total_units', 'level'} de la cuenta en el ranking de niveles, o None si todavía no entra."""
    if not is_account_user(user):
        return None
    stat = ProfileStat.query.filter_by(user_id=user.id).first()
    if stat is None or stat.xp_rank is None or not (stat.xp_total or 0) > 0:
        return None
    return {'position': stat.xp_rank, 'total_units': stat.xp_total, 'level': stat.xp_level or 1}


def level_candidates(limit=ACCOUNT_RANKING_SIZE):
    """Para el cierre de mes: las cuentas con más nivel ahora mismo, [(puntos de nivel, cuenta)] (solo con usuario elegido)."""
    found = []
    for stat, user, _profile in _account_ranking_query_any_month().order_by(ProfileStat.xp_rank.asc(), ProfileStat.user_id.asc()).limit(limit * 3).all():
        if user.has_chosen_username:
            found.append((stat.xp_total or 0, user))
        if len(found) >= limit:
            break
    return found


# ───────────────────────── Buscador de perfiles (buscar amigos por usuario) ─────────────────────────
SEARCH_MIN = 2
SEARCH_MAX = 20
SEARCH_LIMIT = 8


def search_accounts(query, limit=SEARCH_LIMIT):
    """Cuentas cuyo NOMBRE DE USUARIO contiene lo escrito (sin importar mayúsculas), para buscar amigos: [{'name', 'handle', 'avatar'}].
    Solo salen perfiles que cualquiera puede abrir (cuenta activa, con usuario elegido, perfil público y no oculto por el administrador): lo que
    saldría en /u/<usuario>. Primero el usuario exacto, luego los que empiezan igual y después los demás."""
    from .profiles import media_url_path

    text = str(query or '').strip().lstrip('@')[:SEARCH_MAX].lower()
    if len(text) < SEARCH_MIN:
        return []
    escaped = text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    rows = (
        db.session.query(User, UserProfile)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .filter(
            User.account_kind.in_(ACCOUNT_KINDS), User.is_active.is_(True),
            func.lower(User.username).like(f'%{escaped}%', escape='\\'),
            or_(
                UserProfile.user_id.is_(None),
                (or_(UserProfile.is_public.is_(None), UserProfile.is_public.is_(True))
                 & or_(UserProfile.moderation_hidden.is_(None), UserProfile.moderation_hidden.is_(False))),
            ),
        )
        .limit(limit * 6)
        .all()
    )
    found = []
    for user, profile in rows:
        if not user.has_chosen_username:
            continue
        handle = user.username
        low = handle.lower()
        order = 0 if low == text else (1 if low.startswith(text) else 2)
        found.append((order, len(handle), low, {
            'name': _shown_name(user, profile), 'handle': handle, 'official': is_official_user(user),
            'avatar': (media_url_path(profile.avatar_path) or '') if (profile and profile.avatar_path) else '',
        }))
    found.sort(key=lambda item: item[:3])
    return [item[3] for item in found[:limit]]


# ───────────────────────── Me gusta y seguir ─────────────────────────
class SocialError(Exception):
    pass


def viewer_gate(viewer):
    """Dar me gusta o seguir pide una CUENTA (no hace falta haber comprado: la idea es que la gente entre, se cree su perfil y traiga a
    sus amigos antes de recargar). Lo que frena el abuso son los topes por minuto y por día."""
    if viewer is None or not getattr(viewer, 'is_authenticated', False):
        raise SocialError('Inicia sesión con tu cuenta para dar me gusta o seguir.')
    if not is_account_user(viewer):
        raise SocialError('Crea una cuenta para dar me gusta o seguir.')


def target_visible(target, profile):
    return bool(target.is_active and target.has_chosen_username and (profile is None or (profile.is_public is not False and not profile.moderation_hidden)))


def toggle_like(viewer, target):
    viewer_gate(viewer)
    profile = UserProfile.query.filter_by(user_id=target.id).first()
    if viewer.id == target.id:
        raise SocialError('No puedes darte me gusta a ti mismo.')
    if not target_visible(target, profile):
        raise SocialError('Ese perfil no está disponible.')
    if not check_rate_limit(f'profile_like:{viewer.id}', 20, 60):
        raise SocialError('Vas muy rápido. Espera un momento.')
    existing = ProfileLike.query.filter_by(from_user_id=viewer.id, to_user_id=target.id).first()
    if existing is not None:
        db.session.delete(existing)
        liked = False
    else:
        day_start = ve_day_start_utc_naive(now_ve().date())
        today = ProfileLike.query.filter(ProfileLike.from_user_id == viewer.id, ProfileLike.created_at >= day_start).count()
        if today >= get_int_setting(SETTING_LIKE_LIMIT, DEFAULT_LIKE_LIMIT):
            raise SocialError('Llegaste al límite de me gusta de hoy. Vuelve mañana.')
        db.session.add(ProfileLike(from_user_id=viewer.id, to_user_id=target.id))
        liked = True
    db.session.commit()
    refresh_user(target)
    return liked


def toggle_follow(viewer, target):
    viewer_gate(viewer)
    profile = UserProfile.query.filter_by(user_id=target.id).first()
    if viewer.id == target.id:
        raise SocialError('No puedes seguirte a ti mismo.')
    if not target_visible(target, profile):
        raise SocialError('Ese perfil no está disponible.')
    if not check_rate_limit(f'profile_follow:{viewer.id}', 20, 60):
        raise SocialError('Vas muy rápido. Espera un momento.')
    existing = ProfileFollow.query.filter_by(follower_id=viewer.id, following_id=target.id).first()
    if existing is not None:
        db.session.delete(existing)
        following = False
    else:
        db.session.add(ProfileFollow(follower_id=viewer.id, following_id=target.id))
        following = True
    db.session.commit()
    refresh_user(target)
    return following


# ───────────────────────── Listas ─────────────────────────
def badge_cards(user_id):
    earned = {b.badge_key: b for b in UserBadge.query.filter_by(user_id=user_id).all()}
    cards = []
    for key in BADGE_ORDER:
        if key in earned:
            meta = BADGES[key]
            desc = meta['desc']
            if key == 'ballena':
                desc = f"Recargó más de {get_int_setting(SETTING_WHALE, DEFAULT_WHALE):,} diamantes en un solo mes.".replace(',', '.')
            cards.append({'key': key, 'name': meta['name'], 'icon': meta['icon'], 'rarity': meta['rarity'], 'desc': desc, 'earned_at': earned[key].earned_at})
    return cards


def top_rows(kind, page=1):
    """Filas del top de perfiles. kind: 'mes' | 'historico' | 'reputacion' | 'nivel'. Devuelve (filas, total, página, total_páginas)."""
    attr, value_attr = {
        'mes': ('month_rank', 'month_diamonds'),
        'historico': ('all_time_rank', 'total_diamonds'),
        'reputacion': ('reputation_rank', 'reputation'),
        'nivel': ('xp_rank', 'xp_total'),
    }[kind]
    column = getattr(ProfileStat, attr)
    query = db.session.query(ProfileStat, User, UserProfile).join(User, User.id == ProfileStat.user_id).outerjoin(UserProfile, UserProfile.user_id == User.id).filter(column.isnot(None))
    # Las reglas de quién puede salir (misma regla que _eligible) también se aplican al LEER: si el admin oculta o desactiva una cuenta,
    # o su dueño apaga "aparecer en rankings", deja de verse en el Top al instante, sin esperar al próximo recálculo de puestos.
    query = query.filter(
        User.is_active.is_(True),
        or_(
            UserProfile.user_id.is_(None),
            (or_(UserProfile.is_public.is_(None), UserProfile.is_public.is_(True))
             & or_(UserProfile.moderation_hidden.is_(None), UserProfile.moderation_hidden.is_(False))
             & or_(UserProfile.in_rankings.is_(None), UserProfile.in_rankings.is_(True))),
        ),
    )
    total = query.count()
    pages = max(1, (total + TOP_PAGE_SIZE - 1) // TOP_PAGE_SIZE)
    page = min(max(int(page or 1), 1), pages)
    thresholds = get_thresholds()
    rows = []
    page_items = query.order_by(column.asc()).offset((page - 1) * TOP_PAGE_SIZE).limit(TOP_PAGE_SIZE).all()
    for stat, user, profile in page_items:
        if not user.has_chosen_username:
            continue
        shows_stats = profile is None or profile.show_stats is not False
        info = level_info(stat, thresholds) if shows_stats else None
        rows.append({
            'rank': getattr(stat, attr), 'user': user, 'profile': profile, 'level': info,
            'xp': XP.badge_for(stat.xp_level) if shows_stats else None,
            'name': public_name(profile.display_name, user.username) if profile and profile.display_name else user.username,
            'avatar': profile.avatar_path if profile else '',
            'value': getattr(stat, value_attr) if (kind == 'reputacion' or shows_stats) else None,
        })
    return rows, total, page, pages


def mini_card(user):
    profile = UserProfile.query.filter_by(user_id=user.id).first()
    stat = ProfileStat.query.filter_by(user_id=user.id).first()
    shows = profile is None or profile.show_stats is not False
    return {
        'user': user, 'profile': profile,
        'name': public_name(profile.display_name, user.username) if profile and profile.display_name else user.username,
        'avatar': profile.avatar_path if profile else '',
        'level': level_info(stat, get_thresholds()) if shows else None,
        'xp': XP.badge_for(stat.xp_level) if (shows and stat is not None and (stat.xp_total or 0) > 0) else None,
    }


def network_users(user, kind):
    """Cuentas que sigue ('siguiendo') o que la siguen ('seguidores'); solo perfiles visibles."""
    if kind == 'seguidores':
        ids = [row.follower_id for row in ProfileFollow.query.filter_by(following_id=user.id).order_by(ProfileFollow.created_at.desc()).all()]
    else:
        ids = [row.following_id for row in ProfileFollow.query.filter_by(follower_id=user.id).order_by(ProfileFollow.created_at.desc()).all()]
    cards = []
    for uid in ids:
        other = db.session.get(User, uid)
        if other is None or not other.is_active or not other.has_chosen_username:
            continue
        profile = UserProfile.query.filter_by(user_id=other.id).first()
        if not target_visible(other, profile):
            continue
        cards.append(mini_card(other))
    return cards
