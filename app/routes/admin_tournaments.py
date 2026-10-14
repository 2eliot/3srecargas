"""Torneos en el admin: crear torneos y salas, definir premios, ver a los inscritos de cada sala y entregar el premio a los ganadores.

Va en su propio módulo (como `admin_support.py`): `admin.py` ya es enorme y el `before_request` de `admin_bp` solo protege a su blueprint.
Cuelga de `/admin/torneos`. La lógica (inscripción por orden de llegada, desbloqueo de salas, entrega) vive en `utils/tournaments.py`.
"""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..models import Game, Package, Tournament, TournamentEntry, TournamentPrize, TournamentRoom, db
from ..utils import tournaments as T

admin_tournaments_bp = Blueprint('admin_tournaments_bp', __name__, url_prefix='/admin/torneos')


@admin_tournaments_bp.before_request
def guard():
    if not current_user.is_authenticated:
        return None  # lo resuelve @login_required + el unauthorized_handler
    if current_user.__class__.__name__ != 'AdminUser':
        flash('Esta sección es solo para administradores.', 'warning')
        return redirect(url_for('main_bp.index'))
    return None


def _tournament_or_404(tournament_id):
    tournament = db.session.get(Tournament, tournament_id)
    if tournament is None:
        abort(404)
    return tournament


def _back(tournament_id, anchor=''):
    return redirect(url_for('admin_tournaments_bp.detail', tournament_id=tournament_id) + (f'#{anchor}' if anchor else ''))


def _run(action, tournament_id, anchor='', success=None):
    """Corre una acción de la lógica: si falla con un mensaje claro, lo muestra en rojo; si sale bien, muestra `success`. Devuelve la redirección."""
    try:
        action()
    except T.TournamentError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
        return _back(tournament_id, anchor)
    if success:
        flash(success, 'success')
    return _back(tournament_id, anchor)


# ───────────────────────── Lista ─────────────────────────
@admin_tournaments_bp.route('/')
@login_required
def index():
    from ..routes.verify import verifiable_game_ids

    rows = []
    for tournament in Tournament.query.order_by(Tournament.id.desc()).all():
        by_room = T.active_counts(tournament.id)
        state_key, state_text = T.display_status(tournament)
        rows.append({'t': tournament, 'rooms': len(tournament.rooms), 'joined': sum(by_room.values()), 'capacity': sum(r.max_players for r in tournament.rooms),
                     'state_key': state_key, 'state_text': state_text, 'opens_label': T.opens_label(tournament)})
    games = Game.query.filter_by(is_active=True).order_by(Game.name.asc()).all()
    return render_template(
        'admin/tournaments.html', rows=rows, games=games, verifiable_ids=verifiable_game_ids(), status_labels=T.STATUS_LABELS,
        history=T.winners_history(limit=60),
    )


@admin_tournaments_bp.route('/crear', methods=['POST'])
@login_required
def create():
    try:
        tournament = T.create_tournament(request.form.get('title'), request.form.get('game_id'), request.form.get('description'), request.form.get('max_per_ip'),
                                        request.form.get('opens_at'))
    except T.TournamentError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin_tournaments_bp.index'))
    flash('Torneo creado. Ahora agrega sus salas y premios; cuando sea la hora, abre las inscripciones.', 'success')
    return _back(tournament.id)


