import hmac
import os
import json
import re
import requests
from collections import defaultdict
from datetime import datetime, timedelta
from functools import wraps
from flask import (
    Blueprint, render_template, request, redirect, Response,
    url_for, flash, session, current_app, jsonify
)
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import or_, false
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename
from ..models import (
    db, AdminUser, Game, Package, Category, Order,
    Pin, Affiliate, AffiliateCommission, PaymentMethod, Setting, Discount,
    OrderMiniGameOpportunity, PlayerPoints, PointsPrizeMapping, PointsSpinLog,
    RevendedoresCatalogItem, RevendedoresItemMapping, GiftCode,
    AffiliateWithdrawal, MiniRank, MiniVideo, MiniViewTier,
)
from ..utils.availability import format_hour, get_manual_schedule
from ..utils.gift_codes import create_batch as create_gift_batch, format_code as format_gift_code
from ..utils.mini_influencers import (
    award_rank_bonus,
    approve_withdrawal as approve_mini_withdrawal,
    get_rank_progress,
    reject_withdrawal as reject_mini_withdrawal,
    review_mini_video,
    suggested_reward_for_views,
)
from ..utils.timezone import format_ve, now_ve, now_ve_naive, to_ve, ve_day_start_utc_naive
from ..utils.minigames import (
    get_minigame_slot_defs,
    get_minigame_slots_config,
    get_minigame_win_interval,
    get_minigame_counter_scope_key,
    get_or_create_minigame_counter,
    is_minigame_dev_mode,
    DEFAULT_MINIGAME_WIN_INTERVAL,
)
from ..utils.notifications import (
    notify_order_approved, notify_order_completed, notify_order_rejected,
)
from ..utils.order_processing import approve_order, get_revendedores_env, process_affiliate_commission, process_revendedores_queue
from ..models import PushSubscription
from ..utils.push_notifications import is_push_configured, send_push_broadcast
from ..utils.points import (
    DEFAULT_POINTS_PER_DOLLAR,
    DEFAULT_POINTS_SPIN_COST,
    DEFAULT_POINTS_WIN_INTERVAL,
    get_points_per_dollar_rate,
    get_points_spin_cost,
    get_points_win_interval,
    get_player_points_balance,
)
from ..utils.auth_accounts import (
    attach_matching_orders_to_customer,
    extract_customer_identifier_for_game,
    get_or_create_scoped_customer,
    sync_env_admin_user,
)
from ..utils.payment_verification import (
    clear_pabilo_verification_state,
    normalize_reference_last5,
    payment_method_uses_payer_identity_verification,
    stamp_verified_payment,
    verify_order_payment,
)

admin_bp = Blueprint('admin_bp', __name__)

HOUSEKEEPING_ORDER_RETENTION_DAYS = 60  # ~2 months
HOUSEKEEPING_PIN_RETENTION_DAYS = 30
HOUSEKEEPING_INTERVAL_HOURS = 6
_last_housekeeping_run = None

ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_VIDEO_EXT = {'mp4', 'webm', 'mov', 'm4v'}
PROTECTED_CATEGORY_SLUGS = {'juegos', 'tarjetas', 'wallet'}
RANKING_PRIZE_POSITIONS = [1, 2, 3, 4, 5]
RANKING_PRIZE_LABELS = {
    'free_fire': ['6160 diamantes', '2398 diamantes', '1166 diamantes', '572 diamantes', '341 diamantes'],
    'blood_strike': ['1500 oro', '700 oro', '350 oro', '200 oro', '120 oro'],
}
GAME_PLAYER_INPUT_TYPES = {'numeric', 'text', 'email'}


def slugify_category(value):
    value = (value or '').strip().lower()
    value = re.sub(r'[^a-z0-9]+', '-', value)
    value = re.sub(r'-{2,}', '-', value).strip('-')
    return value


def normalize_game_player_input_type(value):
    normalized = (value or '').strip().lower()
    return normalized if normalized in GAME_PLAYER_INPUT_TYPES else 'numeric'


def normalize_order_player_id(order, raw_value):
    value = ' '.join(str(raw_value or '').strip().split())
    if not order or not order.game:
        return value

    category_slug = ((order.game.category.slug if order.game.category else '') or '').lower()
    if category_slug == 'tarjetas':
        return ''

    player_input_type = normalize_game_player_input_type(getattr(order.game, 'player_id_input_type', 'numeric'))
    if player_input_type == 'numeric':
        return ''.join(ch for ch in value if ch.isdigit())

    return value


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


def delete_uploaded_file(relative_path):
    if not relative_path:
        return
    upload_root = current_app.config['UPLOAD_FOLDER']
    file_path = os.path.join(upload_root, relative_path)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except OSError:
            pass


MAX_IMAGE_DIMENSION = 1920


def _compress_and_save_image(file, dest_path, ext):
    """Redimensiona y comprime antes de guardar. Estas imágenes (logo, fondo,
    banners, juegos) se sirven a todos los visitantes, así que una foto de
    celular sin comprimir (3-8 MB) hace lenta la primera carga. GIF se guarda
    tal cual para no romper animaciones; ante cualquier error se guarda el
    archivo original como antes."""
    if ext == 'gif':
        file.save(dest_path)
        return

    try:
        from PIL import Image, ImageOps

        image = Image.open(file.stream)
        image = ImageOps.exif_transpose(image)
        if max(image.size) > MAX_IMAGE_DIMENSION:
            image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.LANCZOS)

        if ext in ('jpg', 'jpeg'):
            if image.mode not in ('RGB', 'L'):
                image = image.convert('RGB')
            image.save(dest_path, format='JPEG', quality=82, optimize=True, progressive=True)
        elif ext == 'webp':
            if image.mode == 'P':
                image = image.convert('RGBA')
            image.save(dest_path, format='WEBP', quality=82, method=6)
        else:  # png
            image.save(dest_path, format='PNG', optimize=True)
    except Exception:
        file.stream.seek(0)
        file.save(dest_path)


def save_image(file, subfolder=''):
    if not file or not allowed_file(file.filename):
        return None
    filename = secure_filename(file.filename)
    ext = file.filename.rsplit('.', 1)[1].lower()
    ts = now_ve_naive().strftime('%Y%m%d%H%M%S%f')
    filename = f"{ts}_{filename}"
    folder = current_app.config['UPLOAD_FOLDER']
    if subfolder:
        folder = os.path.join(folder, subfolder)
    os.makedirs(folder, exist_ok=True)
    _compress_and_save_image(file, os.path.join(folder, filename), ext)
    return (subfolder + '/' + filename) if subfolder else filename


def allowed_video_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_VIDEO_EXT


def save_video(file, subfolder=''):
    if not file or not allowed_video_file(file.filename):
        return None
    filename = secure_filename(file.filename)
    ts = now_ve_naive().strftime('%Y%m%d%H%M%S%f')
    filename = f"{ts}_{filename}"
    folder = current_app.config['UPLOAD_FOLDER']
    if subfolder:
        folder = os.path.join(folder, subfolder)
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, filename))
    return (subfolder + '/' + filename) if subfolder else filename


def order_supports_delivery_proof(order):
    if not order or not order.package or not order.game:
        return False
    category_slug = (order.game.category.slug if order.game and order.game.category else '').lower()
    return not (order.package.is_automated or category_slug == 'tarjetas')


def cleanup_old_orders():
    threshold = datetime.utcnow() - timedelta(days=HOUSEKEEPING_ORDER_RETENTION_DAYS)
    old_orders = Order.query.filter(Order.created_at < threshold).all()
    removed = 0
    for order in old_orders:
        if order.payment_capture:
            delete_uploaded_file(order.payment_capture)
        # Las oportunidades de minijuego referencian la orden, así que hay que
        # borrarlas primero o el DELETE falla por la restricción de clave ajena.
        for opp in OrderMiniGameOpportunity.query.filter_by(order_id=order.id).all():
            db.session.delete(opp)
        db.session.delete(order)
        removed += 1
    if removed:
        db.session.commit()


def cleanup_used_pins():
    threshold = datetime.utcnow() - timedelta(days=HOUSEKEEPING_PIN_RETENTION_DAYS)
    old_pins = (
        Pin.query
        .filter(Pin.is_used.is_(True))
        .filter(Pin.used_at.isnot(None))
        .filter(Pin.used_at < threshold)
        .all()
    )
    if not old_pins:
        return
    for pin in old_pins:
        db.session.delete(pin)
    db.session.commit()


def run_housekeeping_if_needed():
    global _last_housekeeping_run
    now = datetime.utcnow()
    if _last_housekeeping_run and (now - _last_housekeeping_run) < timedelta(hours=HOUSEKEEPING_INTERVAL_HOURS):
        return
    cleanup_old_orders()
    cleanup_used_pins()
    _last_housekeeping_run = now


def _ranking_prize_package_key(ranking_key, position):
    return f'ranking_{ranking_key}_prize_package_{position}'


def _ranking_prize_auto_key(ranking_key, position):
    return f'ranking_{ranking_key}_prize_auto_{position}'


def _parse_optional_decimal(raw_value):
    raw_value = (raw_value or '').strip()
    if not raw_value:
        return None
    return float(raw_value)


def _parse_optional_int(raw_value):
    raw_value = (raw_value or '').strip()
    if not raw_value:
        return None
    return int(raw_value)


def _parse_optional_datetime(raw_value):
    raw_value = (raw_value or '').strip()
    if not raw_value:
        return None
    return datetime.strptime(raw_value, '%Y-%m-%dT%H:%M')


def _discount_kind_label(discount):
    usage_limit = int(discount.usage_limit or 0)
    if usage_limit == 1:
        return 'Único (1 sola vez)'
    if usage_limit > 1:
        return f'Multi-uso (hasta {usage_limit})'
    return 'Masivo'


def _discount_value_label(discount):
    if discount.discount_type == 'percentage':
        label = f'{float(discount.discount_value or 0):.0f}%'
        if discount.max_discount is not None:
            max_discount = float(discount.max_discount or 0)
            label = f'{label} (max. ${max_discount:.2f})'.rstrip('0').rstrip('.')
        if discount.min_amount is not None:
            min_amount = float(discount.min_amount or 0)
            label = f'{label} | min. ${min_amount:.2f}'.rstrip('0').rstrip('.')
        return label
    value = float(discount.discount_value or 0)
    return f'${value:.2f}'.rstrip('0').rstrip('.')


@admin_bp.before_app_request
def admin_housekeeping_hook():
    if not request.path.startswith('/admin'):
        return
    run_housekeeping_if_needed()


@admin_bp.before_request
def admin_access_guard():
    if request.endpoint in {'admin_bp.login'}:
        return None
    if not current_user.is_authenticated:
        return None
    if current_user.__class__.__name__ != 'AdminUser':
        flash('Esta sección es solo para administradores.', 'warning')
        return redirect(url_for('main_bp.index'))
    return None


# ─── Auth ────────────────────────────────────────────────────────────────────

@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        if current_user.__class__.__name__ == 'AdminUser':
            return redirect(url_for('admin_bp.dashboard'))
        return redirect(url_for('main_bp.index'))

    env_admin_username = (os.environ.get('ADMIN_USERNAME') or '').strip()
    env_admin_password = (os.environ.get('ADMIN_PASSWORD') or '').strip()
    env_admin_email = (os.environ.get('ADMIN_EMAIL') or '').strip()

    if request.method == 'POST':
        if not env_admin_username or not env_admin_password:
            flash('Acceso admin no disponible: faltan ADMIN_USERNAME/ADMIN_PASSWORD en entorno.', 'danger')
            return render_template('admin/login.html')

        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '').strip()

        valid_identifiers = {env_admin_username.lower()}
        if env_admin_email:
            valid_identifiers.add(env_admin_email.lower())

        if identifier.lower() not in valid_identifiers or password != env_admin_password:
            flash('Correo/usuario admin o contraseña incorrectos.', 'danger')
            return render_template('admin/login.html')

        try:
            user = sync_env_admin_user(env_admin_username, env_admin_email, env_admin_password)
        except Exception as exc:
            db.session.rollback()
            flash(f'No se pudo sincronizar la cuenta de administrador. {exc}', 'danger')
            return render_template('admin/login.html')

        if user:
            login_user(user)
            return redirect(url_for('admin_bp.dashboard'))

        flash('No se pudo iniciar sesión de administrador.', 'danger')
    return render_template('admin/login.html')


@admin_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('admin_bp.login'))


# ─── Dashboard ───────────────────────────────────────────────────────────────

@admin_bp.route('/')
@login_required
def dashboard():
    total_orders = Order.query.count()
    pending = Order.query.filter_by(status='pending').count()
    completed = Order.query.filter_by(status='completed').count()
    approved = Order.query.filter_by(status='approved').count()
    rejected = Order.query.filter_by(status='rejected').count()
    revenue = db.session.query(
        db.func.sum(Order.amount)
    ).filter(Order.status.in_(['approved', 'completed'])).scalar() or 0

    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(50).all()

    low_stock = (
        Package.query
        .filter_by(is_automated=True, is_active=True)
        .all()
    )
    low_stock = [p for p in low_stock if p.pin_count < 5]

    return render_template(
        'admin/dashboard.html',
        total_orders=total_orders,
        pending=pending,
        completed=completed,
        approved=approved,
        rejected=rejected,
        revenue=revenue,
        recent_orders=recent_orders,
        low_stock=low_stock,
        to_deliver=count_orders_to_deliver(),
    )


# ─── Categories / Services ──────────────────────────────────────────────────

@admin_bp.route('/categories')
@login_required
def categories():
    all_categories = Category.query.order_by(Category.id.asc()).all()
    return render_template('admin/categories.html', categories=all_categories, protected_slugs=PROTECTED_CATEGORY_SLUGS)


@admin_bp.route('/categories/add', methods=['POST'])
@login_required
def category_add():
    name = request.form.get('name', '').strip()
    slug = slugify_category(request.form.get('slug') or name)
    icon = (request.form.get('icon') or '🎮').strip()[:10]

    if not name:
        flash('El nombre del servicio es obligatorio.', 'danger')
        return redirect(url_for('admin_bp.categories'))

    if not slug:
        flash('No se pudo generar un slug válido para el servicio.', 'danger')
        return redirect(url_for('admin_bp.categories'))

    if Category.query.filter_by(slug=slug).first():
        flash('Ya existe un servicio con ese slug.', 'danger')
        return redirect(url_for('admin_bp.categories'))

    category = Category(name=name, slug=slug, icon=icon or '🎮')
    db.session.add(category)
    db.session.commit()
    flash(f'Servicio "{name}" creado.', 'success')
    return redirect(url_for('admin_bp.categories'))


@admin_bp.route('/categories/<int:category_id>/edit', methods=['POST'])
@login_required
def category_edit(category_id):
    category = Category.query.get_or_404(category_id)
    name = request.form.get('name', '').strip()
    slug = slugify_category(request.form.get('slug') or category.slug)
    icon = (request.form.get('icon') or '🎮').strip()[:10]

    if not name:
        flash('El nombre del servicio es obligatorio.', 'danger')
        return redirect(url_for('admin_bp.categories'))

    if category.slug in PROTECTED_CATEGORY_SLUGS:
        slug = category.slug

    if not slug:
        flash('El slug del servicio es inválido.', 'danger')
        return redirect(url_for('admin_bp.categories'))

    duplicate = Category.query.filter(Category.slug == slug, Category.id != category.id).first()
    if duplicate:
        flash('Ya existe otro servicio con ese slug.', 'danger')
        return redirect(url_for('admin_bp.categories'))

    category.name = name
    category.slug = slug
    category.icon = icon or '🎮'
    db.session.commit()
    flash('Servicio actualizado.', 'success')
    return redirect(url_for('admin_bp.categories'))


