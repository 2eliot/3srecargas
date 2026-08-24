"""Autoservicio de mini influencers (/minis).

Un solo link: /minis sirve de login + solicitud de alta. Una vez aprobado
por el admin desde /admin/minis, el mini entra a su panel a subir videos
como prueba de alcance, ver su progreso de rango y pedir retiro de su
balance. La sesión usa flask_login igual que clientes/admin (ver
get_id()='mini:<id>' en Affiliate y las ramas nuevas en
app/__init__.py::load_user/unauthorized_handler).
"""

from datetime import datetime

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..models import Affiliate, AffiliateWithdrawal, MiniRank, MiniVideo, MiniViewTier, PaymentMethod, db
from ..utils.mini_influencers import (
    clear_attempts,
    count_qualifying_uses,
    detect_platform,
    generate_temp_code,
    get_rank_progress,
    is_rate_limited,
    normalize_video_url,
    register_failed_attempt,
    suggested_reward_for_views,
)

minis_bp = Blueprint('minis_bp', __name__)


def _client_ip():
    """IP real del cliente. La app corre detrás de nginx, así que la
    cabecera del proxy es la que tiene la IP de verdad."""
    forwarded = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    return forwarded or (request.remote_addr or '')


def _current_mini_ok():
    return current_user.__class__.__name__ == 'Affiliate' and bool(getattr(current_user, 'is_mini', False))


@minis_bp.before_request
def minis_access_guard():
    if request.endpoint in {'minis_bp.login', 'minis_bp.mini_login_submit', 'minis_bp.mini_register_submit'}:
        return None
    if not current_user.is_authenticated:
        return None
    if not _current_mini_ok():
        flash('Esta sección es solo para mini influencers.', 'warning')
        return redirect(url_for('main_bp.index'))
    return None


@minis_bp.route('', methods=['GET'])
def login():
    if current_user.is_authenticated and _current_mini_ok():
        return redirect(url_for('minis_bp.panel'))
    return render_template('minis/login.html')


@minis_bp.route('/ingresar', methods=['POST'])
def mini_login_submit():
    ip = _client_ip()
    if is_rate_limited(ip):
        return jsonify({'ok': False, 'message': 'Demasiados intentos seguidos. Espera unos minutos.'}), 429

    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    if not email or not password:
        return jsonify({'ok': False, 'message': 'Escribe tu correo y contraseña.'}), 400

    affiliate = Affiliate.query.filter(
        Affiliate.is_mini.is_(True),
        db.func.lower(Affiliate.email) == email,
    ).first()

    if not affiliate or not affiliate.check_password(password):
        register_failed_attempt(ip)
        return jsonify({'ok': False, 'message': 'Correo o contraseña incorrectos.'}), 401

    if affiliate.status == 'pending':
        return jsonify({'ok': False, 'message': 'Tu solicitud todavía está en revisión. Te avisaremos por WhatsApp cuando la aprobemos.'}), 403

    if affiliate.status == 'rejected':
        reason = (affiliate.rejection_reason or '').strip()
        message = 'Tu solicitud fue rechazada.' + (f' Motivo: {reason}' if reason else '')
        return jsonify({'ok': False, 'message': message}), 403

    clear_attempts(ip)
    login_user(affiliate)
    return jsonify({'ok': True, 'redirect': url_for('minis_bp.panel')})


@minis_bp.route('/solicitar', methods=['POST'])
def mini_register_submit():
    ip = _client_ip()
    if is_rate_limited(ip):
        return jsonify({'ok': False, 'message': 'Demasiados intentos seguidos. Espera unos minutos.'}), 429

    data = request.get_json(silent=True) or {}
    channel_name = (data.get('channel_name') or '').strip()[:100]
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    channel_url = (data.get('channel_url') or '').strip()[:255]
    whatsapp_phone = (data.get('whatsapp_phone') or '').strip()[:50]
    application_note = (data.get('application_note') or '').strip()[:1000]

    if not channel_name:
        return jsonify({'ok': False, 'message': 'Escribe el nombre de tu canal o cuenta.'}), 400
    if not email or '@' not in email:
        return jsonify({'ok': False, 'message': 'Escribe un correo válido.'}), 400
    if len(password) < 6:
        return jsonify({'ok': False, 'message': 'La contraseña debe tener al menos 6 caracteres.'}), 400
    if not channel_url:
        return jsonify({'ok': False, 'message': 'Escribe el link de tu canal o red social.'}), 400
    if not whatsapp_phone:
        return jsonify({'ok': False, 'message': 'Escribe tu número de WhatsApp.'}), 400
    if not application_note:
        return jsonify({'ok': False, 'message': 'Cuéntanos de qué trata tu contenido.'}), 400

    existing = Affiliate.query.filter(
        Affiliate.is_mini.is_(True),
        db.func.lower(Affiliate.email) == email,
    ).first()
    if existing:
        register_failed_attempt(ip)
        return jsonify({'ok': False, 'message': 'Ya existe una solicitud con ese correo.'}), 409

    affiliate = Affiliate(
        name=channel_name,
        email=email,
        code=generate_temp_code(),
        commission_rate=0,
        client_discount_rate=0,
        is_active=False,
        status='pending',
        is_mini=True,
        channel_url=channel_url,
        whatsapp_phone=whatsapp_phone,
        application_note=application_note,
    )
    affiliate.set_password(password)
    db.session.add(affiliate)
    db.session.commit()

    clear_attempts(ip)
    return jsonify({'ok': True, 'message': 'Tu solicitud quedó registrada. Te avisaremos cuando sea aprobada.'})