# ───────────────────────── Detalle de un torneo ─────────────────────────
@admin_tournaments_bp.route('/<int:tournament_id>')
@login_required
def detail(tournament_id):
    from ..routes.verify import verifiable_game_ids

    tournament = _tournament_or_404(tournament_id)
    overview = T.rooms_overview(tournament)
    rooms = []
    for item in overview:
        room = item['room']
        rooms.append({**item, 'roster': T.roster(room), 'winners': T.winners_of_room(room), 'roster_text': T.roster_text(room)})
    packages = Package.query.filter_by(game_id=tournament.game_id, is_active=True).order_by(Package.sort_order.asc(), Package.name.asc()).all()
    used_places = {p.place for p in tournament.prizes}
    next_place = next((n for n in range(1, 101) if n not in used_places), 1)
    removed = TournamentEntry.query.filter_by(tournament_id=tournament.id, status='removed').count()
    return render_template(
        'admin/tournament_detail.html', t=tournament, rooms=rooms, packages=packages, next_place=next_place, removed=removed,
        verifiable=tournament.game_id in verifiable_game_ids(), status_labels=T.STATUS_LABELS,
        public_url=url_for('promos_bp.torneo_page', tournament_id=tournament.id, _external=True),
        history=T.winners_history(tournament.id, limit=300), total=sum(r['count'] for r in rooms), capacity=sum(r['max'] for r in rooms),
        state_key=T.display_status(tournament)[0], state_text=T.display_status(tournament)[1], opens_input=T.opens_at_input_value(tournament),
        opens_label=T.opens_label(tournament), scheduled=T.is_scheduled(tournament), opens_future=T.seconds_until_open(tournament) is not None,
        readiness=T.readiness(tournament),
    )


@admin_tournaments_bp.route('/<int:tournament_id>/editar', methods=['POST'])
@login_required
def edit(tournament_id):
    tournament = _tournament_or_404(tournament_id)
    return _run(lambda: T.update_tournament(tournament, request.form.get('title'), request.form.get('description'), request.form.get('max_per_ip'),
                                           request.form.get('opens_at', '')),
                tournament_id, success='Torneo actualizado.')


@admin_tournaments_bp.route('/<int:tournament_id>/estado', methods=['POST'])
@login_required
def set_status(tournament_id):
    tournament = _tournament_or_404(tournament_id)
    action = request.form.get('action') or ''
    messages = {
        'open': 'Publicado: el link ya funciona para tus clientes (si tiene hora de apertura, verán la cuenta regresiva y se abre solo a esa hora).',
        'open_now': 'Inscripciones ABIERTAS ya: el link funciona para tus clientes.', 'close': 'Inscripciones cerradas.',
        'finish': 'Torneo terminado: ya no se muestra a los clientes (queda en tu historial).', 'draft': 'El torneo volvió a "Preparando".',
    }
    return _run(lambda: T.change_status(tournament, action), tournament_id, success=messages.get(action))


@admin_tournaments_bp.route('/<int:tournament_id>/borrar', methods=['POST'])
@login_required
def delete(tournament_id):
    tournament = _tournament_or_404(tournament_id)
    try:
        T.delete_tournament(tournament)
    except T.TournamentError as exc:
        flash(str(exc), 'danger')
        return _back(tournament_id)
    flash('Torneo borrado.', 'success')
    return redirect(url_for('admin_tournaments_bp.index'))


# ───────────────────────── Premios ─────────────────────────
@admin_tournaments_bp.route('/<int:tournament_id>/premios/agregar', methods=['POST'])
@login_required
def prize_add(tournament_id):
    tournament = _tournament_or_404(tournament_id)
    return _run(lambda: T.add_prize(tournament, request.form.get('place'), request.form.get('package_id')), tournament_id, 'premios', 'Premio agregado.')


@admin_tournaments_bp.route('/<int:tournament_id>/premios/<int:prize_id>/borrar', methods=['POST'])
@login_required
def prize_delete(tournament_id, prize_id):
    tournament = _tournament_or_404(tournament_id)
    prize = TournamentPrize.query.filter_by(id=prize_id, tournament_id=tournament.id).first_or_404()
    return _run(lambda: T.delete_prize(prize), tournament_id, 'premios', 'Premio quitado.')


# ───────────────────────── Salas ─────────────────────────
def _room_or_404(tournament, room_id):
    return TournamentRoom.query.filter_by(id=room_id, tournament_id=tournament.id).first_or_404()