@admin_bp.route('/categories/<int:category_id>/delete', methods=['POST'])
@login_required
def category_delete(category_id):
    category = Category.query.get_or_404(category_id)

    if category.slug in PROTECTED_CATEGORY_SLUGS:
        flash('Los servicios base no se pueden eliminar.', 'danger')
        return redirect(url_for('admin_bp.categories'))

    if category.games.count() > 0:
        flash('No puedes eliminar este servicio porque todavía tiene juegos asociados.', 'danger')
        return redirect(url_for('admin_bp.categories'))

    db.session.delete(category)
    db.session.commit()
    flash('Servicio eliminado.', 'success')
    return redirect(url_for('admin_bp.categories'))


# ─── Games ───────────────────────────────────────────────────────────────────

@admin_bp.route('/games')
@login_required
def games():
    all_games = Game.query.order_by(Game.category_id, Game.position, Game.name).all()
    categories = Category.query.all()
    return render_template('admin/games.html', games=all_games, categories=categories)


@admin_bp.route('/games/add', methods=['POST'])
@login_required
def game_add():
    name = request.form.get('name', '').strip()
    category_id = request.form.get('category_id')
    requires_zone_id = bool(request.form.get('requires_zone_id'))
    player_id_label = request.form.get('player_id_label', 'Player ID').strip()
    player_id_input_type = normalize_game_player_input_type(request.form.get('player_id_input_type'))
    zone_id_label = request.form.get('zone_id_label', 'Zone ID').strip()
    bs_rate_override_raw = request.form.get('bs_rate_override', '').strip()
    is_automated = bool(request.form.get('is_automated'))
    show_selection_popup = bool(request.form.get('show_selection_popup'))
    position = int(request.form.get('position', 100))
    description = request.form.get('description', '').strip()

    if not name or not category_id:
        flash('Nombre y categoría son obligatorios.', 'danger')
        return redirect(url_for('admin_bp.games'))

    try:
        bs_rate_override = float(bs_rate_override_raw) if bs_rate_override_raw else None
    except ValueError:
        flash('La tasa Bs del juego debe ser un número válido.', 'danger')
        return redirect(url_for('admin_bp.games'))

    if bs_rate_override is not None and bs_rate_override <= 0:
        flash('La tasa Bs del juego debe ser mayor a 0.', 'danger')
        return redirect(url_for('admin_bp.games'))

    slug = name.lower().replace(' ', '-').replace('/', '-')
    existing = Game.query.filter_by(slug=slug).first()
    if existing:
        slug = f"{slug}-{Game.query.count()}"

    image = save_image(request.files.get('image'), 'games')
    game = Game(
        name=name, slug=slug, category_id=int(category_id),
        requires_zone_id=requires_zone_id, player_id_label=player_id_label,
        player_id_input_type=player_id_input_type,
        zone_id_label=zone_id_label, is_automated=is_automated,
        bs_rate_override=bs_rate_override,
        show_selection_popup=show_selection_popup,
        position=position, description=description, image=image,
    )
    db.session.add(game)
    db.session.commit()
    flash(f'Juego "{name}" creado.', 'success')
    return redirect(url_for('admin_bp.games'))


@admin_bp.route('/games/<int:game_id>/edit', methods=['POST'])
@login_required
def game_edit(game_id):
    game = Game.query.get_or_404(game_id)
    game.name = request.form.get('name', game.name).strip()
    game.category_id = int(request.form.get('category_id', game.category_id))
    game.requires_zone_id = bool(request.form.get('requires_zone_id'))
    game.player_id_label = request.form.get('player_id_label', game.player_id_label).strip()
    game.player_id_input_type = normalize_game_player_input_type(
        request.form.get('player_id_input_type', game.player_id_input_type)
    )
    game.zone_id_label = request.form.get('zone_id_label', game.zone_id_label).strip()
    bs_rate_override_raw = request.form.get('bs_rate_override', '')
    game.is_automated = bool(request.form.get('is_automated'))
    game.show_selection_popup = bool(request.form.get('show_selection_popup'))
    game.position = int(request.form.get('position', game.position))
    game.description = request.form.get('description', game.description or '').strip()
    game.is_active = bool(request.form.get('is_active'))

    try:
        game.bs_rate_override = float(bs_rate_override_raw) if str(bs_rate_override_raw).strip() else None
    except ValueError:
        flash('La tasa Bs del juego debe ser un número válido.', 'danger')
        return redirect(url_for('admin_bp.games'))

    if game.bs_rate_override is not None and float(game.bs_rate_override) <= 0:
        flash('La tasa Bs del juego debe ser mayor a 0.', 'danger')
        return redirect(url_for('admin_bp.games'))

    new_image = save_image(request.files.get('image'), 'games')
    if new_image:
        delete_uploaded_file(game.image)
        game.image = new_image

    db.session.commit()
    flash('Juego actualizado.', 'success')
    return redirect(url_for('admin_bp.games'))


@admin_bp.route('/games/<int:game_id>/delete', methods=['POST'])
@login_required
def game_delete(game_id):
    game = Game.query.get_or_404(game_id)
    game.is_active = False
    db.session.commit()
    flash('Juego desactivado.', 'warning')
    return redirect(url_for('admin_bp.games'))


@admin_bp.route('/games/<int:game_id>/delete-permanent', methods=['POST'])
@login_required
def game_delete_permanent(game_id):
    game = Game.query.get_or_404(game_id)

    order_count = Order.query.filter_by(game_id=game.id).count()
    if order_count > 0:
        flash(
            f'No puedes eliminar "{game.name}" porque tiene {order_count} orden(es) asociada(s) '
            '(se perdería el historial). Usa "Off" para ocultarlo en su lugar.',
            'danger',
        )
        return redirect(url_for('admin_bp.games'))

    package_ids = [pkg.id for pkg in Package.query.filter_by(game_id=game.id).all()]
    game_name = game.name

    try:
        if package_ids:
            RevendedoresItemMapping.query.filter(
                RevendedoresItemMapping.store_package_id.in_(package_ids)
            ).delete(synchronize_session=False)
            Pin.query.filter(Pin.package_id.in_(package_ids)).delete(synchronize_session=False)
            Package.query.filter(Package.id.in_(package_ids)).delete(synchronize_session=False)

        db.session.delete(game)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        flash(f'No se pudo eliminar "{game_name}": {exc}', 'danger')
        return redirect(url_for('admin_bp.games'))

    flash(f'Juego "{game_name}" eliminado.', 'success')
    return redirect(url_for('admin_bp.games'))


# ─── Packages ────────────────────────────────────────────────────────────────

@admin_bp.route('/packages')
@login_required
def packages():
    game_id = request.args.get('game_id', type=int)
    query = Package.query.join(Game)
    if game_id:
        query = query.filter(Package.game_id == game_id)
    all_packages = query.order_by(Game.name, Package.sort_order).all()
    all_games = Game.query.filter_by(is_active=True).order_by(Game.name).all()
    return render_template(
        'admin/packages.html',
        packages=all_packages,
        games=all_games,
        selected_game_id=game_id,
    )


PACKAGE_ANNOUNCEMENT_TYPES = {'', 'one_time_purchase', 'redeem_code'}


def _normalize_package_announcement_type(raw_value):
    value = (raw_value or '').strip()
    return value if value in PACKAGE_ANNOUNCEMENT_TYPES else ''


@admin_bp.route('/packages/add', methods=['POST'])
@login_required
def package_add():
    game_id = request.form.get('game_id')
    name = request.form.get('name', '').strip()
    price = request.form.get('price', '0').strip()
    usd_price_raw = request.form.get('usd_price', '').strip()
    description = request.form.get('description', '').strip()
    is_automated = bool(request.form.get('is_automated'))
    sort_order = int(request.form.get('sort_order', 100))
    announcement_type = _normalize_package_announcement_type(request.form.get('announcement_type'))

    if not game_id or not name or not price:
        flash('Juego, nombre y precio son obligatorios.', 'danger')
        return redirect(url_for('admin_bp.packages'))

    try:
        base_price = float(price)
        usd_price = float(usd_price_raw) if usd_price_raw else None
    except ValueError:
        flash('Los precios deben ser números válidos.', 'danger')
        return redirect(url_for('admin_bp.packages'))

    if base_price <= 0 or (usd_price is not None and usd_price <= 0):
        flash('Los precios deben ser mayores a 0.', 'danger')
        return redirect(url_for('admin_bp.packages'))

    image = save_image(request.files.get('image'), 'packages')
    pkg = Package(
        game_id=int(game_id), name=name, price=base_price, usd_price=usd_price,
        description=description, is_automated=is_automated,
        sort_order=sort_order, image=image, announcement_type=announcement_type or None,
    )
    db.session.add(pkg)
    db.session.commit()
    flash(f'Paquete "{name}" creado.', 'success')
    return redirect(url_for('admin_bp.packages', game_id=pkg.game_id))


@admin_bp.route('/packages/<int:pkg_id>/edit', methods=['POST'])
@login_required
def package_edit(pkg_id):
    pkg = Package.query.get_or_404(pkg_id)
    return_game_id = request.form.get('return_game_id', type=int) or pkg.game_id
    pkg.name = request.form.get('name', pkg.name).strip()
    price_raw = request.form.get('price', pkg.price)
    usd_price_raw = request.form.get('usd_price', '')
    try:
        pkg.price = float(price_raw)
        pkg.usd_price = float(usd_price_raw) if str(usd_price_raw).strip() else None
    except ValueError:
        flash('Los precios deben ser números válidos.', 'danger')
        return redirect(url_for('admin_bp.packages', game_id=return_game_id))

    if float(pkg.price or 0) <= 0 or (pkg.usd_price is not None and float(pkg.usd_price) <= 0):
        flash('Los precios deben ser mayores a 0.', 'danger')
        return redirect(url_for('admin_bp.packages', game_id=return_game_id))

    pkg.description = request.form.get('description', pkg.description or '').strip()
    pkg.is_automated = bool(request.form.get('is_automated'))
    pkg.sort_order = int(request.form.get('sort_order', pkg.sort_order))
    pkg.is_active = bool(request.form.get('is_active'))
    pkg.announcement_type = _normalize_package_announcement_type(request.form.get('announcement_type')) or None

    if request.form.get('remove_image'):
        if pkg.image:
            delete_uploaded_file(pkg.image)
        pkg.image = None

    new_image = save_image(request.files.get('image'), 'packages')
    if new_image:
        delete_uploaded_file(pkg.image)
        pkg.image = new_image

    db.session.commit()
    flash('Paquete actualizado.', 'success')
    return redirect(url_for('admin_bp.packages', game_id=return_game_id))


@admin_bp.route('/packages/<int:pkg_id>/delete', methods=['POST'])
@login_required
def package_delete(pkg_id):
    pkg = Package.query.get_or_404(pkg_id)
    pkg.is_active = False
    db.session.commit()
    flash('Paquete desactivado.', 'warning')
    return redirect(url_for('admin_bp.packages', game_id=pkg.game_id))


@admin_bp.route('/packages/<int:pkg_id>/delete-permanent', methods=['POST'])
@login_required
def package_delete_permanent(pkg_id):
    pkg = Package.query.get_or_404(pkg_id)
    game_id = pkg.game_id

    order_count = Order.query.filter_by(package_id=pkg.id).count()
    if order_count > 0:
        flash(
            f'No puedes eliminar "{pkg.name}" porque tiene {order_count} orden(es) asociada(s) '
            '(se perdería el historial). Usa "Off" para ocultarlo en su lugar.',
            'danger',
        )
        return redirect(url_for('admin_bp.packages', game_id=game_id))

    pkg_name = pkg.name
    try:
        RevendedoresItemMapping.query.filter_by(store_package_id=pkg.id).delete(synchronize_session=False)
        Pin.query.filter_by(package_id=pkg.id).delete(synchronize_session=False)
        if pkg.image:
            delete_uploaded_file(pkg.image)
        db.session.delete(pkg)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        flash(f'No se pudo eliminar "{pkg_name}": {exc}', 'danger')
        return redirect(url_for('admin_bp.packages', game_id=game_id))

    flash(f'Paquete "{pkg_name}" eliminado.', 'success')
    return redirect(url_for('admin_bp.packages', game_id=game_id))


# ─── Orders ──────────────────────────────────────────────────────────────────

def _parse_order_filter_date(raw_value, end_of_day=False):
    raw_value = (raw_value or '').strip()
    if not raw_value:
        return None

    try:
        parsed = datetime.strptime(raw_value, '%Y-%m-%d')
    except ValueError:
        return None

    if end_of_day:
        return parsed + timedelta(days=1)
    return parsed


def count_orders_to_deliver():
    """Órdenes con el pago ya verificado que siguen esperando la recarga
    manual. Es el número que hay que tener a la vista para que ninguna se
    quede sin hacer."""
    try:
        return (
            Order.query
            .filter(Order.status == 'pending', Order.payment_verified_at.isnot(None))
            .count()
        )
    except Exception:
        return 0


def _apply_order_filters(
    query,
    status_filter='',
    search_query='',
    date_from='',
    date_to='',
    package_id=None,
    service_id=None,
):
    status_filter = (status_filter or '').strip()
    search_query = (search_query or '').strip()
    date_from = (date_from or '').strip()
    date_to = (date_to or '').strip()

    try:
        package_id = int(package_id) if package_id not in (None, '') else None
    except (TypeError, ValueError):
        package_id = None

    try:
        service_id = int(service_id) if service_id not in (None, '') else None
    except (TypeError, ValueError):
        service_id = None

    # "Por entregar" no es un estado guardado sino una vista: órdenes cuyo
    # pago ya se verificó solo (Binance/Pabilo) pero cuyo producto se recarga
    # a mano, así que siguen pendientes esperando que un admin las haga.
    # Antes se perdían entre el resto de las pendientes y el cliente terminaba
    # reclamando por WhatsApp.
    if status_filter == 'to_deliver':
        query = query.filter(
            Order.status == 'pending',
            Order.payment_verified_at.isnot(None),
        )
    elif status_filter:
        query = query.filter_by(status=status_filter)

    if search_query:
        like_term = f"%{search_query}%"
        maybe_id = int(search_query) if search_query.isdigit() else None
        query = query.filter(
            or_(
                Order.id == maybe_id if maybe_id is not None else false(),
                Order.order_number.ilike(like_term),
                Order.payment_reference.ilike(like_term),
                Order.player_id.ilike(like_term),
                Order.player_nickname.ilike(like_term),
                Order.email.ilike(like_term),
                Order.phone.ilike(like_term),
            )
        )

    parsed_from = _parse_order_filter_date(date_from)
    if parsed_from:
        query = query.filter(Order.created_at >= parsed_from)

    parsed_to = _parse_order_filter_date(date_to, end_of_day=True)
    if parsed_to:
        query = query.filter(Order.created_at < parsed_to)

    if package_id:
        query = query.filter(Order.package_id == package_id)

    if service_id:
        query = query.join(Order.game).filter(Game.category_id == service_id)

    return query