@minis_bp.route('/panel', methods=['GET'])
@login_required
def panel():
    affiliate = current_user
    rank_progress = get_rank_progress(affiliate) if affiliate.status == 'approved' else None
    videos = (
        MiniVideo.query.filter_by(affiliate_id=affiliate.id).order_by(MiniVideo.created_at.desc()).all()
        if affiliate.status == 'approved' else []
    )
    withdrawals = (
        AffiliateWithdrawal.query.filter_by(affiliate_id=affiliate.id).order_by(AffiliateWithdrawal.created_at.desc()).all()
        if affiliate.status == 'approved' else []
    )
    view_tiers = MiniViewTier.query.order_by(MiniViewTier.sort_order.asc(), MiniViewTier.min_views.asc()).all()
    payment_methods = PaymentMethod.query.filter_by(is_active=True).order_by(PaymentMethod.sort_order.asc()).all()
    has_pending_withdrawal = any(w.status == 'pending' for w in withdrawals)

    return render_template(
        'minis/panel.html',
        affiliate=affiliate,
        rank_progress=rank_progress,
        videos=videos,
        withdrawals=withdrawals,
        view_tiers=view_tiers,
        payment_methods=payment_methods,
        has_pending_withdrawal=has_pending_withdrawal,
    )


@minis_bp.route('/videos', methods=['POST'])
@login_required
def mini_video_add():
    affiliate = current_user
    if affiliate.status != 'approved' or not affiliate.is_active:
        return jsonify({'ok': False, 'message': 'Tu perfil todavía no está activo.'}), 403

    data = request.get_json(silent=True) or {}
    url = normalize_video_url(data.get('url'))
    if not url:
        return jsonify({'ok': False, 'message': 'Escribe el link del video.'}), 400
    if len(url) > 500:
        return jsonify({'ok': False, 'message': 'Ese link es demasiado largo.'}), 400

    try:
        views_declared = max(0, int(data.get('views_declared') or 0))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'message': 'Las vistas declaradas deben ser un número.'}), 400

    dupe = MiniVideo.query.filter(
        MiniVideo.affiliate_id == affiliate.id,
        db.func.lower(MiniVideo.url) == url.lower(),
    ).first()
    if dupe:
        return jsonify({'ok': False, 'message': 'Ya subiste ese video.'}), 409

    video = MiniVideo(
        affiliate_id=affiliate.id,
        url=url,
        platform=detect_platform(url),
        views_declared=views_declared,
    )
    db.session.add(video)
    db.session.commit()

    return jsonify({
        'ok': True,
        'message': 'Video enviado, en revisión.',
        'video': {
            'id': video.id,
            'url': video.url,
            'platform': video.platform,
            'views_declared': video.views_declared,
            'status': video.status,
            'suggested_reward': suggested_reward_for_views(views_declared),
        },
    })


@minis_bp.route('/videos/<int:video_id>/eliminar', methods=['POST'])
@login_required
def mini_video_delete(video_id):
    video = MiniVideo.query.get(video_id)
    if not video or video.affiliate_id != current_user.id:
        return jsonify({'ok': False, 'message': 'Video no encontrado.'}), 404
    if video.status != 'pending':
        return jsonify({'ok': False, 'message': 'Solo puedes borrar videos pendientes.'}), 400

    db.session.delete(video)
    db.session.commit()
    return jsonify({'ok': True})


@minis_bp.route('/retiro', methods=['POST'])
@login_required
def mini_withdraw():
    affiliate = current_user
    if affiliate.status != 'approved' or not affiliate.is_active:
        return jsonify({'ok': False, 'message': 'Tu perfil todavía no está activo.'}), 403

    data = request.get_json(silent=True) or {}
    try:
        amount = round(float(data.get('amount') or 0), 2)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'message': 'Monto inválido.'}), 400

    method = (data.get('method') or '').strip()
    payout_details = (data.get('payout_details') or '').strip()[:500]

    if amount <= 0:
        return jsonify({'ok': False, 'message': 'El monto a retirar debe ser mayor a 0.'}), 400
    if amount > float(affiliate.balance or 0):
        return jsonify({'ok': False, 'message': 'No tienes saldo suficiente para ese monto.'}), 400
    if not PaymentMethod.query.filter_by(code=method, is_active=True).first():
        return jsonify({'ok': False, 'message': 'Selecciona un método de pago válido.'}), 400
    if not payout_details:
        return jsonify({'ok': False, 'message': 'Escribe los datos donde quieres recibir el pago.'}), 400
    if AffiliateWithdrawal.query.filter_by(affiliate_id=affiliate.id, status='pending').first():
        return jsonify({'ok': False, 'message': 'Ya tienes un retiro pendiente. Espera a que se resuelva.'}), 409

    withdrawal = AffiliateWithdrawal(
        affiliate_id=affiliate.id,
        amount=amount,
        method=method,
        payout_details=payout_details,
    )
    db.session.add(withdrawal)
    db.session.commit()

    return jsonify({'ok': True, 'message': 'Solicitud de retiro enviada. La revisamos pronto.'})


@minis_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    flash('Sesión de mini influencer cerrada.', 'info')
    return redirect(url_for('minis_bp.login'))
