"""Bandeja de soporte del admin.

Va en su propio módulo y no dentro de `admin.py`, que ya pasa de las 3800
líneas. Cuelga de `/admin/soporte` con su propio guardián de acceso: el
`before_request` de `admin_bp` solo protege a su blueprint, no a este.
"""

from datetime import datetime

from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request, url_for
)
from flask_login import current_user, login_required
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from ..models import db, Order, SupportChat, SupportChatTag, SupportMessage, SupportTag
from ..utils import support as support_service
from ..utils.support import SupportError
from ..utils.notifications import notify_support_admin_reply
from ..utils.timezone import format_ve

admin_support_bp = Blueprint('admin_support_bp', __name__, url_prefix='/admin/soporte')

PAGE_SIZE = 25


@admin_support_bp.before_request
def guard():
    if not current_user.is_authenticated:
        return None  # lo resuelve @login_required + el unauthorized_handler
    if current_user.__class__.__name__ != 'AdminUser':
        flash('Esta sección es solo para administradores.', 'warning')
        return redirect(url_for('main_bp.index'))
    return None


def _admin_id():
    return current_user.id if current_user.is_authenticated else None


def _slugify(value):
    text = ''.join(c.lower() if c.isalnum() else '-' for c in str(value or '').strip())
    while '--' in text:
        text = text.replace('--', '-')
    return text.strip('-')[:60] or 'etiqueta'


# ─── Bandeja ─────────────────────────────────────────────────────────────────

@admin_support_bp.route('/')
@login_required
def inbox():
    status = (request.args.get('status') or 'active').strip()
    tag_slug = (request.args.get('tag') or '').strip()
    term = (request.args.get('q') or '').strip()
    page = request.args.get('page', type=int) or 1

    query = SupportChat.query.options(
        joinedload(SupportChat.tag_links).joinedload(SupportChatTag.tag)
    )

    if status == 'active':
        query = query.filter(SupportChat.status.in_(support_service.OPEN_STATUSES))
    elif status == 'unread':
        query = query.filter(SupportChat.unread_admin > 0)
    elif status in ('open', 'waiting_client', 'closed'):
        query = query.filter(SupportChat.status == status)

    if tag_slug:
        tag = support_service.get_tag(tag_slug)
        if tag:
            query = query.filter(SupportChat.tag_links.any(SupportChatTag.tag_id == tag.id))

    if term:
        like = f'%{term}%'
        query = query.filter(or_(
            SupportChat.client_name.ilike(like),
            SupportChat.short_code.ilike(like),
            SupportChat.context_order_number.ilike(like),
            SupportChat.context_email.ilike(like),
            SupportChat.context_player_id.ilike(like),
            SupportChat.admin_note.ilike(like),
        ))

    pagination = (query
                  .order_by(SupportChat.unread_admin.desc(),
                            SupportChat.last_message_at.desc())
                  .paginate(page=page, per_page=PAGE_SIZE, error_out=False))

    return render_template(
        'admin/support.html',
        chats=pagination.items,
        pagination=pagination,
        status=status,
        tag_slug=tag_slug,
        term=term,
        tags=SupportTag.query.filter_by(is_active=True)
                      .order_by(SupportTag.kind, SupportTag.sort_order).all(),
        counts={
            'active': SupportChat.query.filter(
                SupportChat.status.in_(support_service.OPEN_STATUSES)).count(),
            'unread': support_service.pending_chats_count(),
            'closed': SupportChat.query.filter_by(status='closed').count(),
        },
    )


# ─── Hilo ────────────────────────────────────────────────────────────────────

