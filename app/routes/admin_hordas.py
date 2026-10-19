"""Hordas de Diamantes en el admin (Dinámicas → Hordas): el dueño configura el juego, ve el ranking, bloquea tramposos, revisa repeticiones y aprueba premios.

Va en su propio módulo (como `admin_points_shop.py` y `admin_tournaments.py`): `admin.py` ya es enorme y el `before_request` de `admin_bp` solo protege a su
blueprint. Cuelga de `/admin/hordas`. La lógica vive en `utils/hordas.py`. Los personajes se guardan con la ruta `admin_bp.promos_horde_image_save` (de siempre).

También muestra, mientras queden, los ganadores PENDIENTES de la última semana jugada por ID y por juego (12-18/10/2026): esos se aprueban con las rutas
viejas de `admin.py` (premio = paquete a un ID).
"""
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ..models import Game, HordeAccountBan, HordeAccountReplay, HordeAccountRun, HordeAccountWinner, PromoHordeWinner, User, db
from ..utils import account_points, hordas
from ..utils.auth_accounts import ACCOUNT_KINDS
from ..utils.promos import _horde_week_key, get_horde_characters

admin_hordas_bp = Blueprint('admin_hordas_bp', __name__, url_prefix='/admin/hordas')


@admin_hordas_bp.before_request
def guard():
    if not current_user.is_authenticated:
        return None  # lo resuelve @login_required + el unauthorized_handler
    if current_user.__class__.__name__ != 'AdminUser':
        flash('Esta sección es solo para administradores.', 'warning')
        return redirect(url_for('main_bp.index'))
    return None


def _back(default_endpoint='admin_hordas_bp.index', **values):
    """Vuelve a la página desde donde se hizo la acción (solo rutas del admin)."""
    back = request.form.get('back') or ''
    if back.startswith('/admin/') and not back.startswith('//'):
        return redirect(back)
    return redirect(url_for(default_endpoint, **values))


def _account_by_username(raw):
    name = str(raw or '').strip().lstrip('@')
    if not name:
        return None
    return User.query.filter(func.lower(User.username) == name.lower(), User.account_kind.in_(ACCOUNT_KINDS)).first()


@admin_hordas_bp.route('/')
@login_required
def index():
    settings = hordas.get_settings()
    week_key = _horde_week_key()
    ranking = hordas.week_ranking(week_key)
    bans = HordeAccountBan.query.order_by(HordeAccountBan.created_at.desc()).all()
    recent = HordeAccountWinner.query.order_by(HordeAccountWinner.week_key.desc(), HordeAccountWinner.place.asc()).limit(15).all()
    who = hordas.identities([uid for uid, _t in ranking] + [b.user_id for b in bans] + [w.user_id for w in recent])
    legacy_pending = PromoHordeWinner.query.filter_by(status='pending').order_by(PromoHordeWinner.week_key.desc(), PromoHordeWinner.place.asc()).all()
    return render_template(
        'admin/hordas.html',
        settings=settings, prize_count=len(settings['prizes']) or 3, max_places=hordas.MAX_PRIZE_PLACES, max_points=account_points.MAX_POINTS_PER_MOVE,
        week_key=week_key, ranking=ranking, who=who, bans=bans, recent_winners=recent,
        pending_count=HordeAccountWinner.query.filter_by(status='pending').count(),
        characters=get_horde_characters(),
        legacy_pending=legacy_pending, games={g.id: g.name for g in Game.query.all()},
    )


@admin_hordas_bp.route('/guardar', methods=['POST'])
@login_required
def save():
    error = hordas.save_settings(request.form)
    if error:
        db.session.rollback()
        flash(error, 'danger')
    else:
        db.session.commit()
        flash('Hordas de Diamantes guardado.', 'success')
    return redirect(url_for('admin_hordas_bp.index'))


@admin_hordas_bp.route('/bloquear', methods=['POST'])
@login_required
def ban():
    raw_id = (request.form.get('user_id') or '').strip()
    user = db.session.get(User, int(raw_id)) if raw_id.isdigit() else _account_by_username(request.form.get('username'))
    reason = (request.form.get('reason') or '').strip()[:200] or 'Trampa'
    if user is None:
        flash('No encontré esa cuenta. Escribe su nombre de usuario (con o sin @).', 'danger')
        return _back()
    if not hordas.ban_user(user.id, reason):
        flash(f'@{user.username} ya estaba bloqueada en Hordas.', 'info')
        return _back()
    try:
        hordas.refresh_pending_winners()   # si esa cuenta era ganadora pendiente, sale y suben los siguientes
    except Exception:
        db.session.rollback()
    flash(f'@{user.username} bloqueada en Hordas: ya no puede jugar ni sale en el ranking.', 'success')
    return _back()


