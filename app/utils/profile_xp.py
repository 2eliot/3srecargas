"""Nivel POR NÚMEROS (como Steam): puntos de experiencia (XP) que solo suben con la actividad.

Es independiente del "nivel del mes" (Bronce... Leyenda), que se queda como estaba y se reinicia cada mes. Este nivel es permanente: Nivel 1, 2, 3...

Cómo funciona (decidido con el dueño, 2026-10-13):
  * El administrador pone cuántos puntos da cada acción (cada recarga, cada me gusta, cada seguidor...), cuántos puntos hace falta para cada
    nivel (una curva de 3 números) y las misiones diarias y semanales. Todo vive en un ajuste (`profile_xp_config`) y se edita en
    Admin → Cuentas → Ajustes de perfiles.
  * Los puntos se CALCULAN desde los hechos (compras de la cuenta, me gusta, seguidores, insignias...), no se van sumando a mano. Las cuentas son
    independientes de los IDs (2026-10-14): lo que se juega con un ID (Hordas, Sorteo, Adivina) ya no da puntos de nivel.
    Así quitar un me gusta o dejar de seguir quita sus puntos (no se puede hacer trampa activando y desactivando) y, si el administrador cambia
    los números, todos los niveles se recalculan.
  * Las acciones de comunidad y de juego tienen un TOPE DIARIO (editable) para que no se pueda inflar el nivel con cuentas falsas.
  * Las misiones cuentan solo desde la fecha en que se activó el sistema (`start`): no regalan puntos por el pasado.
  * El nivel se ve al lado del nombre con un color por tramos de 10 niveles (como Steam). Sin premios: solo el número y el color.
"""
import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func, or_

from ..models import AccountNotice, ProfileFollow, ProfileLike, Setting, db
from .timezone import now_ve, to_ve

SETTING_XP = 'profile_xp_config'
SETTING_XP_DESC = 'Niveles por números (XP): puntos por acción, curva y misiones'

# (clave, texto, ayuda, puntos por defecto, tope diario por defecto o None = esta acción no tiene tope)
POINT_DEFS = [
    ('order', 'Cada recarga', 'Cada recarga completada que hace la cuenta (a cualquier ID).', 30, None),
    ('per_100_units', 'Cada 100 diamantes (u oro) recargados', 'Se suma además de los puntos de la recarga: recargar más da más.', 2, None),
    ('like_received', 'Cada me gusta que recibes', 'Un me gusta de otra cuenta a tu perfil.', 3, 10),
    ('like_given', 'Cada me gusta que das', 'Un me gusta tuyo al perfil de otra cuenta.', 1, 10),
    ('follower_received', 'Cada seguidor nuevo', 'Otra cuenta empieza a seguirte.', 5, 10),
    ('follow_given', 'Cada persona que sigues', 'Tú empiezas a seguir a otra cuenta.', 1, 10),
    ('profile_complete', 'Perfil completo (una sola vez)', 'Foto, portada y una frase o "acerca de mí".', 50, None),
    ('badge', 'Cada insignia ganada', 'Primera recarga, 10 recargas, Fiel, Popular...', 25, None),
    ('streak_month', 'Cada mes de racha recargando', 'Por cada mes seguido con recarga (la racha actual).', 15, None),
    ('top3', 'Cada vez que quedas en el Top 3 del mes', 'Por cada premio de Top 1, 2 o 3 del ranking mensual.', 100, None),
]
POINT_KEYS = [d[0] for d in POINT_DEFS]
CAP_KEYS = [d[0] for d in POINT_DEFS if d[4] is not None]
POINT_TEXT = {d[0]: d[1] for d in POINT_DEFS}
CAPPED_EVENTS = ('like_received', 'like_given', 'follower_received', 'follow_given')
POINT_MAX = 100_000
CAP_MAX = 1_000