@admin_support_bp.route('/<int:chat_id>')
@login_required
def detail(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    support_service.mark_read_by_admin(chat)

    return render_template(
        'admin/support_detail.html',
        chat=chat,
        messages=[support_service.serialize_message(m) for m in chat.messages],
        suggested_orders=support_service.find_matching_orders(chat),
        recent_orders=(Order.query
                       .filter(Order.player_id == chat.order.player_id)
                       .order_by(Order.created_at.desc())
                       .limit(5).all()) if chat.order and chat.order.player_id else [],
        user_tags=SupportTag.query.filter_by(kind='user', is_active=True)
                            .order_by(SupportTag.sort_order).all(),
        error_tags=SupportTag.query.filter_by(kind='error', is_active=True)
                             .order_by(SupportTag.sort_order).all(),
        applied_tag_ids={t.id for t in chat.tags},
    )


@admin_support_bp.route('/<int:chat_id>/hilo.json')
@login_required
def thread_json(chat_id):
    """Sondeo de la vista del hilo. Solo mensajes nuevos."""
    chat = SupportChat.query.get_or_404(chat_id)
    after_id = request.args.get('after_id', type=int) or 0

    query = SupportMessage.query.filter(SupportMessage.chat_id == chat.id)
    if after_id:
        query = query.filter(SupportMessage.id > after_id)
    messages = query.order_by(SupportMessage.id).all()

    if messages:
        support_service.mark_read_by_admin(chat)

    return jsonify({
        'ok': True,
        'status': chat.status,
        'status_label': chat.status_label,
        'messages': [support_service.serialize_message(m) for m in messages],
    })


@admin_support_bp.route('/pendientes.json')
@login_required
def pending_json():
    """Badge del menú lateral, y de paso el barrido de chats inactivos.

    Aprovecha este sondeo en vez de un cron: si nadie está mirando el
    panel tampoco urge cerrar hilos, y cuando abres la bandeja ya llega
    barrida.
    """
    support_service.close_idle_chats()
    return jsonify({'ok': True, 'pending': support_service.pending_chats_count()})


@admin_support_bp.route('/<int:chat_id>/responder', methods=['POST'])
@login_required
def reply(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    data = request.get_json(silent=True) or request.form

    try:
        attachment = support_service.save_attachment(request.files.get('file'))
        message = support_service.add_admin_message(
            chat, data.get('body'), admin_id=_admin_id(), attachment=attachment
        )
    except SupportError as exc:
        return jsonify({'ok': False, 'error': exc.message}), exc.status

    notify_support_admin_reply(chat, message)

    return jsonify({
        'ok': True,
        'status': chat.status,
        'status_label': chat.status_label,
        'message': support_service.serialize_message(message),
    })


# ─── Etiquetas aplicadas ─────────────────────────────────────────────────────

@admin_support_bp.route('/<int:chat_id>/etiqueta', methods=['POST'])
@login_required
def toggle_tag(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    data = request.get_json(silent=True) or request.form

    try:
        tag_id = int(data.get('tag_id') or 0)
    except (TypeError, ValueError):
        tag_id = 0

    tag = SupportTag.query.get(tag_id) if tag_id else None
    if not tag:
        return jsonify({'ok': False, 'error': 'Etiqueta no encontrada.'}), 404

    if str(data.get('action') or 'add') == 'remove':
        support_service.remove_tag(chat, tag)
        applied = False
    else:
        support_service.add_tag(chat, tag, admin_id=_admin_id())
        applied = True

    db.session.commit()
    return jsonify({'ok': True, 'tag_id': tag.id, 'applied': applied})


# ─── Nota interna ────────────────────────────────────────────────────────────

@admin_support_bp.route('/<int:chat_id>/nota', methods=['POST'])
@login_required
def save_note(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    data = request.get_json(silent=True) or request.form

    support_service.save_admin_note(chat, data.get('note'), admin_id=_admin_id())

    return jsonify({
        'ok': True,
        'has_note': bool(chat.admin_note),
        'saved_at': format_ve(chat.admin_note_at) if chat.admin_note_at else '',
        'author': chat.admin_note_author.username if chat.admin_note_author else '',
    })


# ─── Vinculación con una orden ───────────────────────────────────────────────

@admin_support_bp.route('/<int:chat_id>/buscar-ordenes')
@login_required
def search_orders(chat_id):
    SupportChat.query.get_or_404(chat_id)
    orders = support_service.search_orders(request.args.get('q'))
    return jsonify({'ok': True, 'orders': [{
        'id': o.id,
        'order_number': o.order_number,
        'player_id': o.player_id or '',
        'nickname': o.player_nickname or '',
        'game': o.game.name if o.game else '',
        'package': o.package.name if o.package else '',
        'status': o.status,
        'status_label': o.status_label,
        'email': o.email or '',
        'created_at': (o.created_at or datetime.utcnow()).isoformat() + 'Z',
    } for o in orders]})


@admin_support_bp.route('/<int:chat_id>/vincular', methods=['POST'])
@login_required
def link_order(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    data = request.get_json(silent=True) or request.form

    order = Order.query.get(data.get('order_id'))
    if not order:
        return jsonify({'ok': False, 'error': 'Orden no encontrada.'}), 404

    support_service.link_order(chat, order, admin_id=_admin_id())
    return jsonify({'ok': True, 'order_number': order.order_number})


@admin_support_bp.route('/<int:chat_id>/desvincular', methods=['POST'])
@login_required
def unlink_order(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    chat.order_id = None
    chat.user_id = None
    support_service.sync_unidentified_tag(chat)
    support_service.add_system_message(chat, 'Se desvinculó la orden de este chat.')
    db.session.commit()
    return jsonify({'ok': True})


# ─── Estado del chat ─────────────────────────────────────────────────────────

@admin_support_bp.route('/<int:chat_id>/estado', methods=['POST'])
@login_required
def set_status(chat_id):
    chat = SupportChat.query.get_or_404(chat_id)
    action = (request.form.get('action') or (request.get_json(silent=True) or {}).get('action') or '').strip()

    if action == 'cerrar':
        support_service.close_chat(chat, by_admin=True, admin_id=_admin_id())
    elif action == 'reabrir':
        support_service.reopen_chat(chat)
    elif action == 'bloquear':
        # Corta a un insistente sin tocar la IP: bloquear por IP en
        # Venezuela suele llevarse por delante a media zona detrás del
        # mismo CGNAT del operador.
        chat.is_blocked = True
        chat.status = 'closed'
        chat.closed_at = datetime.utcnow()
        chat.unread_admin = 0
        db.session.commit()
    elif action == 'desbloquear':
        chat.is_blocked = False
        db.session.commit()
    else:
        return jsonify({'ok': False, 'error': 'Acción desconocida.'}), 400

    if request.is_json:
        return jsonify({'ok': True, 'status': chat.status, 'blocked': bool(chat.is_blocked)})
    return redirect(url_for('admin_support_bp.detail', chat_id=chat.id))


# ─── Catálogo de etiquetas ───────────────────────────────────────────────────

@admin_support_bp.route('/etiquetas', methods=['GET'])
@login_required
def tags():
    return render_template(
        'admin/support_tags.html',
        user_tags=SupportTag.query.filter_by(kind='user').order_by(SupportTag.sort_order).all(),
        error_tags=SupportTag.query.filter_by(kind='error').order_by(SupportTag.sort_order).all(),
        idle_minutes=support_service.get_idle_close_minutes(),
    )


@admin_support_bp.route('/etiquetas/inactividad', methods=['POST'])
@login_required
def set_idle_minutes():
    from ..models import Setting

    raw = (request.form.get('minutes') or '').strip()
    try:
        minutes = max(0, min(int(raw), 1440))
    except (TypeError, ValueError):
        flash('Pon un número de minutos.', 'danger')
        return redirect(url_for('admin_support_bp.tags'))

    row = Setting.query.filter_by(key=support_service.IDLE_CLOSE_SETTING_KEY).first()
    if not row:
        row = Setting(key=support_service.IDLE_CLOSE_SETTING_KEY,
                      description='Minutos sin respuesta del cliente antes de cerrar su chat')
        db.session.add(row)
    row.value = str(minutes)
    db.session.commit()

    flash('Cierre automático desactivado.' if minutes == 0
          else f'Los chats se cerrarán tras {minutes} minutos sin respuesta.', 'success')
    return redirect(url_for('admin_support_bp.tags'))


@admin_support_bp.route('/etiquetas/crear', methods=['POST'])
@login_required
def tag_create():
    name = (request.form.get('name') or '').strip()
    kind = (request.form.get('kind') or 'error').strip()

    if not name:
        flash('Ponle un nombre a la etiqueta.', 'danger')
        return redirect(url_for('admin_support_bp.tags'))
    if kind not in ('user', 'error'):
        kind = 'error'

    slug = _slugify(name)
    if SupportTag.query.filter_by(slug=slug).first():
        flash('Ya existe una etiqueta con ese nombre.', 'warning')
        return redirect(url_for('admin_support_bp.tags'))

    last = (SupportTag.query.filter_by(kind=kind)
            .order_by(SupportTag.sort_order.desc()).first())

    db.session.add(SupportTag(
        name=name[:60], slug=slug, kind=kind,
        color=(request.form.get('color') or '#6c5ce7')[:9],
        sort_order=(last.sort_order or 0) + 1 if last else 1,
    ))
    db.session.commit()
    flash('Etiqueta creada.', 'success')
    return redirect(url_for('admin_support_bp.tags'))


@admin_support_bp.route('/etiquetas/<int:tag_id>/editar', methods=['POST'])
@login_required
def tag_edit(tag_id):
    tag = SupportTag.query.get_or_404(tag_id)
    name = (request.form.get('name') or '').strip()
    if name:
        tag.name = name[:60]
    tag.color = (request.form.get('color') or tag.color)[:9]
    tag.is_active = request.form.get('is_active') == 'on'
    db.session.commit()
    flash('Etiqueta actualizada.', 'success')
    return redirect(url_for('admin_support_bp.tags'))


@admin_support_bp.route('/etiquetas/<int:tag_id>/borrar', methods=['POST'])
@login_required
def tag_delete(tag_id):
    tag = SupportTag.query.get_or_404(tag_id)

    # "Sin identificar" la mantiene el sistema sola; borrarla dejaría al
    # admin sin la cola de chats por identificar y sin forma de recuperarla
    # salvo recreándola con el mismo slug exacto.
    if tag.slug == 'sin-identificar':
        flash('Esa etiqueta la usa el sistema y no se puede borrar. Puedes desactivarla.', 'warning')
        return redirect(url_for('admin_support_bp.tags'))

    SupportChatTag.query.filter_by(tag_id=tag.id).delete()
    db.session.delete(tag)
    db.session.commit()
    flash('Etiqueta borrada.', 'success')
    return redirect(url_for('admin_support_bp.tags'))


@admin_support_bp.app_context_processor
def _inject_support_pending():
    """Badge de chats sin responder, junto al link de Soporte."""
    try:
        return {'support_pending_count': support_service.pending_chats_count()}
    except Exception:
        return {'support_pending_count': 0}