@admin_tournaments_bp.route('/<int:tournament_id>/salas/agregar', methods=['POST'])
@login_required
def room_add(tournament_id):
    tournament = _tournament_or_404(tournament_id)
    return _run(lambda: T.add_room(tournament, request.form.get('name'), request.form.get('room_code'), request.form.get('room_password'), request.form.get('max_players')),
                tournament_id, 'salas', 'Sala creada.')


@admin_tournaments_bp.route('/<int:tournament_id>/salas/<int:room_id>/editar', methods=['POST'])
@login_required
def room_edit(tournament_id, room_id):
    tournament = _tournament_or_404(tournament_id)
    room = _room_or_404(tournament, room_id)
    return _run(lambda: T.update_room(room, request.form.get('name'), request.form.get('room_code'), request.form.get('room_password'), request.form.get('max_players')),
                tournament_id, f'sala-{room_id}', 'Sala actualizada.')


@admin_tournaments_bp.route('/<int:tournament_id>/salas/<int:room_id>/borrar', methods=['POST'])
@login_required
def room_delete(tournament_id, room_id):
    tournament = _tournament_or_404(tournament_id)
    room = _room_or_404(tournament, room_id)
    return _run(lambda: T.delete_room(room), tournament_id, 'salas', 'Sala borrada.')


@admin_tournaments_bp.route('/<int:tournament_id>/salas/<int:room_id>/mover', methods=['POST'])
@login_required
def room_move(tournament_id, room_id):
    tournament = _tournament_or_404(tournament_id)
    room = _room_or_404(tournament, room_id)
    T.move_room(room, 'up' if request.form.get('direction') == 'up' else 'down')
    return _back(tournament_id, 'salas')


@admin_tournaments_bp.route('/<int:tournament_id>/salas/<int:room_id>/terminar', methods=['POST'])
@login_required
def room_finish(tournament_id, room_id):
    tournament = _tournament_or_404(tournament_id)
    room = _room_or_404(tournament, room_id)
    return _run(lambda: T.finish_room(room), tournament_id, f'sala-{room_id}',
                f'{room.name} terminada: sus jugadores ya pueden inscribirse en otra sala.')


@admin_tournaments_bp.route('/<int:tournament_id>/salas/<int:room_id>/reabrir', methods=['POST'])
@login_required
def room_reopen(tournament_id, room_id):
    tournament = _tournament_or_404(tournament_id)
    room = _room_or_404(tournament, room_id)
    return _run(lambda: T.reopen_room(room), tournament_id, f'sala-{room_id}', f'{room.name} reabierta.')


# ───────────────────────── Inscritos y ganadores ─────────────────────────
@admin_tournaments_bp.route('/<int:tournament_id>/inscritos/<int:entry_id>/quitar', methods=['POST'])
@login_required
def entry_remove(tournament_id, entry_id):
    tournament = _tournament_or_404(tournament_id)
    entry = TournamentEntry.query.filter_by(id=entry_id, tournament_id=tournament.id).first_or_404()
    return _run(lambda: T.remove_entry(entry), tournament_id, f'sala-{entry.room_id}', 'Lo sacaste de la lista de la sala.')


@admin_tournaments_bp.route('/<int:tournament_id>/salas/<int:room_id>/ganador', methods=['POST'])
@login_required
def winner_deliver(tournament_id, room_id):
    tournament = _tournament_or_404(tournament_id)
    room = _room_or_404(tournament, room_id)
    try:
        place = int(request.form.get('place') or 0)
    except ValueError:
        place = 0
    try:
        winner, message = T.deliver_prize(tournament, room, place, request.form.get('entry_id'))
    except T.TournamentError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
        return _back(tournament_id, f'sala-{room_id}')
    order = winner.prize_order
    suffix = f' (orden #{order.order_number}, estado: {order.status})' if order else ''
    flash(f'Premio del puesto {place} entregado a {winner.player_nick or winner.player_id}{suffix}. {message}', 'success')
    return _back(tournament_id, f'sala-{room_id}')