# Misiones: se reinician cada día o cada semana (lunes a domingo, hora de Venezuela). Se completan solas al cumplir la meta.
# (clave, periodo, qué se cuenta, texto con {n}, meta por defecto, puntos por defecto)
MISSION_DEFS = [
    ('d_likes', 'day', 'like_given', 'Da {n} me gusta a otros perfiles hoy', 3, 8),
    ('d_follow', 'day', 'follow_given', 'Sigue a {n} persona{s} hoy', 1, 5),
    ('w_recharge', 'week', 'order', 'Haz {n} recarga{s} esta semana', 1, 50),
    ('w_recharge3', 'week', 'order', 'Reto semanal: haz {n} recarga{s}', 3, 120),
    ('w_likes', 'week', 'like_received', 'Recibe {n} me gusta esta semana', 5, 30),
    ('w_followers', 'week', 'follower_received', 'Gana {n} seguidor{es} esta semana', 3, 40),
]
MISSION_KEYS = [m[0] for m in MISSION_DEFS]


def mission_text(label, n):
    """El texto de una misión con su meta y bien conjugado: {n} = la meta, {s} = "s" si son varias, {es} = "es", {vez} = "vez"/"veces"."""
    one = str(n) == '1'
    return label.format(n=n, s='' if one else 's', es='' if one else 'es', vez='vez' if one else 'veces')


MISSION_TARGET_MAX = 1_000

CURVE_DEFAULT = {'first': 100, 'step': 10, 'max_level': 100}
CURVE_LIMITS = {'first': (1, 1_000_000), 'step': (0, 1_000_000), 'max_level': (10, 500)}
CURVE_LABELS = {'first': 'Puntos para pasar del nivel 1 al 2', 'step': 'Cuánto más cuesta cada nivel', 'max_level': 'Nivel máximo'}

# Colores por tramos de 10 niveles (como Steam): desde qué nivel, nombre, color 1, color 2
TIERS = [
    (1, 'Gris', '#9ca3af', '#4b5563'),
    (10, 'Rojo', '#ff5a6a', '#a3101f'),
    (20, 'Naranja', '#fb923c', '#c2410c'),
    (30, 'Amarillo', '#facc15', '#a16207'),
    (40, 'Verde', '#4ade80', '#15803d'),
    (50, 'Azul', '#60a5fa', '#1d4ed8'),
    (60, 'Violeta', '#a78bfa', '#6d28d9'),
    (70, 'Rosa', '#f472b6', '#be185d'),
    (80, 'Turquesa', '#22d3ee', '#0e7490'),
    (90, 'Oro', '#fde047', '#b45309'),
    (100, 'Leyenda', '#ffffff', '#f59e0b'),
]


# ───────────────────────── Ajustes (los pone el administrador) ─────────────────────────
def _clamp_int(raw, lo, hi, default):
    try:
        number = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def today_ve():
    return now_ve().date()


def default_config():
    return {
        'points': {key: default for key, _t, _h, default, _c in POINT_DEFS},
        'caps': {key: cap for key, _t, _h, _d, cap in POINT_DEFS if cap is not None},
        'curve': dict(CURVE_DEFAULT),
        'missions': {key: {'enabled': True, 'target': target, 'xp': xp} for key, _p, _m, _l, target, xp in MISSION_DEFS},
        'start': '',
    }


def get_config():
    """La configuración vigente: lo guardado por el administrador sobre los valores de fábrica (siempre completa y con números válidos)."""
    cfg = default_config()
    row = Setting.query.filter_by(key=SETTING_XP).first()
    try:
        data = json.loads((row.value if row is not None else '') or '{}')
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    saved_points = data.get('points') if isinstance(data.get('points'), dict) else {}
    for key in POINT_KEYS:
        if key in saved_points:
            cfg['points'][key] = _clamp_int(saved_points[key], 0, POINT_MAX, cfg['points'][key])
    saved_caps = data.get('caps') if isinstance(data.get('caps'), dict) else {}
    for key in CAP_KEYS:
        if key in saved_caps:
            cfg['caps'][key] = _clamp_int(saved_caps[key], 0, CAP_MAX, cfg['caps'][key])
    saved_curve = data.get('curve') if isinstance(data.get('curve'), dict) else {}
    for key, (lo, hi) in CURVE_LIMITS.items():
        if key in saved_curve:
            cfg['curve'][key] = _clamp_int(saved_curve[key], lo, hi, cfg['curve'][key])
    saved_missions = data.get('missions') if isinstance(data.get('missions'), dict) else {}
    for key in MISSION_KEYS:
        mission = saved_missions.get(key)
        if isinstance(mission, dict):
            cfg['missions'][key] = {
                'enabled': bool(mission.get('enabled', True)),
                'target': _clamp_int(mission.get('target'), 1, MISSION_TARGET_MAX, cfg['missions'][key]['target']),
                'xp': _clamp_int(mission.get('xp'), 0, POINT_MAX, cfg['missions'][key]['xp']),
            }
    start = str(data.get('start') or '')
    try:
        date.fromisoformat(start)
    except ValueError:
        start = ''
    cfg['start'] = start
    return cfg