@admin_bp.route('/orders')
@login_required
def orders():
    page = request.args.get('page', type=int) or 1
    if page < 1:
        page = 1

    status_filter = (request.args.get('status') or '').strip()
    search_query = (request.args.get('q') or '').strip()
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    package_id = request.args.get('package_id', type=int)
    service_id = request.args.get('service_id', type=int)
    query = Order.query.order_by(Order.created_at.desc())
    query = _apply_order_filters(
        query,
        status_filter=status_filter,
        search_query=search_query,
        date_from=date_from,
        date_to=date_to,
        package_id=package_id,
        service_id=service_id,
    )
    page_size = 50
    total_orders = query.count()
    total_pages = max(1, (total_orders + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages

    all_orders = (
        query
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    page_window_start = max(1, page - 2)
    page_window_end = min(total_pages, page + 2)
    page_numbers = list(range(page_window_start, page_window_end + 1))
    start_order_index = ((page - 1) * page_size) + 1 if total_orders else 0
    end_order_index = min(page * page_size, total_orders) if total_orders else 0

    services = Category.query.order_by(Category.name.asc()).all()
    packages = (
        Package.query
        .join(Game)
        .order_by(Game.name.asc(), Package.sort_order.asc(), Package.name.asc())
        .all()
    )
    return render_template(
        'admin/orders.html',
        orders=all_orders,
        status_filter=status_filter,
        search_query=search_query,
        date_from=date_from,
        date_to=date_to,
        package_id=package_id,
        service_id=service_id,
        current_page=page,
        page_size=page_size,
        total_orders=total_orders,
        total_pages=total_pages,
        page_numbers=page_numbers,
        start_order_index=start_order_index,
        end_order_index=end_order_index,
        services=services,
        packages=packages,
        to_deliver_count=count_orders_to_deliver(),
    )


@admin_bp.route('/orders/latest')
@login_required
def orders_latest():
    status_filter = (request.args.get('status') or '').strip()
    search_query = (request.args.get('q') or '').strip()
    date_from = (request.args.get('date_from') or '').strip()
    date_to = (request.args.get('date_to') or '').strip()
    package_id = request.args.get('package_id', type=int)
    service_id = request.args.get('service_id', type=int)
    since_id_raw = (request.args.get('since_id') or '').strip()
    try:
        since_id = int(since_id_raw) if since_id_raw else 0
    except Exception:
        since_id = 0

    query = Order.query
    query = _apply_order_filters(
        query,
        status_filter=status_filter,
        search_query=search_query,
        date_from=date_from,
        date_to=date_to,
        package_id=package_id,
        service_id=service_id,
    )
    if since_id:
        query = query.filter(Order.id > since_id)

    newest = query.order_by(Order.id.desc()).limit(20).all()
    newest.reverse()

    payload = []
    for o in newest:
        payload.append({
            'id': o.id,
            'order_number': o.order_number,
            'game': o.game.name if o.game else '',
            'package': o.package.name if o.package else '',
            'player_id': o.player_id or '',
            'player_nickname': o.player_nickname or '',
            'zone_id': o.zone_id or '',
            'email': o.email or '',
            'phone': o.phone or '',
            'payment_method': (o.payment_method or '').title(),
            'payment_reference': o.payment_reference or '',
            'amount': float(o.amount or 0),
            'affiliate_code': (o.affiliate.code if o.affiliate else ''),
            'status': o.status,
            'status_label': o.status_label,
            'status_class': o.status_class,
            'created_at': format_ve(o.created_at, '%d/%m/%Y %H:%M'),
            'automation_response': o.automation_response or '',
            'pin_delivered': o.pin_delivered or '',
            'can_send_delivery_proof': order_supports_delivery_proof(o),
        })

    return jsonify({'ok': True, 'orders': payload})


@admin_bp.route('/orders/<int:order_id>')
@login_required
def order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    payment_method_config = PaymentMethod.query.filter_by(code=(order.payment_method or '').strip().lower()).first()
    return render_template(
        'admin/order_detail.html',
        order=order,
        payment_method_config=payment_method_config,
        can_send_delivery_proof=order_supports_delivery_proof(order),
    )


@admin_bp.route('/orders/<int:order_id>/player-id', methods=['POST'])
@login_required
def order_update_player_id(order_id):
    order = Order.query.get_or_404(order_id)
    redirect_target = url_for('admin_bp.order_detail', order_id=order.id)

    if order.status != 'pending':
        flash('Solo puedes editar el ID del jugador en órdenes pendientes.', 'warning')
        return redirect(redirect_target)

    original_player_id = (order.player_id or '').strip()
    updated_player_id = normalize_order_player_id(order, request.form.get('player_id', ''))
    should_reprocess = request.form.get('reprocess') == '1'

    if not updated_player_id:
        flash('Debes ingresar un ID de jugador válido para esta orden.', 'danger')
        return redirect(redirect_target)

    if updated_player_id == original_player_id and not should_reprocess:
        flash('El ID del jugador no cambió.', 'info')
        return redirect(redirect_target)

    try:
        if updated_player_id != original_player_id:
            order.player_id = updated_player_id
            order.player_nickname = None
            order.automation_response = None
            order.updated_at = datetime.utcnow()

            note = f'[Admin] {order.game.player_id_label or "ID del jugador"} actualizado de {original_player_id or "(vacío)"} a {updated_player_id}.'
            existing_notes = order.notes or ''
            if note not in existing_notes:
                order.notes = (existing_notes + '\n' + note).strip()

            identity_meta = extract_customer_identifier_for_game(order.game, player_id=updated_player_id, email=order.email or '')
            identifier_value = (identity_meta.get('identifier') or '').strip()
            if identifier_value:
                scoped_user = get_or_create_scoped_customer(
                    scope_key=identity_meta['scope_key'],
                    scope_label=identity_meta['scope_label'],
                    raw_identifier=identifier_value,
                    account_kind=identity_meta['account_kind'],
                    contact_email=(order.email or '').strip(),
                    phone=(order.phone or '').strip(),
                )
                if scoped_user:
                    order.user_id = scoped_user.id
                    attach_matching_orders_to_customer(
                        scoped_user,
                        order.game.id,
                        identifier_value,
                        identity_meta['account_kind'],
                    )

            db.session.commit()
            flash(f'{order.game.player_id_label or "ID del jugador"} actualizado correctamente.', 'success')

        if should_reprocess:
            result = approve_order(order)
            flash(result['message'], result['category'])

        return redirect(redirect_target)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            'Error al actualizar el ID del jugador de la orden %s',
            getattr(order, 'id', None),
        )
        flash('Ocurrió un error al actualizar el ID del jugador.', 'danger')
        return redirect(redirect_target)


def _run_admin_pabilo_reverification(order, reference=None, force_reference=False):
    try:
        payment_method_config = PaymentMethod.query.filter_by(code=(order.payment_method or '').strip().lower()).first()
        uses_payer_identity = payment_method_uses_payer_identity_verification(payment_method_config) and not force_reference

        reference = str(reference if reference is not None else order.payment_reference or '').strip()
        if not uses_payer_identity and not reference:
            flash('La referencia bancaria es obligatoria.', 'danger')
            return redirect(url_for('admin_bp.order_detail', order_id=order.id))

        previous_reference = str(order.payment_reference or '').strip()
        clear_pabilo_verification_state(order)
        if not uses_payer_identity:
            order.payment_reference = reference
            order.payment_reference_last5 = normalize_reference_last5(reference)
        order.updated_at = datetime.utcnow()

        verification = verify_order_payment(order, force_reference=force_reference)
        order.payment_verification_attempts = int(order.payment_verification_attempts or 0) + 1
        order.payment_last_verification_at = datetime.utcnow()

        if verification.get('verified'):
            stamp_verified_payment(order, verification)
            note = '[Admin] Pago re-verificado manualmente en Pabilo.'
            if uses_payer_identity:
                note = '[Admin] Pago re-verificado manualmente en Pabilo con telefono y cedula del pagador.'
            elif previous_reference and previous_reference != reference:
                note = f'[Admin] Referencia bancaria actualizada de {previous_reference} a {reference} y pago re-verificado en Pabilo.'
            existing_notes = order.notes or ''
            if note not in existing_notes:
                order.notes = (existing_notes + '\n' + note).strip()
            db.session.commit()
            flash(verification.get('message') or 'Pago re-verificado correctamente en Pabilo.', 'success')
            return redirect(url_for('admin_bp.order_detail', order_id=order.id))

        note = verification.get('message') or 'No se pudo re-verificar el pago en Pabilo.'
        audit_note = f'[Admin] {note}'
        existing_notes = order.notes or ''
        if audit_note not in existing_notes:
            order.notes = (existing_notes + '\n' + audit_note).strip()
        db.session.commit()
        flash(note, 'warning' if verification.get('ok') else 'danger')
        return redirect(url_for('admin_bp.order_detail', order_id=order.id))
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            'Error al re-verificar manualmente el pago de la orden %s',
            getattr(order, 'id', None),
        )
        flash('Ocurrió un error interno al re-verificar el pago. Revisa el log del servidor para el detalle.', 'danger')
        return redirect(url_for('admin_bp.order_detail', order_id=order.id))


@admin_bp.route('/orders/<int:order_id>/payment-reference', methods=['POST'])
@login_required
def order_update_payment_reference(order_id):
    order = Order.query.get_or_404(order_id)
    reference = request.form.get('payment_reference', '').strip()
    return _run_admin_pabilo_reverification(order, reference=reference, force_reference=True)


@admin_bp.route('/orders/<int:order_id>/reverify-payment', methods=['POST'])
@login_required
def order_reverify_payment(order_id):
    order = Order.query.get_or_404(order_id)
    return _run_admin_pabilo_reverification(order)


@admin_bp.route('/orders/<int:order_id>/approve', methods=['POST'])
@login_required
def order_approve(order_id):
    order = Order.query.get_or_404(order_id)
    redirect_target = request.referrer or url_for('admin_bp.orders')
    delivery_proof_path = None
    delivery_proof_file = request.files.get('delivery_proof')

    try:
        if delivery_proof_file and delivery_proof_file.filename:
            if not order_supports_delivery_proof(order):
                flash('El comprobante adjunto solo se usa en órdenes manuales sin entrega de PIN.', 'warning')
            elif not allowed_file(delivery_proof_file.filename):
                flash('El comprobante debe ser una imagen PNG, JPG, JPEG, GIF o WEBP.', 'danger')
                return redirect(redirect_target)
            else:
                delivery_proof_path = save_image(delivery_proof_file, 'delivery_proofs')

        result = approve_order(order, delivery_proof_path=delivery_proof_path)
        if delivery_proof_path and getattr(order, 'delivery_proof', None) != delivery_proof_path:
            delete_uploaded_file(delivery_proof_path)
        flash(result['message'], result['category'])
        return redirect(redirect_target)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            'Error al aprobar manualmente la orden %s',
            getattr(order, 'id', None),
        )
        if delivery_proof_path:
            delete_uploaded_file(delivery_proof_path)
        flash('Ocurrió un error interno al aprobar la orden. Revisa el log del servidor para el detalle.', 'danger')
        return redirect(redirect_target)


@admin_bp.route('/orders/<int:order_id>/reject', methods=['POST'])
@login_required
def order_reject(order_id):
    order = Order.query.get_or_404(order_id)
    notes = request.form.get('notes', '').strip()
    order.status = 'rejected'
    order.notes = notes
    order.updated_at = datetime.utcnow()
    db.session.commit()
    try:
        notify_order_rejected(order, order.package, order.game, reason=notes)
    except Exception:
        pass
    flash(f'Orden #{order.order_number} rechazada.', 'warning')
    return redirect(url_for('admin_bp.orders'))


# ─── PINs ────────────────────────────────────────────────────────────────────

STOCK_PINS_SESSION_KEY = 'stock_pins_unlocked_at'
STOCK_PINS_ATTEMPTS_KEY = 'stock_pins_attempts'
STOCK_PINS_MAX_ATTEMPTS = 5
# Endpoints que cuentan como "estar dentro de la zona de códigos". Salir de
# aquí cierra el acceso y hay que volver a poner la clave. Los códigos de
# regalo entran en la misma zona: también son dinero y los protege la misma
# clave, así que moverse entre ambas pantallas no la vuelve a pedir.
STOCK_PINS_ENDPOINTS = {
    'admin_bp.pins',
    'admin_bp.pins_upload',
    'admin_bp.pin_delete',
    'admin_bp.pins_unlock',
    'admin_bp.pins_lock',
    'admin_bp.gift_codes',
    'admin_bp.gift_codes_generate',
    'admin_bp.gift_codes_export',
    'admin_bp.gift_code_toggle',
    'admin_bp.gift_codes_batch_disable',
}


def get_stock_pins_access_code():
    return (current_app.config.get('STOCK_PINS_ACCESS_CODE') or '').strip()


@admin_bp.app_context_processor
def _inject_stock_pins_state():
    """Para pintar el candado en el menú lateral."""
    try:
        return {'stock_pins_protected': bool(get_stock_pins_access_code())}
    except Exception:
        return {'stock_pins_protected': False}


@admin_bp.before_request
def _relock_stock_pins_on_exit():
    """Al salir de la sección, el acceso se cierra solo.

    Basta con irse a Órdenes o al tablero para que Stock PINs vuelva a pedir
    el código: así el acceso no queda abierto el resto de la sesión si quien
    atiende la web se sienta en el mismo equipo.
    """
    if request.endpoint in STOCK_PINS_ENDPOINTS:
        return
    session.pop(STOCK_PINS_SESSION_KEY, None)


def stock_pins_is_unlocked():
    """True si esta sesión ya puso el código y el desbloqueo sigue vigente.

    Sin código configurado la sección queda abierta, para no dejar fuera a
    quien todavía no puso la variable de entorno.
    """
    if not get_stock_pins_access_code():
        return True

    stamp = session.get(STOCK_PINS_SESSION_KEY)
    if not stamp:
        return False
    try:
        unlocked_at = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        session.pop(STOCK_PINS_SESSION_KEY, None)
        return False

    minutes = int(current_app.config.get('STOCK_PINS_UNLOCK_MINUTES') or 30)
    if datetime.utcnow() - unlocked_at > timedelta(minutes=minutes):
        session.pop(STOCK_PINS_SESSION_KEY, None)
        return False
    return True