@admin_hordas_bp.route('/desbloquear/<int:ban_id>', methods=['POST'])
@login_required
def unban(ban_id):
    row = db.session.get(HordeAccountBan, ban_id)
    if row is None:
        abort(404)
    name = row.user.username if row.user else f'#{row.user_id}'
    db.session.delete(row)
    db.session.commit()
    flash(f'@{name} desbloqueada: puede volver a jugar y vuelve a salir en el ranking.', 'success')
    return _back()


# ───────────────────────── Premios y repeticiones ─────────────────────────
def _winner_unverified_summary(winner):
    """Texto con lo que NO se ha comprobado de las partidas de este ganador en la semana ('' si todo está en orden). Las partidas se aceptan por lo que
    dice el teléfono (con topes); lo que las respalda es la repetición que el admin reproduce."""
    runs = (
        HordeAccountRun.query
        .filter(HordeAccountRun.week_key == winner.week_key, HordeAccountRun.user_id == winner.user_id,
                HordeAccountRun.finished_at.isnot(None), HordeAccountRun.diamonds > 0)
        .all()
    )
    if not runs:
        return ''
    with_replay = {run_id for (run_id,) in db.session.query(HordeAccountReplay.run_id).filter(HordeAccountReplay.run_id.in_([r.id for r in runs]))}
    bad = sum(1 for r in runs if r.flag or r.replay_check == 'no_coincide')
    no_replay = sum(1 for r in runs if r.id not in with_replay)
    unchecked = sum(1 for r in runs if r.id in with_replay and r.replay_check != 'ok')
    parts = []
    if bad:
        parts.append(f'{bad} partida(s) marcadas por el servidor o que no coinciden con su repetición')
    if no_replay:
        parts.append(f'{no_replay} partida(s) sin repetición guardada')
    if unchecked:
        parts.append(f'{unchecked} repetición(es) sin comprobar')
    return '; '.join(parts)


@admin_hordas_bp.route('/repeticiones')
@login_required
def replays():
    """Top 5 de una semana con todas sus partidas y sus repeticiones, y los premios pendientes de aprobar."""
    current_week = _horde_week_key()
    weeks = sorted({w for (w,) in db.session.query(HordeAccountRun.week_key).distinct()} | {current_week}, reverse=True)
    # Por defecto, la semana con premios por aprobar; si no hay, la última cerrada.
    pending = HordeAccountWinner.query.filter_by(status='pending').order_by(HordeAccountWinner.week_key.desc()).first()
    default_week = pending.week_key if pending else next((w for w in weeks if w < current_week), current_week)
    week = request.args.get('week') or default_week

    ranking = hordas.week_ranking(week, limit=hordas.ADMIN_REPLAY_TOP)
    winners = HordeAccountWinner.query.filter_by(week_key=week).order_by(HordeAccountWinner.place.asc()).all()
    who = hordas.identities([uid for uid, _t in ranking] + [w.user_id for w in winners])
    players = []
    for place, (uid, total) in enumerate(ranking, start=1):
        runs = (
            HordeAccountRun.query
            .filter(HordeAccountRun.week_key == week, HordeAccountRun.user_id == uid, HordeAccountRun.finished_at.isnot(None), HordeAccountRun.diamonds > 0)
            .order_by(HordeAccountRun.diamonds.desc()).all()
        )
        with_replay = {run_id for (run_id,) in db.session.query(HordeAccountReplay.run_id).filter(HordeAccountReplay.run_id.in_([r.id for r in runs] or [0]))}
        players.append({
            'place': place, 'user_id': uid, 'who': who.get(uid, {}), 'total': total,
            'runs': [{'run': r, 'has_replay': r.id in with_replay, 'seconds': int((r.finished_at - r.started_at).total_seconds())} for r in runs],
            'bad': sum(1 for r in runs if r.replay_check == 'no_coincide' or r.flag),
            'unchecked': sum(1 for r in runs if r.id in with_replay and not r.replay_check),
        })
    return render_template(
        'admin/hordas_replays.html', week=week, weeks=weeks, current_week=current_week, players=players, week_winners=winners,
        who=who, player_info={pl['user_id']: pl for pl in players}, prizes_set=any(p > 0 for p in hordas.get_settings()['prizes']),
        first_week=hordas.ACCOUNT_MODE_FIRST_WEEK,
    )


@admin_hordas_bp.route('/semana/rearmar', methods=['POST'])
@login_required
def rebuild_week():
    week = (request.form.get('week') or '').strip()
    if not week or week >= _horde_week_key() or week < hordas.ACCOUNT_MODE_FIRST_WEEK:
        flash('Solo se pueden armar los ganadores de una semana ya cerrada.', 'warning')
        return _back()
    try:
        hordas.rebuild_pending_winners(week)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return _back()
    flash(f'Ganadores pendientes de la semana {week} armados de nuevo con los premios actuales (los ya aprobados no se tocan).', 'success')
    return _back()