def _store(cfg):
    row = Setting.query.filter_by(key=SETTING_XP).first()
    text = json.dumps(cfg, ensure_ascii=False)
    if row is None:
        db.session.add(Setting(key=SETTING_XP, value=text, description=SETTING_XP_DESC))
    else:
        row.value = text


def mission_start(cfg):
    """Desde qué día cuentan las misiones (hoy, mientras nadie lo haya fijado)."""
    try:
        return date.fromisoformat(cfg.get('start') or '')
    except ValueError:
        return today_ve()


def ensure_started():
    """La configuración vigente, fijando (una sola vez) el día en que arrancó el sistema: las misiones no dan puntos por días anteriores.
    No hace commit: quien la llama guarda después."""
    cfg = get_config()
    if not cfg['start']:
        cfg['start'] = today_ve().isoformat()
        _store(cfg)
        db.session.flush()
    return cfg


def save_config(form):
    """Valida y guarda lo que manda el formulario del administrador. Devuelve el mensaje de error o '' si quedó guardado."""
    old = get_config()
    cfg = default_config()
    for key, text, _help, _default, _cap in POINT_DEFS:
        raw = str(form.get(f'xp_points_{key}', '')).strip()
        if not raw.isdigit():
            return f'"{text}": escribe un número entero (0 si no quieres que dé puntos).'
        if int(raw) > POINT_MAX:
            return f'"{text}": no puede pasar de {POINT_MAX:,}.'.replace(',', '.')
        cfg['points'][key] = int(raw)
    for key in CAP_KEYS:
        raw = str(form.get(f'xp_cap_{key}', '')).strip()
        if not raw.isdigit() or int(raw) > CAP_MAX:
            return f'Tope diario de "{POINT_TEXT[key]}": escribe un número entre 0 (sin tope) y {CAP_MAX}.'
        cfg['caps'][key] = int(raw)
    for key, (lo, hi) in CURVE_LIMITS.items():
        raw = str(form.get(f'xp_curve_{key}', '')).strip()
        if not raw.isdigit() or not (lo <= int(raw) <= hi):
            return f'"{CURVE_LABELS[key]}": debe ser un número entre {lo:,} y {hi:,}.'.replace(',', '.')
        cfg['curve'][key] = int(raw)
    for key, _period, _metric, label, _t, _x in MISSION_DEFS:
        raw_target = str(form.get(f'xp_m_{key}_target', '')).strip()
        raw_xp = str(form.get(f'xp_m_{key}_xp', '')).strip()
        name = mission_text(label, 'N')
        if not raw_target.isdigit() or not (1 <= int(raw_target) <= MISSION_TARGET_MAX):
            return f'Misión "{name}": la meta debe ser un número entre 1 y {MISSION_TARGET_MAX}.'
        if not raw_xp.isdigit() or int(raw_xp) > POINT_MAX:
            return f'Misión "{name}": los puntos no pueden pasar de {POINT_MAX:,}.'.replace(',', '.')
        cfg['missions'][key] = {'enabled': form.get(f'xp_m_{key}_on') == '1', 'target': int(raw_target), 'xp': int(raw_xp)}
    cfg['start'] = old['start'] or today_ve().isoformat()
    _store(cfg)
    db.session.commit()
    return ''


# ───────────────────────── Niveles ─────────────────────────
def xp_to_reach(level, cfg=None):
    """Puntos TOTALES necesarios para estar en ese nivel (el nivel 1 es el de partida: 0 puntos)."""
    curve = (cfg or get_config())['curve']
    n = max(0, int(level) - 1)
    return n * curve['first'] + curve['step'] * n * (n - 1) // 2


def level_for_xp(xp, cfg=None):
    cfg = cfg or get_config()
    xp = max(0, int(xp or 0))
    lo, hi = 1, cfg['curve']['max_level']
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if xp_to_reach(mid, cfg) <= xp:
            lo = mid
        else:
            hi = mid - 1
    return lo