def stock_pins_required(view):
    """Pide el código antes de dejar entrar a cualquier ruta de Stock PINs.

    Va sobre TODAS las rutas de la sección, no solo sobre la pantalla:
    proteger la vista y dejar abiertas la carga y el borrado no serviría de
    nada, porque se llega a ellas con un POST directo.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        if stock_pins_is_unlocked():
            return view(*args, **kwargs)
        if request.method == 'POST':
            flash('Tu acceso a Stock PINs expiró. Ingresa el código de nuevo.', 'warning')
            return redirect(url_for('admin_bp.pins_unlock'))
        # full_path deja un '?' colgando cuando no hay query string.
        target = request.full_path.rstrip('?')
        return redirect(url_for('admin_bp.pins_unlock', next=target))
    return wrapper


@admin_bp.route('/pins/unlock', methods=['GET', 'POST'])
@login_required
def pins_unlock():
    access_code = get_stock_pins_access_code()
    if not access_code or stock_pins_is_unlocked():
        destino = (request.values.get('next') or '').strip()
        if not destino.startswith(('/admin/pins', '/admin/gift-codes')):
            destino = url_for('admin_bp.pins')
        return redirect(destino)

    next_url = (request.values.get('next') or '').strip()
    # Solo rutas internas de la zona de códigos: un `next` externo
    # convertiría esta pantalla en un redirector abierto.
    if not next_url.startswith(('/admin/pins', '/admin/gift-codes')):
        next_url = url_for('admin_bp.pins')

    if request.method == 'POST':
        attempts = int(session.get(STOCK_PINS_ATTEMPTS_KEY) or 0)
        if attempts >= STOCK_PINS_MAX_ATTEMPTS:
            flash('Demasiados intentos fallidos. Cierra sesión y vuelve a entrar.', 'danger')
            return render_template('admin/pins_unlock.html', next_url=next_url, locked_out=True)

        submitted = (request.form.get('access_code') or '').strip()
        if hmac.compare_digest(submitted, access_code):
            session[STOCK_PINS_SESSION_KEY] = datetime.utcnow().isoformat()
            session.pop(STOCK_PINS_ATTEMPTS_KEY, None)
            return redirect(next_url)

        session[STOCK_PINS_ATTEMPTS_KEY] = attempts + 1
        restantes = STOCK_PINS_MAX_ATTEMPTS - (attempts + 1)
        flash(f'Código incorrecto. Te quedan {max(restantes, 0)} intentos.', 'danger')

    return render_template('admin/pins_unlock.html', next_url=next_url, locked_out=False)


@admin_bp.route('/pins/lock', methods=['POST'])
@login_required
def pins_lock():
    """Cierra el acceso a mano, sin esperar a que caduque."""
    session.pop(STOCK_PINS_SESSION_KEY, None)
    flash('Stock PINs quedó bloqueado.', 'info')
    return redirect(url_for('admin_bp.dashboard'))


@admin_bp.route('/pins')
@login_required
@stock_pins_required
def pins():
    package_id = request.args.get('package_id', type=int)

    pin_enabled_query = (
        Package.query
        .join(Game)
        .join(Category)
        .filter(Package.is_active == True)
        .filter(
            or_(
                Package.is_automated.is_(True),
                Category.slug == 'tarjetas'
            )
        )
        .order_by(Game.name, Package.sort_order)
    )

    pin_enabled_packages = pin_enabled_query.all()
    selected_package = None
    pins_list = []

    if package_id:
        selected_package = pin_enabled_query.filter(Package.id == package_id).first()
        if selected_package:
            pins_list = (
                Pin.query
                .filter_by(package_id=package_id)
                .order_by(Pin.is_used.asc(), Pin.created_at.asc())
                .all()
            )

    # Antes salían todos los paquetes revueltos en una sola rejilla: Apple,
    # Free Fire y Roblox juntos. Ahora se elige primero el juego o servicio y
    # solo entonces aparecen sus paquetes.
    games_map = {}
    for pkg in pin_enabled_packages:
        entry = games_map.setdefault(pkg.game_id, {
            'game': pkg.game,
            'packages': [],
            'available': 0,
        })
        entry['packages'].append(pkg)
        entry['available'] += int(pkg.pin_count or 0)
    pin_games = sorted(games_map.values(), key=lambda item: (item['game'].name or '').lower())

    game_id = request.args.get('game_id', type=int)
    if selected_package:
        game_id = selected_package.game_id
    selected_game_entry = games_map.get(game_id) if game_id else None

    return render_template(
        'admin/pins.html',
        automated_packages=pin_enabled_packages,
        pin_games=pin_games,
        selected_game=selected_game_entry,
        selected_package=selected_package,
        pins_list=pins_list,
    )


@admin_bp.route('/pins/<int:package_id>/upload', methods=['POST'])
@login_required
@stock_pins_required
def pins_upload(package_id):
    package = Package.query.get_or_404(package_id)
    raw = request.form.get('pins_text', '').strip()
    if not raw:
        flash('No se ingresaron PINs.', 'warning')
        return redirect(url_for('admin_bp.pins', package_id=package_id))

    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    added = 0
    for line in lines:
        pin = Pin(package_id=package.id, code=line)
        db.session.add(pin)
        added += 1

    db.session.commit()
    flash(f'{added} PINs cargados para "{package.name}".', 'success')
    return redirect(url_for('admin_bp.pins', package_id=package_id))


@admin_bp.route('/pins/<int:pin_id>/delete', methods=['POST'])
@login_required
@stock_pins_required
def pin_delete(pin_id):
    pin = Pin.query.get_or_404(pin_id)
    package_id = pin.package_id
    if pin.is_used:
        flash('No se puede eliminar un PIN ya utilizado.', 'danger')
    else:
        db.session.delete(pin)
        db.session.commit()
        flash('PIN eliminado.', 'warning')
    return redirect(url_for('admin_bp.pins', package_id=package_id))


# ─── Códigos de regalo ───────────────────────────────────────────────────────

@admin_bp.route('/gift-codes')
@login_required
@stock_pins_required
def gift_codes():
    """Los códigos son dinero: viven detrás del mismo candado que el stock."""
    batch_filter = (request.args.get('batch') or '').strip()
    status_filter = (request.args.get('status') or '').strip()
    game_id = request.args.get('game_id', type=int)

    query = GiftCode.query.order_by(GiftCode.created_at.desc(), GiftCode.id.desc())
    if batch_filter:
        query = query.filter(GiftCode.batch == batch_filter)
    if status_filter == 'used':
        query = query.filter(GiftCode.is_used.is_(True))
    elif status_filter == 'available':
        query = query.filter(GiftCode.is_used.is_(False), GiftCode.is_active.is_(True))
    elif status_filter == 'disabled':
        query = query.filter(GiftCode.is_active.is_(False))
    if game_id:
        query = query.join(Package).filter(Package.game_id == game_id)

    codes = query.limit(500).all()

    total = GiftCode.query.count()
    usados = GiftCode.query.filter(GiftCode.is_used.is_(True)).count()
    disponibles = GiftCode.query.filter(
        GiftCode.is_used.is_(False), GiftCode.is_active.is_(True)
    ).count()

    lotes = [
        row[0] for row in
        db.session.query(GiftCode.batch)
        .filter(GiftCode.batch.isnot(None))
        .distinct().order_by(GiftCode.batch.asc()).all()
        if row[0]
    ]

    games = (
        Game.query.filter_by(is_active=True)
        .order_by(Game.name.asc()).all()
    )
    # Cada juego con sus paquetes: el paquete ES la cantidad de recarga que
    # va a entregar el código, así que se elige juego y después monto.
    packages_by_game = {}
    for pkg in (
        Package.query.filter_by(is_active=True)
        .order_by(Package.game_id.asc(), Package.sort_order.asc(), Package.name.asc())
        .all()
    ):
        packages_by_game.setdefault(str(pkg.game_id), []).append({
            'id': pkg.id,
            'name': pkg.name,
            'price': str(pkg.price),
        })

    return render_template(
        'admin/gift_codes.html',
        codes=codes,
        games=games,
        packages_by_game=packages_by_game,
        batches=lotes,
        batch_filter=batch_filter,
        status_filter=status_filter,
        game_id=game_id,
        total_codes=total,
        used_codes=usados,
        available_codes=disponibles,
        format_code=format_gift_code,
    )


@admin_bp.route('/gift-codes/generate', methods=['POST'])
@login_required
@stock_pins_required
def gift_codes_generate():
    package_id = request.form.get('package_id', type=int)
    quantity = request.form.get('quantity', type=int)
    batch = (request.form.get('batch') or '').strip()
    source = (request.form.get('source') or '').strip()
    expires_raw = (request.form.get('expires_at') or '').strip()

    expires_at = None
    if expires_raw:
        try:
            expires_at = datetime.strptime(expires_raw, '%Y-%m-%d').replace(
                hour=23, minute=59, second=59
            )
        except ValueError:
            flash('La fecha de vencimiento no es válida.', 'danger')
            return redirect(url_for('admin_bp.gift_codes'))

    try:
        creados = create_gift_batch(
            package_id, quantity, batch=batch, source=source, expires_at=expires_at
        )
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('admin_bp.gift_codes'))

    package = Package.query.get(package_id)
    nombre = f'{package.game.name} / {package.name}' if package and package.game else 'el paquete'
    flash(f'{len(creados)} códigos generados para {nombre}.', 'success')
    return redirect(url_for('admin_bp.gift_codes', batch=batch or None))


@admin_bp.route('/gift-codes/export')
@login_required
@stock_pins_required
def gift_codes_export():
    """Descarga los códigos sin usar como texto plano, listos para repartir."""
    batch_filter = (request.args.get('batch') or '').strip()

    query = GiftCode.query.filter(
        GiftCode.is_used.is_(False), GiftCode.is_active.is_(True)
    )
    if batch_filter:
        query = query.filter(GiftCode.batch == batch_filter)
    codes = query.order_by(GiftCode.created_at.asc()).all()

    cuerpo = '\n'.join(format_gift_code(c.code) for c in codes)
    nombre = f'codigos-{batch_filter or "todos"}.txt'.replace(' ', '-')
    return Response(
        cuerpo,
        mimetype='text/plain; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{nombre}"'},
    )


@admin_bp.route('/gift-codes/<int:code_id>/toggle', methods=['POST'])
@login_required
@stock_pins_required
def gift_code_toggle(code_id):
    gift = GiftCode.query.get_or_404(code_id)
    if gift.is_used:
        flash('Ese código ya fue canjeado, no se puede reactivar ni desactivar.', 'warning')
    else:
        gift.is_active = not bool(gift.is_active)
        db.session.commit()
        flash(
            'Código reactivado.' if gift.is_active else 'Código desactivado.',
            'success' if gift.is_active else 'warning',
        )
    return redirect(request.referrer or url_for('admin_bp.gift_codes'))


@admin_bp.route('/gift-codes/batch-disable', methods=['POST'])
@login_required
@stock_pins_required
def gift_codes_batch_disable():
    """Apaga un lote completo, para cuando un video se filtra o se cancela
    una campaña. Solo toca los que nadie canjeó todavía."""
    batch = (request.form.get('batch') or '').strip()
    if not batch:
        flash('Elige un lote.', 'danger')
        return redirect(url_for('admin_bp.gift_codes'))

    afectados = (
        GiftCode.query
        .filter(GiftCode.batch == batch, GiftCode.is_used.is_(False))
        .update({'is_active': False}, synchronize_session=False)
    )
    db.session.commit()
    flash(f'{afectados} códigos del lote "{batch}" quedaron desactivados.', 'warning')
    return redirect(url_for('admin_bp.gift_codes', batch=batch))

# ─── Affiliates ──────────────────────────────────────────────────────────────

@admin_bp.route('/affiliates')
@login_required
def affiliates():
    # Los minis (auto-registrados en /minis) se gestionan aparte en
    # /admin/minis: no listarlos aquí evita que el botón "Pagar" de esta
    # pantalla (que pone balance=0 directo) le pise el flujo de retiro.
    all_affiliates = (
        Affiliate.query
        .filter(or_(Affiliate.is_mini.is_(False), Affiliate.is_mini.is_(None)))
        .order_by(Affiliate.created_at.desc())
        .all()
    )
    discount_codes = Discount.query.order_by(Discount.created_at.desc()).all()
    return render_template(
        'admin/affiliates.html',
        affiliates=all_affiliates,
        discount_codes=discount_codes,
        discount_kind_label=_discount_kind_label,
        discount_value_label=_discount_value_label,
    )


@admin_bp.route('/affiliates/add', methods=['POST'])
@login_required
def affiliate_add():
    name = request.form.get('name', '').strip()
    code = request.form.get('code', '').strip().upper()
    email = request.form.get('email', '').strip()
    commission_rate = float(request.form.get('commission_rate', 1.0))
    client_discount_rate = float(request.form.get('client_discount_rate', 2.0))

    if not name or not code:
        flash('Nombre y código son obligatorios.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if Affiliate.query.filter_by(code=code).first():
        flash('Ese código ya existe.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    aff = Affiliate(
        name=name, code=code, email=email, commission_rate=commission_rate,
        client_discount_rate=client_discount_rate,
        one_per_player=bool(request.form.get('one_per_player')),
    )
    db.session.add(aff)
    db.session.commit()
    flash(f'Afiliado "{name}" creado con código {code}.', 'success')
    return redirect(url_for('admin_bp.affiliates'))


@admin_bp.route('/affiliates/<int:aff_id>/edit', methods=['POST'])
@login_required
def affiliate_edit(aff_id):
    aff = Affiliate.query.get_or_404(aff_id)
    aff.name = request.form.get('name', aff.name).strip()
    aff.email = request.form.get('email', aff.email or '').strip()
    aff.commission_rate = float(request.form.get('commission_rate', aff.commission_rate))
    aff.client_discount_rate = float(request.form.get('client_discount_rate', aff.client_discount_rate or 0))
    aff.one_per_player = bool(request.form.get('one_per_player'))
    aff.is_active = bool(request.form.get('is_active'))
    db.session.commit()
    flash('Afiliado actualizado.', 'success')
    return redirect(url_for('admin_bp.affiliates'))


@admin_bp.route('/affiliates/<int:aff_id>/pay', methods=['POST'])
@login_required
def affiliate_pay(aff_id):
    aff = Affiliate.query.get_or_404(aff_id)
    unpaid = AffiliateCommission.query.filter_by(affiliate_id=aff_id, is_paid=False).all()
    for c in unpaid:
        c.is_paid = True
    aff.balance = 0
    db.session.commit()
    flash(f'Comisiones de {aff.name} marcadas como pagadas.', 'success')
    return redirect(url_for('admin_bp.affiliates'))


@admin_bp.route('/affiliates/<int:aff_id>/balance', methods=['POST'])
@login_required
def affiliate_update_balance(aff_id):
    aff = Affiliate.query.get_or_404(aff_id)
    raw_balance = (request.form.get('balance') or '').strip()

    try:
        new_balance = round(float(raw_balance), 2)
    except Exception:
        flash('Monto inválido. Debe ser un número.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if new_balance < 0:
        flash('El monto no puede ser negativo.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    aff.balance = new_balance
    db.session.commit()
    flash(f'Monto actualizado para {aff.name}: ${new_balance:.2f}', 'success')
    return redirect(url_for('admin_bp.affiliates'))


# ─── Mini influencers (/minis) ───────────────────────────────────────────────

@admin_bp.route('/minis')
@login_required
def minis():
    section = request.args.get('section', 'solicitudes')

    pending_applications = (
        Affiliate.query.filter_by(is_mini=True, status='pending')
        .order_by(Affiliate.created_at.desc()).all()
    )
    reviewed_applications = (
        Affiliate.query.filter(Affiliate.is_mini.is_(True), Affiliate.status.in_(['approved', 'rejected']))
        .order_by(Affiliate.created_at.desc()).limit(100).all()
    )

    pending_videos = MiniVideo.query.filter_by(status='pending').order_by(MiniVideo.created_at.desc()).all()
    reviewed_videos = (
        MiniVideo.query.filter(MiniVideo.status.in_(['approved', 'rejected']))
        .order_by(MiniVideo.created_at.desc()).limit(100).all()
    )

    pending_withdrawals = AffiliateWithdrawal.query.filter_by(status='pending').order_by(AffiliateWithdrawal.created_at.desc()).all()
    reviewed_withdrawals = (
        AffiliateWithdrawal.query.filter(AffiliateWithdrawal.status.in_(['approved', 'rejected']))
        .order_by(AffiliateWithdrawal.created_at.desc()).limit(100).all()
    )

    approved_minis = Affiliate.query.filter_by(is_mini=True, status='approved').order_by(Affiliate.name.asc()).all()
    rank_progress_by_id = {a.id: get_rank_progress(a) for a in approved_minis}
    ranks = MiniRank.query.order_by(MiniRank.sort_order.asc(), MiniRank.uses_required.asc()).all()

    return render_template(
        'admin/minis.html',
        section=section,
        pending_applications=pending_applications,
        reviewed_applications=reviewed_applications,
        pending_videos=pending_videos,
        reviewed_videos=reviewed_videos,
        pending_withdrawals=pending_withdrawals,
        reviewed_withdrawals=reviewed_withdrawals,
        approved_minis=approved_minis,
        rank_progress_by_id=rank_progress_by_id,
        ranks=ranks,
        suggested_reward_for_views=suggested_reward_for_views,
    )


@admin_bp.route('/minis/<int:aff_id>/approve', methods=['POST'])
@login_required
def mini_approve(aff_id):
    aff = Affiliate.query.get_or_404(aff_id)
    if not aff.is_mini:
        flash('Ese afiliado no es un mini influencer.', 'danger')
        return redirect(url_for('admin_bp.minis'))
    if aff.status == 'approved':
        flash('Esa solicitud ya estaba aprobada.', 'warning')
        return redirect(url_for('admin_bp.minis'))

    code = (request.form.get('code') or '').strip().upper()
    if not code:
        flash('El código es obligatorio.', 'danger')
        return redirect(url_for('admin_bp.minis'))
    if Affiliate.query.filter(Affiliate.code == code, Affiliate.id != aff.id).first():
        flash('Ese código ya está en uso por otro afiliado.', 'danger')
        return redirect(url_for('admin_bp.minis'))

    try:
        client_discount_rate = float(request.form.get('client_discount_rate', 2.0))
        commission_rate = float(request.form.get('commission_rate', 1.0))
    except ValueError:
        flash('Los porcentajes deben ser números.', 'danger')
        return redirect(url_for('admin_bp.minis'))

    aff.code = code
    aff.client_discount_rate = client_discount_rate
    aff.commission_rate = commission_rate
    aff.status = 'approved'
    aff.is_active = True
    aff.rejection_reason = None
    aff.reviewed_at = datetime.utcnow()
    db.session.commit()
    flash(f'Mini influencer "{aff.name}" aprobado con código {code}.', 'success')
    return redirect(url_for('admin_bp.minis'))


@admin_bp.route('/minis/<int:aff_id>/reject', methods=['POST'])
@login_required
def mini_reject(aff_id):
    aff = Affiliate.query.get_or_404(aff_id)
    if not aff.is_mini:
        flash('Ese afiliado no es un mini influencer.', 'danger')
        return redirect(url_for('admin_bp.minis'))
    if aff.status != 'pending':
        flash('Esa solicitud ya fue procesada.', 'warning')
        return redirect(url_for('admin_bp.minis'))

    aff.status = 'rejected'
    aff.is_active = False
    aff.rejection_reason = (request.form.get('rejection_reason') or '').strip() or None
    aff.reviewed_at = datetime.utcnow()
    db.session.commit()
    flash(f'Solicitud de "{aff.name}" rechazada.', 'success')
    return redirect(url_for('admin_bp.minis'))


@admin_bp.route('/minis/videos/<int:video_id>/review', methods=['POST'])
@login_required
def mini_video_review(video_id):
    video = MiniVideo.query.get_or_404(video_id)
    ok, error = review_mini_video(
        video,
        (request.form.get('action') or '').strip(),
        reward_amount=request.form.get('reward_amount'),
        note=request.form.get('note') or '',
    )
    flash(error if not ok else 'Video revisado.', 'danger' if not ok else 'success')
    return redirect(url_for('admin_bp.minis', section='videos'))


@admin_bp.route('/minis/<int:aff_id>/rank-award', methods=['POST'])
@login_required
def mini_rank_award(aff_id):
    aff = Affiliate.query.get_or_404(aff_id)
    if not aff.is_mini:
        flash('Ese afiliado no es un mini influencer.', 'danger')
        return redirect(url_for('admin_bp.minis'))

    rank_name = (request.form.get('rank_name') or '').strip()
    amount_raw = request.form.get('bonus_amount')
    try:
        amount = float(amount_raw) if amount_raw not in (None, '') else None
    except ValueError:
        flash('Monto inválido.', 'danger')
        return redirect(url_for('admin_bp.minis'))

    ok, error = award_rank_bonus(aff, rank_name, amount)
    flash(error if not ok else f'Bono de rango "{rank_name}" pagado.', 'danger' if not ok else 'success')
    return redirect(url_for('admin_bp.minis'))


@admin_bp.route('/minis/retiros/<int:w_id>/approve', methods=['POST'])
@login_required
def mini_withdrawal_approve(w_id):
    withdrawal = AffiliateWithdrawal.query.get_or_404(w_id)
    ok, error = approve_mini_withdrawal(withdrawal)
    flash(error if not ok else 'Retiro aprobado.', 'danger' if not ok else 'success')
    return redirect(url_for('admin_bp.minis', section='retiros'))


@admin_bp.route('/minis/retiros/<int:w_id>/reject', methods=['POST'])
@login_required
def mini_withdrawal_reject(w_id):
    withdrawal = AffiliateWithdrawal.query.get_or_404(w_id)
    ok, error = reject_mini_withdrawal(withdrawal, request.form.get('rejection_reason') or '')
    flash(error if not ok else 'Retiro rechazado.', 'danger' if not ok else 'success')
    return redirect(url_for('admin_bp.minis', section='retiros'))


@admin_bp.app_context_processor
def _inject_minis_pending_state():
    """Para el badge de pendientes en el menú lateral, junto al link de Minis."""
    try:
        return {
            'minis_pending_count': (
                Affiliate.query.filter_by(is_mini=True, status='pending').count()
                + MiniVideo.query.filter_by(status='pending').count()
                + AffiliateWithdrawal.query.filter_by(status='pending').count()
            )
        }
    except Exception:
        return {'minis_pending_count': 0}


@admin_bp.route('/discount-codes/add', methods=['POST'])
@login_required
def discount_code_add():
    code = (request.form.get('code') or '').strip().upper()
    description = (request.form.get('description') or '').strip()
    discount_type = (request.form.get('discount_type') or 'percentage').strip().lower()

    try:
        discount_value = float((request.form.get('discount_value') or '').strip())
        usage_limit = _parse_optional_int(request.form.get('usage_limit'))
        min_amount = _parse_optional_decimal(request.form.get('min_amount'))
        max_discount = _parse_optional_decimal(request.form.get('max_discount'))
        expires_at = _parse_optional_datetime(request.form.get('expires_at'))
    except ValueError:
        flash('Datos inválidos para el código de descuento.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if not code:
        flash('El código es obligatorio.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if discount_type not in {'percentage', 'fixed'}:
        flash('Tipo de descuento inválido.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if discount_value <= 0:
        flash('El valor del descuento debe ser mayor a 0.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if discount_type == 'percentage' and discount_value > 100:
        flash('Un descuento porcentual no puede ser mayor a 100%.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if max_discount is not None and max_discount <= 0:
        flash('El tope máximo debe ser mayor a 0.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if min_amount is not None and min_amount < 0:
        flash('El monto mínimo no puede ser negativo.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if discount_type == 'fixed':
        max_discount = None

    if usage_limit is not None and usage_limit < 1:
        flash('El límite de usos debe ser mayor o igual a 1.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if Discount.query.filter_by(code=code).first():
        flash('Ese código de descuento ya existe.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    discount = Discount(
        code=code,
        description=description or None,
        discount_type=discount_type,
        discount_value=discount_value,
        min_amount=min_amount,
        max_discount=max_discount,
        usage_limit=usage_limit,
        one_per_player=bool(request.form.get('one_per_player')),
        is_active=bool(request.form.get('is_active')),
        expires_at=expires_at,
    )
    db.session.add(discount)
    db.session.commit()
    flash(f'Código de descuento {code} creado.', 'success')
    return redirect(url_for('admin_bp.affiliates'))


@admin_bp.route('/discount-codes/<int:discount_id>/edit', methods=['POST'])
@login_required
def discount_code_edit(discount_id):
    discount = Discount.query.get_or_404(discount_id)
    code = (request.form.get('code') or discount.code).strip().upper()
    description = (request.form.get('description') or '').strip()
    discount_type = (request.form.get('discount_type') or discount.discount_type).strip().lower()
    discount_value_raw = (request.form.get('discount_value') or '').strip()

    try:
        discount_value = float(discount_value_raw or float(discount.discount_value or 0))
        usage_limit = _parse_optional_int(request.form.get('usage_limit'))
        min_amount = _parse_optional_decimal(request.form.get('min_amount'))
        max_discount = _parse_optional_decimal(request.form.get('max_discount'))
        expires_at = _parse_optional_datetime(request.form.get('expires_at'))
    except ValueError:
        flash('Datos inválidos para el código de descuento.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if not code:
        flash('El código es obligatorio.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if discount_type not in {'percentage', 'fixed'}:
        flash('Tipo de descuento inválido.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if discount_value <= 0:
        flash('El valor del descuento debe ser mayor a 0.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if discount_type == 'percentage' and discount_value > 100:
        flash('Un descuento porcentual no puede ser mayor a 100%.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if max_discount is not None and max_discount <= 0:
        flash('El tope máximo debe ser mayor a 0.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if min_amount is not None and min_amount < 0:
        flash('El monto mínimo no puede ser negativo.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if discount_type == 'fixed':
        max_discount = None

    if usage_limit is not None and usage_limit < 1:
        flash('El límite de usos debe ser mayor o igual a 1.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    duplicate = Discount.query.filter(Discount.code == code, Discount.id != discount.id).first()
    if duplicate:
        flash('Ya existe otro código de descuento con ese valor.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    discount.code = code
    discount.description = description or None
    discount.discount_type = discount_type
    discount.discount_value = discount_value
    discount.usage_limit = usage_limit
    discount.min_amount = min_amount
    discount.max_discount = max_discount
    discount.expires_at = expires_at
    discount.one_per_player = bool(request.form.get('one_per_player'))
    discount.is_active = bool(request.form.get('is_active'))
    db.session.commit()
    flash(f'Código de descuento {code} actualizado.', 'success')
    return redirect(url_for('admin_bp.affiliates'))


# ─── Payment Methods ─────────────────────────────────────────────────────────

@admin_bp.route('/payment-methods')
@login_required
def payment_methods():
    methods = PaymentMethod.query.order_by(PaymentMethod.sort_order, PaymentMethod.name).all()
    return render_template('admin/payment_methods.html', methods=methods)


@admin_bp.route('/payment-methods/add', methods=['POST'])
@login_required
def payment_method_add():
    code = request.form.get('code', '').strip().lower()
    name = request.form.get('name', '').strip()
    sort_order = int(request.form.get('sort_order', 100))
    contact_email = request.form.get('contact_email', '').strip() or None
    pay_id = request.form.get('pay_id', '').strip() or None
    contact_phone = request.form.get('contact_phone', '').strip() or None
    bank_name = request.form.get('bank_name', '').strip() or None
    id_number = request.form.get('id_number', '').strip() or None
    account_currency = (request.form.get('account_currency', 'bs') or 'bs').strip().lower()
    pabilo_user_bank_id = request.form.get('pabilo_user_bank_id', '').strip() or None
    pabilo_requires_phone_dni = bool(request.form.get('pabilo_requires_phone_dni'))
    show_contact_email = bool(request.form.get('show_contact_email'))
    show_pay_id = bool(request.form.get('show_pay_id'))
    show_contact_phone = bool(request.form.get('show_contact_phone'))

    if not code or not name:
        flash('Código y nombre son obligatorios.', 'danger')
        return redirect(url_for('admin_bp.payment_methods'))

    if PaymentMethod.query.filter_by(code=code).first():
        flash('Ya existe un método con ese código.', 'danger')
        return redirect(url_for('admin_bp.payment_methods'))

    logo = save_image(request.files.get('logo'), 'payments')
    tutorial_video = save_video(request.files.get('tutorial_video'), 'payments')
    uses_rate = bool(request.form.get('uses_rate'))
    method = PaymentMethod(
        code=code,
        name=name,
        sort_order=sort_order,
        logo=logo,
        uses_rate=uses_rate,
        contact_email=contact_email,
        pay_id=pay_id,
        contact_phone=contact_phone,
        bank_name=bank_name,
        id_number=id_number,
        account_currency=account_currency,
        pabilo_user_bank_id=pabilo_user_bank_id,
        pabilo_requires_phone_dni=pabilo_requires_phone_dni,
        show_contact_email=show_contact_email,
        show_pay_id=show_pay_id,
        show_contact_phone=show_contact_phone,
        tutorial_video=tutorial_video,
    )
    db.session.add(method)
    db.session.commit()
    flash('Método de pago creado.', 'success')
    return redirect(url_for('admin_bp.payment_methods'))


@admin_bp.route('/payment-methods/<int:method_id>/edit', methods=['POST'])
@login_required
def payment_method_edit(method_id):
    method = PaymentMethod.query.get_or_404(method_id)
    method.code = request.form.get('code', method.code).strip().lower()
    method.name = request.form.get('name', method.name).strip()
    method.sort_order = int(request.form.get('sort_order', method.sort_order))
    method.is_active = bool(request.form.get('is_active'))
    method.uses_rate = bool(request.form.get('uses_rate'))
    method.contact_email = request.form.get('contact_email', '').strip() or None
    method.pay_id = request.form.get('pay_id', '').strip() or None
    method.contact_phone = request.form.get('contact_phone', '').strip() or None
    method.bank_name = request.form.get('bank_name', '').strip() or None
    method.id_number = request.form.get('id_number', '').strip() or None
    method.account_currency = (request.form.get('account_currency', method.account_currency or 'bs') or 'bs').strip().lower()
    method.pabilo_user_bank_id = request.form.get('pabilo_user_bank_id', '').strip() or None
    method.pabilo_requires_phone_dni = bool(request.form.get('pabilo_requires_phone_dni'))
    method.show_contact_email = bool(request.form.get('show_contact_email'))
    method.show_pay_id = bool(request.form.get('show_pay_id'))
    method.show_contact_phone = bool(request.form.get('show_contact_phone'))

    new_logo = save_image(request.files.get('logo'), 'payments')
    if new_logo:
        delete_uploaded_file(method.logo)
        method.logo = new_logo

    if request.form.get('remove_tutorial_video'):
        if method.tutorial_video:
            delete_uploaded_file(method.tutorial_video)
        method.tutorial_video = None

    new_tutorial_video = save_video(request.files.get('tutorial_video'), 'payments')
    if new_tutorial_video:
        if method.tutorial_video:
            delete_uploaded_file(method.tutorial_video)
        method.tutorial_video = new_tutorial_video

    db.session.commit()
    flash('Método de pago actualizado.', 'success')
    return redirect(url_for('admin_bp.payment_methods'))


@admin_bp.route('/payment-methods/<int:method_id>/delete', methods=['POST'])
@login_required
def payment_method_delete(method_id):
    method = PaymentMethod.query.get_or_404(method_id)
    method.is_active = False
    db.session.commit()
    flash('Método de pago desactivado.', 'warning')
    return redirect(url_for('admin_bp.payment_methods'))


# ─── Settings ────────────────────────────────────────────────────────────────

def _build_admin_rankings_payload():
    from .main import (
        RANKING_DEFS,
        _get_archive_history_payload,
        _get_previous_archive_payload,
        _get_ranking_entries,
        _is_ranking_enabled,
        _resolve_ranking_game,
    )

    month_label = now_ve().strftime('%m/%Y')
    rankings = []

    for ranking_key, config in RANKING_DEFS.items():
        game = _resolve_ranking_game(config)
        enabled = bool(game and _is_ranking_enabled(config, game))
        rewards = config.get('rewards') or []
        item = {
            'key': ranking_key,
            'label': config.get('label') or ranking_key,
            'units_label': config.get('units_label') or 'Unidades',
            'month_label': month_label,
            'enabled': enabled,
            'game_id': game.id if game else None,
            'game_name': game.name if game else (config.get('label') or ranking_key),
            'leaders': [],
            'total_players': 0,
            'archive_history': _get_archive_history_payload(ranking_key, include_private=True),
            'previous_winners': _get_previous_archive_payload(ranking_key),
        }

        if enabled:
            entries = _get_ranking_entries(game.id)
            item['total_players'] = len(entries)
            for index, entry in enumerate(entries[:10], start=1):
                reward_value = rewards[index - 1] if index - 1 < len(rewards) else None
                item['leaders'].append({
                    'position': index,
                    'player_id': (entry.get('player_id') or '').strip() or '----',
                    'nickname': (entry.get('nickname') or '').strip() or 'Sin nickname',
                    'total_units': int(entry.get('total_units') or 0),
                    'total_spent': round(float(entry.get('total_spent') or 0), 2),
                    'reward_value': reward_value,
                    'prize_label': str(reward_value) if reward_value is not None else 'Sin premio',
                    'is_prize_eligible': index <= len(rewards),
                })

        rankings.append(item)

    return rankings


@admin_bp.route('/rankings')
@login_required
def rankings():
    return render_template('admin/rankings.html')


@admin_bp.route('/rankings/live')
@login_required
def rankings_live():
    from .main import backfill_ranking_archives_if_needed

    backfill_ranking_archives_if_needed()
    return jsonify({
        'ok': True,
        'generated_at': format_ve(now_ve(), '%d/%m/%Y %H:%M:%S'),
        'rankings': _build_admin_rankings_payload(),
    })

@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    usd_rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
    usd_rate = usd_rate_setting.value if usd_rate_setting else ''
    default_pkg_setting = Setting.query.filter_by(key='default_auto_package_id').first()
    default_auto_package_id = default_pkg_setting.value if default_pkg_setting else ''
    site_logo_setting = Setting.query.filter_by(key='site_logo').first()
    site_logo_value = site_logo_setting.value if site_logo_setting else ''
    site_background_setting = Setting.query.filter_by(key='site_background_image').first()
    site_background_value = site_background_setting.value if site_background_setting else ''
    order_status_image_setting = Setting.query.filter_by(key='order_status_image').first()
    order_status_image_value = order_status_image_setting.value if order_status_image_setting else ''
    promo_banner_image_setting = Setting.query.filter_by(key='promo_banner_image').first()
    promo_banner_image_value = promo_banner_image_setting.value if promo_banner_image_setting else ''
    promo_banner_link_setting = Setting.query.filter_by(key='promo_banner_link').first()
    promo_banner_link_value = promo_banner_link_setting.value if promo_banner_link_setting else ''
    checkout_payment_video_method_setting = Setting.query.filter_by(key='checkout_payment_video_method').first()
    checkout_payment_video_method_value = checkout_payment_video_method_setting.value if checkout_payment_video_method_setting else ''
    checkout_payment_video_title_setting = Setting.query.filter_by(key='checkout_payment_video_title').first()
    checkout_payment_video_title_value = checkout_payment_video_title_setting.value if checkout_payment_video_title_setting else ''
    checkout_payment_video_message_setting = Setting.query.filter_by(key='checkout_payment_video_message').first()
    checkout_payment_video_message_value = checkout_payment_video_message_setting.value if checkout_payment_video_message_setting else ''
    checkout_payment_video_cta_setting = Setting.query.filter_by(key='checkout_payment_video_cta').first()
    checkout_payment_video_cta_value = checkout_payment_video_cta_setting.value if checkout_payment_video_cta_setting else ''
    checkout_payment_video_file_setting = Setting.query.filter_by(key='checkout_payment_video_file').first()
    checkout_payment_video_file_value = checkout_payment_video_file_setting.value if checkout_payment_video_file_setting else ''

    social_keys = {
        'social_facebook': 'URL de Facebook',
        'social_instagram': 'URL o usuario de Instagram',
        'social_tiktok': 'URL o usuario de TikTok',
        'social_whatsapp': 'Link directo de WhatsApp',
    }
    social_settings = {}
    for key in social_keys:
        setting = Setting.query.filter_by(key=key).first()
        social_settings[key] = setting.value if setting else ''

    email_keys = {
        'email_brand_name': 'Nombre de la marca para correos',
        'support_email': 'Correo de soporte',
        'support_whatsapp': 'Link directo a WhatsApp soporte',
        'support_schedule': 'Horario visible en el footer',
        'support_location': 'Ubicación visible en el footer',
        'support_site_url': 'URL del sitio o centro de ayuda',
        'privacy_url': 'URL de política de privacidad',
        'unsubscribe_url': 'URL para darse de baja',
        'admin_notify_email': 'Correo para alertas de nuevas órdenes',
    }
    payment_verify_keys = {
        'auto_verify_payments': 'Habilita la verificación automática de pagos con Pabilo',
        'pabilo_api_key': 'API key privada de Pabilo para validar pagos',
    }
    binance_auto_keys = {
        'binance_auto_enabled': 'Activa verificación automática de Binance Pay',
        'binance_wallet_address': 'Dirección/email de Binance Pay que se muestra al cliente',
    }
    ranking_keys = {
        'ranking_free_fire_enabled': 'Mostrar ranking mensual de Free Fire',
        'ranking_blood_strike_enabled': 'Mostrar ranking mensual de Blood Strike',
        'ranking_free_fire_game_id': 'Juego asociado al ranking de Free Fire',
        'ranking_blood_strike_game_id': 'Juego asociado al ranking de Blood Strike',
    }
    community_popup_keys = {
        'community_popup_enabled': 'Activa el popup recurrente de "Únete a nuestra comunidad"',
        'community_popup_interval_hours': 'Cada cuántas horas puede reaparecer (por pestaña/visita)',
        'community_popup_whatsapp_url': 'Link del canal de WhatsApp al que invita el popup',
    }
    manual_schedule_keys = {
        'manual_open_hour': 'Hora (0-23, Venezuela) en que abren los paquetes de recarga manual',
        'manual_close_hour': 'Hora (0-23, Venezuela) en que cierran los paquetes de recarga manual',
    }
    email_settings = {}
    for key in email_keys:
        setting = Setting.query.filter_by(key=key).first()
        email_settings[key] = setting.value if setting else ''

    payment_verify_settings = {}
    for key in payment_verify_keys:
        setting = Setting.query.filter_by(key=key).first()
        payment_verify_settings[key] = setting.value if setting else ''

    binance_auto_settings = {}
    for key in binance_auto_keys:
        setting = Setting.query.filter_by(key=key).first()
        binance_auto_settings[key] = setting.value if setting else ''

    ranking_settings = {}
    for key in ranking_keys:
        setting = Setting.query.filter_by(key=key).first()
        ranking_settings[key] = setting.value if setting else ''

    community_popup_settings = {}
    for key in community_popup_keys:
        setting = Setting.query.filter_by(key=key).first()
        community_popup_settings[key] = setting.value if setting else ''
    if not community_popup_settings.get('community_popup_interval_hours'):
        community_popup_settings['community_popup_interval_hours'] = '3'

    # Se leen con el mismo helper que usa la tienda, así el admin ve
    # exactamente las horas que se están aplicando (incluidos los valores
    # por defecto cuando todavía no se ha guardado nada).
    manual_schedule = get_manual_schedule()
    manual_schedule_settings = {
        'manual_open_hour': str(manual_schedule['open_hour']),
        'manual_close_hour': str(manual_schedule['close_hour']),
    }

    ranking_games = Game.query.filter_by(is_active=True).order_by(Game.name.asc()).all()
    active_payment_methods = PaymentMethod.query.filter_by(is_active=True).order_by(PaymentMethod.sort_order.asc(), PaymentMethod.name.asc()).all()
    ranking_packages = Package.query.filter_by(is_active=True).order_by(Package.game_id.asc(), Package.sort_order.asc(), Package.name.asc()).all()
    ranking_packages_by_game = {}
    for package in ranking_packages:
        ranking_packages_by_game.setdefault(str(package.game_id), []).append({
            'id': package.id,
            'name': package.name,
            'sort_order': package.sort_order,
            'is_automated': bool(package.is_automated),
        })

    ranking_prize_settings = {'free_fire': {}, 'blood_strike': {}}
    for ranking_key_name in ranking_prize_settings.keys():
        labels = RANKING_PRIZE_LABELS.get(ranking_key_name, [])
        for position in RANKING_PRIZE_POSITIONS:
            package_setting = Setting.query.filter_by(key=_ranking_prize_package_key(ranking_key_name, position)).first()
            auto_setting = Setting.query.filter_by(key=_ranking_prize_auto_key(ranking_key_name, position)).first()
            ranking_prize_settings[ranking_key_name][position] = {
                'package_id': package_setting.value if package_setting else '',
                'auto': auto_setting.value if auto_setting else '0',
                'reward_label': labels[position - 1] if position - 1 < len(labels) else f'Puesto #{position}',
            }

    if request.method == 'POST':
        new_rate = request.form.get('usd_rate_bs', '').strip()
        default_pkg = request.form.get('default_auto_package_id', '').strip()
        remove_logo = request.form.get('remove_logo')
        logo_file = request.files.get('site_logo')
        remove_site_background = request.form.get('remove_site_background')
        site_background_file = request.files.get('site_background_image')
        remove_order_status_image = request.form.get('remove_order_status_image')
        order_status_image_file = request.files.get('order_status_image')
        remove_promo_banner_image = request.form.get('remove_promo_banner_image')
        promo_banner_image_file = request.files.get('promo_banner_image')
        promo_banner_link = (request.form.get('promo_banner_link', '') or '').strip()
        checkout_payment_video_method = (request.form.get('checkout_payment_video_method', '') or '').strip().lower()
        checkout_payment_video_title = (request.form.get('checkout_payment_video_title', '') or '').strip()
        checkout_payment_video_message = (request.form.get('checkout_payment_video_message', '') or '').strip()
        checkout_payment_video_cta = (request.form.get('checkout_payment_video_cta', '') or '').strip()
        remove_checkout_payment_video_file = request.form.get('remove_checkout_payment_video_file')
        checkout_payment_video_file = request.files.get('checkout_payment_video_file')
        social_payload = {k: (request.form.get(k, '') or '').strip() for k in social_keys}
        email_payload = {k: (request.form.get(k, '') or '').strip() for k in email_keys}
        payment_verify_payload = {
            'auto_verify_payments': 'true' if request.form.get('auto_verify_payments') else 'false',
            'pabilo_api_key': (request.form.get('pabilo_api_key', '') or '').strip(),
        }
        binance_auto_payload = {
            'binance_auto_enabled': '1' if request.form.get('binance_auto_enabled') else '0',
            'binance_wallet_address': (request.form.get('binance_wallet_address', '') or '').strip(),
        }
        ranking_payload = {
            'ranking_free_fire_enabled': '1' if request.form.get('ranking_free_fire_enabled') else '0',
            'ranking_blood_strike_enabled': '1' if request.form.get('ranking_blood_strike_enabled') else '0',
            'ranking_free_fire_game_id': (request.form.get('ranking_free_fire_game_id', '') or '').strip(),
            'ranking_blood_strike_game_id': (request.form.get('ranking_blood_strike_game_id', '') or '').strip(),
        }
        community_interval_raw = (request.form.get('community_popup_interval_hours', '') or '').strip()
        try:
            community_interval_hours = str(max(1, int(float(community_interval_raw)))) if community_interval_raw else '3'
        except ValueError:
            community_interval_hours = '3'
        community_popup_payload = {
            'community_popup_enabled': '1' if request.form.get('community_popup_enabled') else '0',
            'community_popup_interval_hours': community_interval_hours,
            'community_popup_whatsapp_url': (request.form.get('community_popup_whatsapp_url', '') or '').strip(),
        }

        def _clean_hour(field_name, fallback):
            raw = (request.form.get(field_name, '') or '').strip()
            try:
                hour = int(raw)
            except (TypeError, ValueError):
                return str(fallback)
            return str(hour) if 0 <= hour <= 23 else str(fallback)

        manual_schedule_payload = {
            'manual_open_hour': _clean_hour('manual_open_hour', manual_schedule['open_hour']),
            'manual_close_hour': _clean_hour('manual_close_hour', manual_schedule['close_hour']),
        }

        if new_rate:
            try:
                float(new_rate)
            except ValueError:
                flash('La tasa debe ser un número válido.', 'danger')
                return redirect(url_for('admin_bp.settings'))

            if not usd_rate_setting:
                usd_rate_setting = Setting(
                    key='usd_rate_bs',
                    value=new_rate,
                    description='Tasa de cambio USD a Bs',
                )
                db.session.add(usd_rate_setting)
            else:
                usd_rate_setting.value = new_rate

        if default_pkg:
            if not default_pkg_setting:
                default_pkg_setting = Setting(
                    key='default_auto_package_id',
                    value=default_pkg,
                    description='ID del primer paquete automático',
                )
                db.session.add(default_pkg_setting)
            else:
                default_pkg_setting.value = default_pkg

        if remove_logo and site_logo_setting:
            delete_uploaded_file(site_logo_setting.value)
            site_logo_setting.value = ''

        if logo_file and logo_file.filename:
            saved_logo = save_image(logo_file, 'branding')
            if saved_logo:
                if site_logo_setting and site_logo_setting.value:
                    delete_uploaded_file(site_logo_setting.value)
                if not site_logo_setting:
                    site_logo_setting = Setting(
                        key='site_logo',
                        value=saved_logo,
                        description='Logo personalizado para el header'
                    )
                    db.session.add(site_logo_setting)
                else:
                    site_logo_setting.value = saved_logo

        if remove_site_background and site_background_setting:
            delete_uploaded_file(site_background_setting.value)
            site_background_setting.value = ''

        if site_background_file and site_background_file.filename:
            saved_background = save_image(site_background_file, 'branding')
            if saved_background:
                if site_background_setting and site_background_setting.value:
                    delete_uploaded_file(site_background_setting.value)
                if not site_background_setting:
                    site_background_setting = Setting(
                        key='site_background_image',
                        value=saved_background,
                        description='Imagen de fondo de toda la web'
                    )
                    db.session.add(site_background_setting)
                else:
                    site_background_setting.value = saved_background

        if remove_order_status_image and order_status_image_setting:
            delete_uploaded_file(order_status_image_setting.value)
            order_status_image_setting.value = ''

        if order_status_image_file and order_status_image_file.filename:
            saved_order_status_image = save_image(order_status_image_file, 'branding')
            if saved_order_status_image:
                if order_status_image_setting and order_status_image_setting.value:
                    delete_uploaded_file(order_status_image_setting.value)
                if not order_status_image_setting:
                    order_status_image_setting = Setting(
                        key='order_status_image',
                        value=saved_order_status_image,
                        description='Imagen decorativa para el seguimiento de órdenes'
                    )
                    db.session.add(order_status_image_setting)
                else:
                    order_status_image_setting.value = saved_order_status_image

        if remove_promo_banner_image and promo_banner_image_setting:
            delete_uploaded_file(promo_banner_image_setting.value)
            promo_banner_image_setting.value = ''

        if promo_banner_image_file and promo_banner_image_file.filename:
            saved_promo_banner = save_image(promo_banner_image_file, 'branding')
            if saved_promo_banner:
                if promo_banner_image_setting and promo_banner_image_setting.value:
                    delete_uploaded_file(promo_banner_image_setting.value)
                if not promo_banner_image_setting:
                    promo_banner_image_setting = Setting(
                        key='promo_banner_image',
                        value=saved_promo_banner,
                        description='Banner promocional pequeño mostrado arriba en la tienda'
                    )
                    db.session.add(promo_banner_image_setting)
                else:
                    promo_banner_image_setting.value = saved_promo_banner

        if not promo_banner_link_setting:
            promo_banner_link_setting = Setting(
                key='promo_banner_link',
                value=promo_banner_link,
                description='Link al que lleva el banner promocional al hacer clic'
            )
            db.session.add(promo_banner_link_setting)
        else:
            promo_banner_link_setting.value = promo_banner_link

        valid_video_method = ''
        if checkout_payment_video_method:
            matched_method = PaymentMethod.query.filter_by(code=checkout_payment_video_method, is_active=True).first()
            if matched_method:
                valid_video_method = matched_method.code

        if remove_checkout_payment_video_file and checkout_payment_video_file_setting:
            if checkout_payment_video_file_setting.value:
                delete_uploaded_file(checkout_payment_video_file_setting.value)
            checkout_payment_video_file_setting.value = ''

        if checkout_payment_video_file and checkout_payment_video_file.filename:
            saved_checkout_payment_video = save_video(checkout_payment_video_file, 'payments')
            if not saved_checkout_payment_video:
                flash('El video debe estar en formato mp4, webm, mov o m4v.', 'danger')
                return redirect(url_for('admin_bp.settings'))
            if checkout_payment_video_file_setting and checkout_payment_video_file_setting.value:
                delete_uploaded_file(checkout_payment_video_file_setting.value)
            if not checkout_payment_video_file_setting:
                checkout_payment_video_file_setting = Setting(
                    key='checkout_payment_video_file',
                    value=saved_checkout_payment_video,
                    description='Video tutorial mostrado solo para un método de pago específico en checkout',
                )
                db.session.add(checkout_payment_video_file_setting)
            else:
                checkout_payment_video_file_setting.value = saved_checkout_payment_video

        checkout_video_settings_payload = {
            'checkout_payment_video_method': (valid_video_method, 'Método de pago al que se le muestra el video tutorial del checkout.'),
            'checkout_payment_video_title': (checkout_payment_video_title, 'Título del mensaje tutorial del checkout por método de pago.'),
            'checkout_payment_video_message': (checkout_payment_video_message, 'Mensaje descriptivo del video tutorial del checkout por método de pago.'),
            'checkout_payment_video_cta': (checkout_payment_video_cta, 'Texto del botón para cerrar o continuar luego de ver el video tutorial.'),
        }

        for key, payload in checkout_video_settings_payload.items():
            val, desc = payload
            current_setting = Setting.query.filter_by(key=key).first()
            if not current_setting:
                current_setting = Setting(key=key, value=val, description=desc)
                db.session.add(current_setting)
            else:
                current_setting.value = val

        for key, desc in social_keys.items():
            val = social_payload.get(key, '')
            current_setting = Setting.query.filter_by(key=key).first()
            if val:
                if not current_setting:
                    current_setting = Setting(key=key, value=val, description=desc)
                    db.session.add(current_setting)
                else:
                    current_setting.value = val
            else:
                if current_setting:
                    current_setting.value = ''

        for key, desc in email_keys.items():
            val = email_payload.get(key, '')
            current_setting = Setting.query.filter_by(key=key).first()
            if val:
                if not current_setting:
                    current_setting = Setting(key=key, value=val, description=desc)
                    db.session.add(current_setting)
                else:
                    current_setting.value = val
            else:
                if current_setting:
                    current_setting.value = ''

        for key, desc in payment_verify_keys.items():
            val = payment_verify_payload.get(key, '')
            current_setting = Setting.query.filter_by(key=key).first()
            if not current_setting:
                current_setting = Setting(key=key, value=val, description=desc)
                db.session.add(current_setting)
            else:
                current_setting.value = val

        for key, desc in binance_auto_keys.items():
            val = binance_auto_payload.get(key, '')
            current_setting = Setting.query.filter_by(key=key).first()
            if not current_setting:
                current_setting = Setting(key=key, value=val, description=desc)
                db.session.add(current_setting)
            else:
                current_setting.value = val

        for key, desc in ranking_keys.items():
            val = ranking_payload.get(key, '')
            current_setting = Setting.query.filter_by(key=key).first()
            if not current_setting:
                current_setting = Setting(key=key, value=val, description=desc)
                db.session.add(current_setting)
            else:
                current_setting.value = val

        for key, desc in community_popup_keys.items():
            val = community_popup_payload.get(key, '')
            current_setting = Setting.query.filter_by(key=key).first()
            if not current_setting:
                current_setting = Setting(key=key, value=val, description=desc)
                db.session.add(current_setting)
            else:
                current_setting.value = val

        for key, desc in manual_schedule_keys.items():
            val = manual_schedule_payload.get(key, '')
            current_setting = Setting.query.filter_by(key=key).first()
            if not current_setting:
                current_setting = Setting(key=key, value=val, description=desc)
                db.session.add(current_setting)
            else:
                current_setting.value = val

        ranking_prize_desc = 'Paquete vinculado al premio mensual del ranking por puesto.'
        ranking_prize_auto_desc = 'Si está activo, el paquete del premio se fuerza como automatizado.'
        for ranking_key_name in ('free_fire', 'blood_strike'):
            selected_game_id = ranking_payload.get(f'ranking_{ranking_key_name}_game_id', '')
            selected_game_id_int = int(selected_game_id) if selected_game_id.isdigit() else None

            for position in RANKING_PRIZE_POSITIONS:
                package_key = _ranking_prize_package_key(ranking_key_name, position)
                auto_key = _ranking_prize_auto_key(ranking_key_name, position)
                package_value = (request.form.get(package_key, '') or '').strip()
                auto_value = '1' if request.form.get(auto_key) else '0'

                valid_package_value = ''
                prize_package = None
                if package_value.isdigit() and selected_game_id_int:
                    prize_package = Package.query.filter_by(
                        id=int(package_value),
                        game_id=selected_game_id_int,
                        is_active=True,
                    ).first()
                    if prize_package:
                        valid_package_value = str(prize_package.id)

                package_setting = Setting.query.filter_by(key=package_key).first()
                if not package_setting:
                    package_setting = Setting(key=package_key, value=valid_package_value, description=ranking_prize_desc)
                    db.session.add(package_setting)
                else:
                    package_setting.value = valid_package_value

                auto_setting = Setting.query.filter_by(key=auto_key).first()
                if not auto_setting:
                    auto_setting = Setting(key=auto_key, value=auto_value, description=ranking_prize_auto_desc)
                    db.session.add(auto_setting)
                else:
                    auto_setting.value = auto_value

                if prize_package and auto_value == '1':
                    prize_package.is_automated = True

        db.session.commit()
        flash('Configuración actualizada.', 'success')
        return redirect(url_for('admin_bp.settings'))

    return render_template(
        'admin/settings.html',
        usd_rate=usd_rate,
        default_package_id=default_auto_package_id,
        site_logo=site_logo_value,
        site_background_image=site_background_value,
        order_status_image=order_status_image_value,
        promo_banner_image=promo_banner_image_value,
        promo_banner_link=promo_banner_link_value,
        social_settings=social_settings,
        email_settings=email_settings,
        payment_verify_settings=payment_verify_settings,
        binance_auto_settings=binance_auto_settings,
        active_payment_methods=active_payment_methods,
        checkout_payment_video_method=checkout_payment_video_method_value,
        checkout_payment_video_title=checkout_payment_video_title_value,
        checkout_payment_video_message=checkout_payment_video_message_value,
        checkout_payment_video_cta=checkout_payment_video_cta_value,
        checkout_payment_video_file=checkout_payment_video_file_value,
        ranking_settings=ranking_settings,
        ranking_games=ranking_games,
        ranking_prize_settings=ranking_prize_settings,
        ranking_prize_positions=RANKING_PRIZE_POSITIONS,
        ranking_prize_labels=RANKING_PRIZE_LABELS,
        ranking_packages_by_game=ranking_packages_by_game,
        community_popup_settings=community_popup_settings,
        manual_schedule_settings=manual_schedule_settings,
    )


@admin_bp.route('/minigames', methods=['GET', 'POST'])
@login_required
def minigames():
    slot_defs = get_minigame_slot_defs()
    all_games = Game.query.filter_by(is_active=True).order_by(Game.name.asc()).all()
    all_packages = (
        Package.query.filter_by(is_active=True)
        .order_by(Package.game_id.asc(), Package.sort_order.asc(), Package.name.asc())
        .all()
    )
    packages_by_game_id = {}
    for pkg in all_packages:
        packages_by_game_id.setdefault(str(pkg.game_id), []).append({'id': pkg.id, 'name': pkg.name})

    if request.method == 'POST':
        interval_raw = (request.form.get('minigame_win_every_n_spins', '') or '').strip()
        try:
            interval_value = str(max(1, int(float(interval_raw)))) if interval_raw else str(DEFAULT_MINIGAME_WIN_INTERVAL)
        except ValueError:
            interval_value = str(DEFAULT_MINIGAME_WIN_INTERVAL)

        setting_updates = {'minigame_win_every_n_spins': interval_value}
        for slot in slot_defs:
            slot_key = slot['slot_key']
            game_id = (request.form.get(f'minigame_slot_{slot_key}_game_id', '') or '').strip()
            package_id = (request.form.get(f'minigame_slot_{slot_key}_prize_package_id', '') or '').strip()
            setting_updates[f'minigame_slot_{slot_key}_game_id'] = game_id
            setting_updates[f'minigame_slot_{slot_key}_prize_package_id'] = package_id

        for key, value in setting_updates.items():
            setting = Setting.query.filter_by(key=key).first()
            if not setting:
                setting = Setting(key=key, value=value, description='Configuración de minijuego por juego.')
                db.session.add(setting)
            else:
                setting.value = value
        db.session.commit()
        flash('Configuración de minijuegos actualizada.', 'success')
        return redirect(url_for('admin_bp.minigames'))

    win_interval = get_minigame_win_interval()
    slots_config = get_minigame_slots_config()
    counter_cards = []
    for slot in slots_config:
        counter = get_or_create_minigame_counter(slot['game']) if slot['game'] else None
        play_count = int(counter.play_count or 0) if counter else 0
        remaining = win_interval - (play_count % win_interval) if play_count % win_interval else win_interval
        counter_cards.append({
            'key': slot['slot_key'],
            'label': slot['label'],
            'enabled': slot['enabled'],
            'game_name': slot['game'].name if slot['game'] else None,
            'prize_name': slot['prize_package'].name if slot['prize_package'] else None,
            'play_count': play_count,
            'spins_until_win': remaining if slot['enabled'] else None,
        })
    db.session.commit()  # persiste los contadores creados recién arriba

    winners = (
        OrderMiniGameOpportunity.query
        .filter(OrderMiniGameOpportunity.status == 'played')
        .filter(OrderMiniGameOpportunity.result_kind == 'game_prize')
        .options(
            joinedload(OrderMiniGameOpportunity.order).joinedload(Order.game),
            joinedload(OrderMiniGameOpportunity.prize_order),
        )
        .order_by(OrderMiniGameOpportunity.played_at.desc())
        .limit(100)
        .all()
    )

    return render_template(
        'admin/minigames.html',
        slot_defs=slot_defs,
        slots_config=slots_config,
        all_games=all_games,
        packages_by_game_id=packages_by_game_id,
        win_interval=win_interval,
        counter_cards=counter_cards,
        winners=winners,
        minigame_dev_mode=is_minigame_dev_mode(),
    )


# ─── Sistema de puntos ────────────────────────────────────────────────────────

@admin_bp.route('/points', methods=['GET', 'POST'])
@login_required
def points():
    all_games = Game.query.filter_by(is_active=True).order_by(Game.name.asc()).all()
    all_packages = (
        Package.query.filter_by(is_active=True)
        .order_by(Package.game_id.asc(), Package.sort_order.asc(), Package.name.asc())
        .all()
    )
    packages_by_game_id = {}
    for pkg in all_packages:
        packages_by_game_id.setdefault(str(pkg.game_id), []).append({'id': pkg.id, 'name': pkg.name})

    if request.method == 'POST':
        form_action = request.form.get('form_action', 'settings')

        if form_action == 'add_mapping':
            game_id = (request.form.get('mapping_game_id', '') or '').strip()
            package_id = (request.form.get('mapping_package_id', '') or '').strip()
            if not game_id or not package_id:
                flash('Elige un juego y un paquete premio.', 'danger')
                return redirect(url_for('admin_bp.points'))

            existing = PointsPrizeMapping.query.filter_by(game_id=int(game_id)).first()
            if existing:
                existing.package_id = int(package_id)
                existing.is_active = True
                flash('Premio de puntos actualizado para ese juego.', 'success')
            else:
                db.session.add(PointsPrizeMapping(game_id=int(game_id), package_id=int(package_id), is_active=True))
                flash('Premio de puntos agregado.', 'success')
            db.session.commit()
            return redirect(url_for('admin_bp.points'))

        # form_action == 'settings' (por defecto)
        per_dollar_raw = (request.form.get('points_per_dollar', '') or '').strip()
        spin_cost_raw = (request.form.get('points_spin_cost', '') or '').strip()
        interval_raw = (request.form.get('points_win_every_n_spins', '') or '').strip()

        def _clean_positive(raw, default_value):
            try:
                value = float(raw)
                return str(value if value > 0 else default_value)
            except (TypeError, ValueError):
                return str(default_value)

        setting_updates = {
            'points_per_dollar': _clean_positive(per_dollar_raw, DEFAULT_POINTS_PER_DOLLAR),
            'points_spin_cost': str(int(float(_clean_positive(spin_cost_raw, DEFAULT_POINTS_SPIN_COST)))),
            'points_win_every_n_spins': str(int(float(_clean_positive(interval_raw, DEFAULT_POINTS_WIN_INTERVAL)))),
        }
        for key, value in setting_updates.items():
            setting = Setting.query.filter_by(key=key).first()
            if not setting:
                setting = Setting(key=key, value=value, description='Configuración del sistema de puntos.')
                db.session.add(setting)
            else:
                setting.value = value
        db.session.commit()
        flash('Configuración de puntos actualizada.', 'success')
        return redirect(url_for('admin_bp.points'))

    mappings = (
        PointsPrizeMapping.query
        .options(joinedload(PointsPrizeMapping.game), joinedload(PointsPrizeMapping.package))
        .join(Game, Game.id == PointsPrizeMapping.game_id)
        .order_by(Game.name.asc())
        .all()
    )

    recent_spins = (
        PointsSpinLog.query
        .options(joinedload(PointsSpinLog.game), joinedload(PointsSpinLog.prize_order))
        .order_by(PointsSpinLog.created_at.desc())
        .limit(100)
        .all()
    )

    top_balances = (
        PlayerPoints.query
        .options(joinedload(PlayerPoints.game))
        .order_by(PlayerPoints.points_balance.desc())
        .limit(50)
        .all()
    )

    return render_template(
        'admin/points.html',
        all_games=all_games,
        packages_by_game_id=packages_by_game_id,
        mappings=mappings,
        recent_spins=recent_spins,
        top_balances=top_balances,
        points_per_dollar=get_points_per_dollar_rate(),
        points_spin_cost=get_points_spin_cost(),
        points_win_interval=get_points_win_interval(),
    )


@admin_bp.route('/points/mappings/<int:mapping_id>/toggle', methods=['POST'])
@login_required
def points_mapping_toggle(mapping_id):
    mapping = PointsPrizeMapping.query.get_or_404(mapping_id)
    mapping.is_active = not mapping.is_active
    db.session.commit()
    flash('Premio de puntos actualizado.', 'success')
    return redirect(url_for('admin_bp.points'))


@admin_bp.route('/points/mappings/<int:mapping_id>/delete', methods=['POST'])
@login_required
def points_mapping_delete(mapping_id):
    mapping = PointsPrizeMapping.query.get_or_404(mapping_id)
    db.session.delete(mapping)
    db.session.commit()
    flash('Premio de puntos eliminado. Ese juego dejará de aparecer en "Canjear puntos".', 'warning')
    return redirect(url_for('admin_bp.points'))


# ─── Notificaciones push ──────────────────────────────────────────────────────

@admin_bp.route('/notifications', methods=['GET', 'POST'])
@login_required
def notifications():
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        body = (request.form.get('body') or '').strip()
        url_link = (request.form.get('url') or '').strip()

        if not title or not body:
            flash('Título y mensaje son obligatorios.', 'danger')
            return redirect(url_for('admin_bp.notifications'))

        if not is_push_configured():
            flash('Las notificaciones push todavía no están listas en el servidor.', 'danger')
            return redirect(url_for('admin_bp.notifications'))

        result = send_push_broadcast(title, body, url=url_link or None)
        flash(
            f'Notificación enviada: {result["sent"]} entregada(s), {result["failed"]} fallida(s).',
            'success' if result['sent'] else 'warning',
        )
        return redirect(url_for('admin_bp.notifications'))

    subscriber_count = PushSubscription.query.count()
    return render_template(
        'admin/notifications.html',
        subscriber_count=subscriber_count,
        push_configured=is_push_configured(),
    )


# ─── Revendedores Whitelabel API ─────────────────────────────────────────────

def _normalize_rev_catalog_payload(payload):
    items = []
    games = payload.get('games') or payload.get('products') or []
    if isinstance(payload, list):
        games = payload
    for game in games:
        game_id = game.get('game_id') or game.get('id')
        game_name = game.get('name') or game.get('nombre') or ''
        packages = game.get('packages') or game.get('paquetes') or []
        for pkg in packages:
            pkg_id = pkg.get('package_id') or pkg.get('id')
            pkg_name = pkg.get('name') or pkg.get('nombre') or ''
            price = pkg.get('price') or pkg.get('precio') or 0
            items.append({
                'remote_product_id': int(game_id) if game_id is not None else None,
                'remote_product_name': str(game_name).strip(),
                'remote_package_id': int(pkg_id) if pkg_id is not None else None,
                'remote_package_name': str(pkg_name).strip(),
                'active': True,
                'raw_json': json.dumps(pkg, ensure_ascii=False),
            })
    return items


@admin_bp.route('/revendedores/mapping')
@login_required
def revendedores_mapping():
    return render_template('admin/revendedores_mapping.html')


@admin_bp.route('/revendedores/sync', methods=['POST'])
@login_required
def revendedores_sync_catalog():
    base_url, api_key, catalog_path, _ = get_revendedores_env()
    if not base_url or not api_key:
        return jsonify({'ok': False, 'error': 'REVENDEDORES_BASE_URL o REVENDEDORES_API_KEY no configurados'}), 400

    normalized = []
    remote_error = ''
    try:
        resp = requests.get(
            f'{base_url}{catalog_path}',
            headers={'X-API-Key': api_key},
            timeout=30,
        )
        if not resp.ok:
            key_preview = (api_key[:12] + '...') if len(api_key) > 12 else '(vacía)'
            remote_error = f'HTTP {resp.status_code} en {catalog_path} (url={base_url}, key={key_preview})'
        else:
            payload = resp.json()
            normalized = _normalize_rev_catalog_payload(payload)
            if not normalized:
                remote_error = 'Catálogo API sin paquetes válidos'
    except Exception as exc:
        remote_error = f'No se pudo consultar catálogo API: {str(exc)}'

    if not normalized:
        return jsonify({'ok': False, 'error': f'No se pudo sincronizar catálogo: {remote_error}'}), 502

    games_summary = {}
    for ent in normalized:
        gname = ent.get('remote_product_name') or '?'
        pid = ent.get('remote_product_id')
        k = f'{gname} (pid={pid})'
        games_summary[k] = games_summary.get(k, 0) + 1

    created = 0
    updated = 0
    seen_keys = set()

    try:
        for ent in normalized:
            key = (ent.get('remote_product_id'), ent.get('remote_package_id'))
            seen_keys.add(key)
            row = RevendedoresCatalogItem.query.filter_by(
                remote_product_id=ent.get('remote_product_id'),
                remote_package_id=ent.get('remote_package_id'),
            ).first()
            if not row:
                row = RevendedoresCatalogItem(**ent)
                db.session.add(row)
                created += 1
            else:
                row.remote_product_name = ent.get('remote_product_name', '')
                row.remote_package_name = ent.get('remote_package_name', '')
                row.active = bool(ent.get('active'))
                row.raw_json = ent.get('raw_json', '')
                updated += 1

        deactivated = 0
        for row in RevendedoresCatalogItem.query.all():
            key = (row.remote_product_id, row.remote_package_id)
            if key not in seen_keys:
                if row.active:
                    deactivated += 1
                row.active = False

        db.session.commit()
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': f'Error guardando catálogo: {str(exc)}'}), 500

    active_count = RevendedoresCatalogItem.query.filter_by(active=True).count()

    return jsonify({
        'ok': True,
        'source': 'api',
        'created': created,
        'updated': updated,
        'deactivated': deactivated,
        'total_normalized': len(normalized),
        'active_in_db': active_count,
        'games': games_summary,
    })


@admin_bp.route('/revendedores/mapping-data', methods=['GET'])
@login_required
def revendedores_mapping_data():
    game_id = request.args.get('game_id', type=int)

    games = Game.query.filter_by(is_active=True).order_by(Game.name).all()
    packages_query = Package.query.filter_by(is_active=True)
    if game_id:
        packages_query = packages_query.filter_by(game_id=game_id)
    store_packages = packages_query.order_by(Package.sort_order.asc(), Package.id.asc()).all()

    mappings = RevendedoresItemMapping.query.filter(
        RevendedoresItemMapping.store_package_id.in_([p.id for p in store_packages])
    ).all() if store_packages else []
    mapping_map = {m.store_package_id: m for m in mappings}

    catalog_rows = RevendedoresCatalogItem.query.filter_by(active=True).order_by(
        RevendedoresCatalogItem.remote_product_name.asc(),
        RevendedoresCatalogItem.remote_package_name.asc(),
        RevendedoresCatalogItem.id.asc(),
    ).all()

    def _extract_price(raw_json_str):
        try:
            obj = json.loads(raw_json_str or '{}')
            p = obj.get('price') or obj.get('precio') or obj.get('cost')
            if p is not None:
                return round(float(p), 2)
        except Exception:
            pass
        return None

    return jsonify({
        'ok': True,
        'games': [{'id': g.id, 'name': g.name} for g in games],
        'store_packages': [
            {
                'id': p.id,
                'game_id': p.game_id,
                'name': p.name,
                'price': str(p.price),
                'game_name': p.game.name if p.game else '',
            }
            for p in store_packages
        ],
        'remote_catalog': [
            {
                'catalog_id': r.id,
                'remote_product_id': r.remote_product_id,
                'remote_product_name': r.remote_product_name or '',
                'remote_package_id': r.remote_package_id,
                'remote_package_name': r.remote_package_name or '',
                'price': _extract_price(r.raw_json),
            }
            for r in catalog_rows
        ],
        'mappings': [
            {
                'store_package_id': m.store_package_id,
                'catalog_id': m.catalog_item_id,
                'catalog_id_2': m.catalog_item_id_2,
                'auto_enabled': m.auto_enabled,
            }
            for m in mappings
        ],
    })


@admin_bp.route('/revendedores/mappings/bulk', methods=['POST'])
@login_required
def revendedores_mappings_bulk():
    data = request.get_json(silent=True) or {}
    entries = data.get('entries', [])
    saved = 0
    removed = 0

    try:
        for entry in entries:
            store_pkg_id = int(entry.get('store_package_id', 0))
            catalog_id_str = str(entry.get('catalog_id', '')).strip()
            catalog_id_2_str = str(entry.get('catalog_id_2', '')).strip()
            auto_enabled = bool(entry.get('auto_enabled'))

            if not store_pkg_id:
                continue

            existing = RevendedoresItemMapping.query.filter_by(store_package_id=store_pkg_id).first()

            if not catalog_id_str and not catalog_id_2_str:
                if existing:
                    db.session.delete(existing)
                    removed += 1
                continue

            catalog_id = int(catalog_id_str) if catalog_id_str else None
            catalog_id_2 = int(catalog_id_2_str) if catalog_id_2_str else None

            if not catalog_id and catalog_id_2:
                catalog_id = catalog_id_2
                catalog_id_2 = None

            if existing:
                existing.catalog_item_id = catalog_id
                existing.catalog_item_id_2 = catalog_id_2
                existing.auto_enabled = auto_enabled
                existing.active = True
            else:
                new_map = RevendedoresItemMapping(
                    store_package_id=store_pkg_id,
                    catalog_item_id=catalog_id,
                    catalog_item_id_2=catalog_id_2,
                    active=True,
                    auto_enabled=auto_enabled,
                )
                db.session.add(new_map)
            saved += 1

        db.session.commit()
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': str(exc)}), 500

    return jsonify({'ok': True, 'saved': saved, 'removed': removed})


@admin_bp.route('/orders/<int:order_id>/verify-recharge', methods=['POST'])
@login_required
def order_verify_recharge(order_id):
    """Revisa/continúa la cola de recargas de Revendedores para una orden.

    Delega toda la lógica (confirmar el intento previo, decidir si hay que
    reintentar, avanzar al siguiente paso o completar la orden) a
    process_revendedores_queue, que es la misma función que usa el
    scheduler de recuperación en segundo plano. Así este botón y la
    recuperación automática nunca pueden quedar desincronizados ni disparar
    una recarga duplicada.
    """
    order = Order.query.get_or_404(order_id)
    if order.status != 'pending':
        return jsonify({'ok': True, 'result': 'already_processed', 'order_status': order.status})

    auto_resp = {}
    try:
        auto_resp = json.loads(order.automation_response or '{}')
    except Exception:
        pass

    if not auto_resp.get('pending_verification'):
        return jsonify({'ok': True, 'result': 'no_verification_needed', 'can_approve': True})

    try:
        # force=False: este botón hace polling pasivo (se dispara solo con
        # la página abierta), así que respeta el límite de reintentos
        # automáticos igual que el scheduler de recuperación. Una vez
        # agotados, el flujo pasa al botón normal "✓ Aprobar", que sí
        # fuerza un intento más de forma explícita.
        result = process_revendedores_queue(order, base_state=auto_resp, force=False)
    except Exception as exc:
        current_app.logger.exception('Error verificando recarga de Revendedores para la orden %s', order_id)
        return jsonify({'ok': False, 'error': f'No se pudo verificar: {exc}', 'can_approve': False})

    db.session.refresh(order)

    if not result:
        return jsonify({'ok': True, 'result': 'no_verification_needed', 'can_approve': True, 'order_status': order.status})

    if result.get('ok') and not result.get('pending_verification'):
        return jsonify({
            'ok': True,
            'result': 'completed',
            'order_status': order.status,
            'message': result.get('message'),
        })

    if result.get('pending_verification'):
        return jsonify({
            'ok': True,
            'result': 'processing',
            'order_status': order.status,
            'can_approve': False,
            'message': result.get('message') or 'Verificando en Revendedores...',
        })

    # ok=False y pending_verification=False → se confirmó que no se
    # completó (o se agotaron los reintentos automáticos) y ahora se puede
    # reintentar manualmente con el botón "✓ Aprobar".
    return jsonify({
        'ok': True,
        'result': 'failed',
        'order_status': order.status,
        'can_approve': True,
        'message': result.get('message') or 'No se pudo confirmar la recarga en Revendedores. Puedes reintentar.',
    })


# ─── Statistics ──────────────────────────────────────────────────────────────

@admin_bp.route('/stats')
@login_required
def stats():
    today = now_ve().date()

    tracked_statuses = ('pending', 'approved', 'completed')
    history_start = today - timedelta(days=14)
    history_days = [history_start + timedelta(days=offset) for offset in range(15)]
    history_index = {day.isoformat(): day for day in history_days}

    service_options = Game.query.filter_by(is_active=True).order_by(Game.position.asc(), Game.id.asc()).all()
    service_map = {str(game.id): game for game in service_options}

    selected_service_raw = (request.args.get('service') or '').strip()
    selected_service = service_map.get(selected_service_raw)

    selected_date = today
    selected_date_raw = (request.args.get('date') or '').strip()
    if selected_date_raw:
        try:
            parsed_date = datetime.strptime(selected_date_raw, '%Y-%m-%d').date()
            if parsed_date < history_start:
                selected_date = history_start
            elif parsed_date > today:
                selected_date = today
            else:
                selected_date = parsed_date
        except ValueError:
            selected_date = today

    def _coupon_code_for_order(order):
        code = (order.affiliate_code or '').strip().upper()
        if code:
            return code

        discount_amount = float(order.discount_amount or 0)
        if discount_amount > 0 or order.discount_id or order.affiliate_id:
            return 'CODIGO NO REGISTRADO'

        return ''

    def _sort_text(value):
        return str(value or '').strip().lower()

    window_start = ve_day_start_utc_naive(history_start)
    window_end = ve_day_start_utc_naive(today + timedelta(days=1))

    orders_query = Order.query.options(
        joinedload(Order.game),
        joinedload(Order.package),
    ).filter(
        Order.created_at >= window_start,
        Order.created_at < window_end,
        Order.status.in_(tracked_statuses),
    )

    if selected_service:
        orders_query = orders_query.filter(Order.game_id == selected_service.id)

    orders = orders_query.order_by(Order.created_at.desc()).all()

    daily_stats = {
        day.isoformat(): {
            'date': day,
            'total_orders': 0,
            'sold_orders': 0,
            'pending_orders': 0,
            'revenue': 0.0,
            'coupon_orders': 0,
            'no_coupon_orders': 0,
            'discount_total': 0.0,
        }
        for day in history_days
    }

    package_rows = {}
    daily_orders = []

    for order in orders:
        created_at_ve = to_ve(order.created_at)
        if created_at_ve is None:
            continue

        day_iso = created_at_ve.date().isoformat()
        if day_iso not in history_index:
            continue

        amount_value = float(order.amount or 0)
        discount_value = float(order.discount_amount or 0)
        coupon_code = _coupon_code_for_order(order)
        has_coupon = bool(coupon_code)
        sold_order = order.status in ('approved', 'completed')

        day_bucket = daily_stats[day_iso]
        day_bucket['total_orders'] += 1
        day_bucket['discount_total'] += discount_value
        if sold_order:
            day_bucket['sold_orders'] += 1
            day_bucket['revenue'] += amount_value
        elif order.status == 'pending':
            day_bucket['pending_orders'] += 1

        if has_coupon:
            day_bucket['coupon_orders'] += 1
        else:
            day_bucket['no_coupon_orders'] += 1

        if day_iso != selected_date.isoformat():
            continue

        pkg_key = order.package_id
        row = package_rows.get(pkg_key)
        if row is None:
            row = {
                'game_name': order.game.name if order.game else 'Servicio',
                'game_position': getattr(order.game, 'position', 0) or 0,
                'package_name': order.package.name if order.package else 'Paquete',
                'package_sort_order': getattr(order.package, 'sort_order', 0) or 0,
                'total_orders': 0,
                'sold_orders': 0,
                'pending_orders': 0,
                'revenue': 0.0,
                'discount_total': 0.0,
                'coupon_breakdown': defaultdict(int),
                'no_coupon_orders': 0,
            }
            package_rows[pkg_key] = row

        row['total_orders'] += 1
        row['discount_total'] += discount_value
        if sold_order:
            row['sold_orders'] += 1
            row['revenue'] += amount_value
        elif order.status == 'pending':
            row['pending_orders'] += 1

        if has_coupon:
            row['coupon_breakdown'][coupon_code] += 1
        else:
            row['no_coupon_orders'] += 1

        daily_orders.append({
            'order_number': order.order_number,
            'game_name': order.game.name if order.game else 'Servicio',
            'package_name': order.package.name if order.package else 'Paquete',
            'status_label': order.status_label,
            'status_class': order.status_class,
            'amount': amount_value,
            'discount_amount': discount_value,
            'coupon_code': coupon_code,
            'coupon_label': coupon_code or 'Sin cupón',
            'customer_label': (order.player_id or order.email or order.phone or 'Sin dato'),
            'created_at_ve': created_at_ve,
        })

    package_stats = []
    for row in package_rows.values():
        coupons = [
            {'code': code, 'count': count}
            for code, count in sorted(row['coupon_breakdown'].items(), key=lambda item: (-item[1], item[0]))
        ]
        package_stats.append({
            'game_name': row['game_name'],
            'package_name': row['package_name'],
            'game_position': row['game_position'],
            'package_sort_order': row['package_sort_order'],
            'total_orders': row['total_orders'],
            'sold_orders': row['sold_orders'],
            'pending_orders': row['pending_orders'],
            'revenue': row['revenue'],
            'discount_total': row['discount_total'],
            'coupon_breakdown': coupons,
            'no_coupon_orders': row['no_coupon_orders'],
        })

    package_stats.sort(key=lambda row: (
        row['game_position'],
        _sort_text(row['game_name']),
        row['package_sort_order'],
        _sort_text(row['package_name']),
    ))

    coupon_totals = defaultdict(int)
    no_coupon_total = 0
    for row in package_stats:
        for coupon in row['coupon_breakdown']:
            coupon_totals[coupon['code']] += coupon['count']
        no_coupon_total += row['no_coupon_orders']

    coupon_summary = [
        {'code': code, 'count': count}
        for code, count in sorted(coupon_totals.items(), key=lambda item: (-item[1], item[0]))
    ]

    selected_summary = daily_stats[selected_date.isoformat()]
    daily_history = [daily_stats[day.isoformat()] for day in reversed(history_days)]

    return render_template(
        'admin/stats.html',
        today=today,
        selected_date=selected_date,
        selected_service=selected_service,
        selected_service_id=selected_service.id if selected_service else None,
        service_options=service_options,
        daily_history=daily_history,
        selected_summary=selected_summary,
        package_stats=package_stats,
        coupon_summary=coupon_summary,
        no_coupon_total=no_coupon_total,
        daily_orders=daily_orders,
    )