@admin_hordas_bp.route('/ganador/<int:winner_id>/aprobar', methods=['POST'])
@login_required
def approve(winner_id):
    winner = db.session.get(HordeAccountWinner, winner_id)
    if winner is None:
        abort(404)
    name = (hordas.identities([winner.user_id]).get(winner.user_id) or {}).get('handle') or f'#{winner.user_id}'
    # Aviso de dos pasos: si hay partidas sin respaldo, la primera pulsación solo avisa y la segunda aprueba.
    unverified = _winner_unverified_summary(winner)
    warn_key = f'hordas_approve_warned:{winner_id}'
    if unverified and not session.get(warn_key):
        session[warn_key] = True
        flash(f'Antes de aprobar a @{name}: {unverified}. Revisa sus repeticiones en "Premios y repeticiones". Si aun así quieres aprobarla, pulsa Aprobar otra vez.', 'warning')
        return _back()
    session.pop(warn_key, None)
    try:
        winner = hordas.approve_winner(winner_id)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return _back()
    except Exception as exc:
        flash(f'No se pudieron dar los puntos: {exc}. Sigue pendiente, intenta de nuevo.', 'danger')
        return _back()
    flash(f'Premio aprobado: @{name} (puesto #{winner.place}) recibió {winner.points} puntos en su cuenta.', 'success')
    return _back()


@admin_hordas_bp.route('/ganador/<int:winner_id>/rechazar', methods=['POST'])
@login_required
def reject(winner_id):
    winner = db.session.get(HordeAccountWinner, winner_id)
    if winner is None:
        abort(404)
    name = (hordas.identities([winner.user_id]).get(winner.user_id) or {}).get('handle') or f'#{winner.user_id}'
    try:
        hordas.reject_winner(winner_id, (request.form.get('reason') or '').strip())
    except ValueError as exc:
        flash(str(exc), 'danger')
        return _back()
    flash(f'Premio rechazado: @{name} quedó bloqueada en Hordas y los siguientes del ranking subieron de puesto.', 'success')
    return _back()


# ───────────────────────── Repeticiones (ver y comprobar una partida) ─────────────────────────
def _run_and_replay(run_id):
    run = db.session.get(HordeAccountRun, run_id)
    if run is None:
        abort(404)
    replay = HordeAccountReplay.query.filter_by(run_id=run.id).first()
    if not replay or run.seed is None:
        abort(404)
    return run, replay


@admin_hordas_bp.route('/repeticion/<int:run_id>')
@login_required
def replay_view(run_id):
    """Abre el juego en modo repetición (o comprobación rápida con ?check=1)."""
    run, replay = _run_and_replay(run_id)
    characters = [
        {'slot': c['slot'], 'name': c['name'], 'mode': c['mode'],
         'url': url_for('static', filename='uploads/' + c['image']),
         'cover_url': url_for('static', filename='uploads/' + (c['cover'] or c['image']))}
        for c in get_horde_characters() if c['image']
    ]
    seconds = int((run.finished_at - run.started_at).total_seconds()) if run.finished_at else 0
    handle = (hordas.identities([run.user_id]).get(run.user_id) or {}).get('handle') or f'#{run.user_id}'
    replay_data = {
        'run_id': run.id,
        'check': request.args.get('check') == '1',
        'seed': run.seed,
        'W': replay.width, 'H': replay.height, 'ch': replay.character_slot or 0,
        'rules': replay.rules or 1,
        'data': replay.data,
        'reported': {'wave': run.wave or 0, 'claimed': run.claimed_diamonds or 0},
        'verdict_url': url_for('admin_hordas_bp.replay_verdict', run_id=run.id),
        'player_label': f'@{handle} · {seconds // 60}:{seconds % 60:02d}',
    }
    return render_template('promos/hordas.html', enabled=True, login_next='', characters=characters, replay=replay_data)


@admin_hordas_bp.route('/repeticion/<int:run_id>/veredicto', methods=['POST'])
@login_required
def replay_verdict(run_id):
    run, _replay = _run_and_replay(run_id)
    data = request.get_json(silent=True) or {}
    check = data.get('check')
    if check not in ('ok', 'dudoso', 'no_coincide'):
        return jsonify({'ok': False}), 400
    try:
        run.replay_wave = int(data.get('wave') or 0)
        run.replay_diamonds = int(data.get('diamonds') or 0)
    except (TypeError, ValueError):
        return jsonify({'ok': False}), 400
    run.replay_check = check
    db.session.commit()
    return jsonify({'ok': True})