def tier_for(level):
    """Color del nivel: cambia cada 10 niveles (como Steam). Devuelve (nombre, color 1, color 2)."""
    level = max(1, int(level or 1))
    chosen = TIERS[0]
    for tier in TIERS:
        if level >= tier[0]:
            chosen = tier
    return chosen[1], chosen[2], chosen[3]


def badge_for(level):
    """Lo mínimo para dibujar el número junto al nombre: nivel y colores."""
    level = max(1, int(level or 1))
    name, c1, c2 = tier_for(level)
    return {'level': level, 'tier': name, 'c1': c1, 'c2': c2}


def level_progress(xp, cfg=None):
    """Todo lo que necesita la pantalla: nivel, color y la barra hacia el siguiente nivel."""
    cfg = cfg or get_config()
    xp = max(0, int(xp or 0))
    level = level_for_xp(xp, cfg)
    info = dict(badge_for(level), xp=xp)
    base = xp_to_reach(level, cfg)
    is_max = level >= cfg['curve']['max_level']
    info.update({'is_max': is_max, 'next_level': None if is_max else level + 1, 'into': xp - base, 'needed': 0, 'remaining': 0, 'percent': 100})
    if not is_max:
        target = xp_to_reach(level + 1, cfg)
        span = max(1, target - base)
        info.update({'needed': span, 'remaining': target - xp, 'percent': max(0, min(100, int((xp - base) * 100 / span)))})
    return info


def preview_levels(cfg=None):
    """Tabla para el administrador: puntos totales y puntos de ese salto, para unos cuantos niveles."""
    cfg = cfg or get_config()
    top = cfg['curve']['max_level']
    wanted = {2, 3, 4, 5, 10, 20, 30, 50, 75, 100, 150, 200, 300, 500}
    rows = []
    for level in sorted({lv for lv in wanted if 2 <= lv <= top} | {top}):
        total = xp_to_reach(level, cfg)
        rows.append({'level': level, 'total': total, 'jump': total - xp_to_reach(level - 1, cfg), 'tier': tier_for(level)[0]})
    return rows


# ───────────────────────── Hechos que dan puntos ─────────────────────────
def _ve_date(value):
    local = to_ve(value)
    return local.date() if local else None


def _day_from_key(key):
    try:
        return date.fromisoformat(str(key)[:10])
    except ValueError:
        return None


def collect_events(user_id, order_dates=None):
    """{qué: {día: cuántas veces}} de TODA la historia de la cuenta (hora de Venezuela): sus compras, me gusta y seguidores."""
    events = defaultdict(lambda: defaultdict(int))
    for day in (order_dates or []):
        events['order'][day] += 1
    for column, key, kind in (
        (ProfileLike.to_user_id, 'like_received', ProfileLike), (ProfileLike.from_user_id, 'like_given', ProfileLike),
        (ProfileFollow.following_id, 'follower_received', ProfileFollow), (ProfileFollow.follower_id, 'follow_given', ProfileFollow),
    ):
        for (created,) in db.session.query(kind.created_at).filter(column == user_id).all():
            events[key][_ve_date(created)] += 1
    for metric in list(events):
        events[metric].pop(None, None)
    return events


def _capped_total(per_day, cap):
    if not cap:
        return sum(per_day.values())
    return sum(min(count, cap) for count in per_day.values())


def week_start(day):
    return day - timedelta(days=day.weekday())


def _period_start(period, day):
    return day if period == 'day' else week_start(day)


def mission_completions(events, cfg):
    """{clave de misión: cuántos periodos (días o semanas) cumplió desde que arrancó el sistema}."""
    start = mission_start(cfg)
    result = {}
    for key, period, metric, _label, _t, _x in MISSION_DEFS:
        setting = cfg['missions'][key]
        if not setting['enabled'] or setting['xp'] <= 0:
            result[key] = 0
            continue
        buckets = defaultdict(int)
        for day, count in events.get(metric, {}).items():
            if day >= start:
                buckets[_period_start(period, day)] += count
        result[key] = sum(1 for total in buckets.values() if total >= setting['target'])
    return result


def compute_xp(user_id, orders, units, order_dates, streak_months, badge_count, top_months, profile_complete, cfg=None, bonus_xp=0):
    """Puntos totales de una cuenta y de dónde salen. Todo sale de hechos, así que es seguro recalcular cuantas veces se quiera.
    `bonus_xp`: puntos de nivel que la cuenta compró en la tienda de puntos (canjes que siguen "done"; un canje devuelto ya no cuenta)."""
    cfg = cfg or get_config()
    points, caps = cfg['points'], cfg['caps']
    events = collect_events(user_id, order_dates)
    parts = []

    def add(key, label, count, value):
        parts.append({'key': key, 'label': label, 'count': int(count), 'xp': int(value)})

    add('order', 'Recargas', orders, orders * points['order'])
    add('per_100_units', 'Diamantes recargados', units // 100, (units // 100) * points['per_100_units'])
    for key in CAPPED_EVENTS:
        count = _capped_total(events.get(key, {}), caps.get(key, 0))
        add(key, POINT_TEXT[key], count, count * points[key])
    add('profile_complete', 'Perfil completo', 1 if profile_complete else 0, points['profile_complete'] if profile_complete else 0)
    add('badge', 'Insignias', badge_count, badge_count * points['badge'])
    add('streak_month', 'Meses de racha', streak_months, streak_months * points['streak_month'])
    add('top3', 'Top 3 del mes', top_months, top_months * points['top3'])
    done = mission_completions(events, cfg)
    add('missions', 'Misiones completadas', sum(done.values()), sum(done[key] * cfg['missions'][key]['xp'] for key in MISSION_KEYS))
    if bonus_xp > 0:   # solo se lista cuando existe, así las cuentas que no compraron nada no ven una fila de ceros
        add('shop', 'Comprados en la tienda de puntos', 1, bonus_xp)
    return {'total': sum(part['xp'] for part in parts), 'parts': parts, 'events': events}


# ───────────────────────── Misiones de hoy y de la semana (lo que ve el dueño) ─────────────────────────
def mission_overview(events, cfg=None):
    """Las misiones activas con su avance AHORA (hoy / esta semana): si ya están hechas y cuánto falta para que se reinicien."""
    cfg = cfg or get_config()
    today = today_ve()
    wstart = week_start(today)
    start = mission_start(cfg)
    now_local = now_ve().replace(tzinfo=None)
    tomorrow = datetime.combine(today + timedelta(days=1), datetime.min.time())
    next_week = datetime.combine(wstart + timedelta(days=7), datetime.min.time())
    rows = []
    for key, period, metric, label, _t, _x in MISSION_DEFS:
        setting = cfg['missions'][key]
        if not setting['enabled'] or setting['xp'] <= 0:
            continue
        per_day = events.get(metric, {})
        if period == 'day':
            current = per_day.get(today, 0) if today >= start else 0
            resets = int((tomorrow - now_local).total_seconds())
        else:
            current = sum(count for day, count in per_day.items() if max(wstart, start) <= day <= wstart + timedelta(days=6))
            resets = int((next_week - now_local).total_seconds())
        target = setting['target']
        shown = min(current, target)
        rows.append({
            'key': key, 'period': period, 'label': mission_text(label, target), 'target': target, 'xp': setting['xp'],
            'current': shown, 'done': current >= target, 'percent': max(0, min(100, int(shown * 100 / target))),
            'resets_in': max(0, resets), 'resets_label': format_remaining(resets),
        })
    return rows


def format_remaining(seconds):
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f'{days} d {hours} h'
    if hours:
        return f'{hours} h {minutes} min'
    return f'{max(1, minutes)} min'


def level_up_notice(user_id, old_level, new_level):
    """Aviso de una sola vez en Mi cuenta cuando sube de nivel. Si todavía tiene otro aviso de nivel sin leer se reemplaza (no se acumulan:
    cuando el administrador cambia los puntos y se recalcula todo, cada cuenta recibe como mucho UN aviso, el del nivel más reciente)."""
    if new_level > old_level >= 1:
        AccountNotice.query.filter(
            AccountNotice.user_id == user_id, AccountNotice.seen_at.is_(None), AccountNotice.message.like('¡Subiste al nivel %'),
        ).delete(synchronize_session=False)
        db.session.add(AccountNotice(user_id=user_id, message=f'¡Subiste al nivel {new_level}!'))
