import hmac
import os
import json
import re
import requests
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from functools import wraps
from flask import (
    Blueprint, render_template, request, redirect, Response,
    url_for, flash, session, current_app, jsonify, abort
)
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import or_, false, func
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename
from ..models import (
    db, AdminUser, Game, Package, Category, Order, User, UserLinkedId, ProfileAward, UserBadge,
    Pin, Affiliate, AffiliateCommission, PaymentMethod, Setting, Discount,
    OrderMiniGameOpportunity, PlayerPoints, PointsPrizeMapping, PointsSpinLog, AccountPointsLog,
    PointsRedeemOption, PointsRedeemLog,
    RevendedoresCatalogItem, RevendedoresItemMapping, RevendedoresMappingItem, GiftCode,
    CentralOneCatalogItem, CentralOneItemMapping, CentralOneMappingItem, PackageDeliveryPlan,
    AffiliateWithdrawal, MiniCourseVideo, MiniNotification, MiniRank, MiniVideo, MiniViewTier, MiniPayoutMethod,
    PromoAccumulatedAward, PromoAccumulatedLevel, PromoAccumulatedOrderLog,
    PromoRaffleConfig, PromoRaffleWinner, PromoRaffleEntry,
    PromoGuessConfig, PromoGuessRound, PromoGuessWinner, PromoGuessAttempt,
    PromoHordeConfig, PromoHordeWinner, SiteNotification, OrderNotification, RankingBoard,
)
from ..utils.availability import format_hour, get_manual_schedule
from ..utils import google_login
from ..utils.profiles import clean_text, delete_media, get_or_create_profile, get_profile, reset_profile, save_media
from ..utils import account_points, banner_lite, page_bg
from ..utils import profile_social
from ..utils import profile_xp
from ..utils.player_field import PLAYER_INPUT_TYPES, clean_value, field_kind
from ..utils.locks import admin_login_locked, check_rate_limit, client_ip, register_admin_login_failure, remember_admin_device
from ..utils.promos import (
    ACC_DEFAULT_MAX_PRIZES_PER_DAY, ACC_DEFAULT_MAX_PRIZES_PER_ID_PER_DAY, ACC_ENABLED_KEY,
    ACC_MAX_PRIZES_PER_DAY_KEY, ACC_MAX_PRIZES_PER_ID_PER_DAY_KEY,
    HORDE_CHARACTER_MODES, HORDE_CHARACTER_SLOTS, get_accumulated_safety_overview,
    get_horde_characters, horde_character_keys,
)
from ..utils.gift_codes import create_batch as create_gift_batch, format_code as format_gift_code
from ..utils.mini_influencers import (
    award_rank_bonus,
    approve_withdrawal as approve_mini_withdrawal,
    attach_withdrawal_proof,
    get_rank_progress,
    notify_mini,
    reject_withdrawal as reject_mini_withdrawal,
    review_mini_video,
    suggested_reward_for_views,
)
from ..utils.timezone import format_ve, now_ve, now_ve_naive, to_ve, ve_day_start_utc_naive, today_ve_str
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
from ..utils.order_processing import (
    PROVIDER_LABELS, approve_order, delivery_reset_blocker, force_approve_order_manually, get_revendedores_env,
    process_affiliate_commission, process_order_delivery,
)
from ..utils.central_one import (
    CentralOneError, CentralOneUnknown, attach_delivered_codes, fetch_balance as fetch_central_one_balance,
    get_centralone_env, probe_codes_permission, sync_catalog as sync_central_one_catalog,
)
from ..models import PushSubscription, PACKAGE_SECTION_LABELS
from ..utils.push_notifications import is_push_configured, send_push_broadcast, send_push_to_order_subscribers_async
from ..utils.site_notifications import BODY_MAX, TITLE_MAX, clean_link
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
    ACCOUNT_KINDS,
    attach_matching_orders_to_customer,
    extract_customer_identifier_for_game,
    get_or_create_scoped_customer,
    linked_orders,
    sync_env_admin_user,
    validate_email_address,
    validate_new_password,
    validate_username,
)
from ..utils.payment_verification import (
    apply_underpaid_verification,
    clear_pabilo_verification_state,
    normalize_reference_last5,
    payment_method_uses_payer_identity_verification,
    recompute_order_payment_amount,
    stamp_verified_payment,
    underpaid_payment_note,
    verify_order_payment,
)

admin_bp = Blueprint('admin_bp', __name__)

HOUSEKEEPING_ORDER_RETENTION_DAYS = 60  # ~2 months
HOUSEKEEPING_PIN_RETENTION_DAYS = 30
HOUSEKEEPING_INTERVAL_HOURS = 6
_last_housekeeping_run = None

ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
ALLOWED_VIDEO_EXT = {'mp4', 'webm', 'mov', 'm4v'}

# Fondos opcionales por ítem del menú lateral (ver Configuración General >
# Fondos de los accesos del menú). Mismo Setting key/value que el resto de
# imágenes de branding — (clave, descripción, etiqueta para el form).
DRAWER_BG_ITEMS = [
    ('drawer_bg_recarga', 'Fondo del acceso "Recarga Acumulada"', 'Recarga Acumulada'),
    ('drawer_bg_sorteo', 'Fondo del acceso "Sorteo Diario"', 'Sorteo Diario'),
    ('drawer_bg_adivina', 'Fondo del acceso "Adivina el Número"', 'Adivina el Número'),
    ('drawer_bg_hordas', 'Fondo del acceso "Hordas de Diamantes"', 'Hordas de Diamantes'),
    ('drawer_bg_torneos', 'Fondo del acceso "Torneos"', 'Torneos'),
    ('drawer_bg_canjear', 'Fondo del acceso "Canjear código"', 'Canjear código'),
    ('drawer_bg_mini', 'Fondo del acceso "Soy mini influencer"', 'Soy mini influencer'),
    ('drawer_bg_login', 'Fondo del acceso "Iniciar sesión"', 'Iniciar sesión'),
]
PROTECTED_CATEGORY_SLUGS = {'juegos', 'tarjetas', 'wallet'}
GAME_PLAYER_INPUT_TYPES = set(PLAYER_INPUT_TYPES)


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

    kind = field_kind(order.game)
    if kind == 'none':      # tarjetas: no hay dato de la cuenta
        return ''
    return clean_value(kind, value)


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
# La foto de soporte se ve en un circulo pequeno: 512px sobra y pesa una fraccion.
AVATAR_IMAGE_DIMENSION = 512
# Los fondos de la web van detrás de un velo oscuro y la tienda es de celular: 1280 px de lado largo sobra y pesa una fracción.
BACKGROUND_IMAGE_DIMENSION = 1280


def _compress_and_save_image(file, dest_path, ext, max_dimension=MAX_IMAGE_DIMENSION):
    """Redimensiona y comprime antes de guardar. Estas imágenes (logo, fondo,
    banners, juegos) se sirven a todos los visitantes, así que una foto de
    celular sin comprimir (3-8 MB) hace lenta la primera carga. GIF se guarda
    tal cual para no romper animaciones; ante cualquier error se guarda el
    archivo original como antes."""
    if ext == 'gif':
        file.save(dest_path)
        return dest_path

    try:
        from PIL import Image, ImageOps

        image = Image.open(file.stream)
        image = ImageOps.exif_transpose(image)
        if max(image.size) > max_dimension:
            image.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

        if ext in ('jpg', 'jpeg'):
            if image.mode not in ('RGB', 'L'):
                image = image.convert('RGB')
            image.save(dest_path, format='JPEG', quality=82, optimize=True, progressive=True)
        elif ext == 'webp':
            if image.mode == 'P':
                image = image.convert('RGBA')
            image.save(dest_path, format='WEBP', quality=82, method=6)
        else:  # png
            if not _has_real_transparency(image):
                # Un PNG sin transparencia (una foto o un diseño de fondo) pesa 10-40 veces más que en WebP y no gana nada: se guarda
                # como WebP (el fondo de la portada llegó a pesar 1,2 MB en PNG y 30 KB en WebP). Con transparencia real se queda PNG.
                webp_path = os.path.splitext(dest_path)[0] + '.webp'
                image.convert('RGB').save(webp_path, format='WEBP', quality=82, method=6)
                return webp_path
            image.save(dest_path, format='PNG', optimize=True)
    except Exception:
        file.stream.seek(0)
        file.save(dest_path)
    return dest_path


def _has_real_transparency(image):
    """True si la imagen tiene píxeles de verdad transparentes (no solo un canal alfa lleno de 255)."""
    try:
        if image.mode in ('RGBA', 'LA'):
            return image.getchannel('A').getextrema()[0] < 250
        if image.mode == 'P' and 'transparency' in image.info:
            return True
        if image.mode == 'PA':
            return True
    except Exception:
        return True
    return False


def save_image(file, subfolder='', max_dimension=MAX_IMAGE_DIMENSION):
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
    final_path = _compress_and_save_image(file, os.path.join(folder, filename), ext, max_dimension)
    filename = os.path.basename(final_path or os.path.join(folder, filename))   # puede cambiar de .png a .webp
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


# Banner promocional animado: solo MP4 y liviano (el banner se descarga en la primera visita de TODOS los clientes).
PROMO_BANNER_VIDEO_MAX_MB = 8
PROMO_BANNER_VIDEO_MAX_BYTES = PROMO_BANNER_VIDEO_MAX_MB * 1024 * 1024


def is_promo_banner_video(filename):
    """True si lo guardado/subido es el video del banner (se reconoce por la extensión .mp4)."""
    return (filename or '').strip().lower().endswith('.mp4')


def save_promo_banner_video(file):
    """Guarda el MP4 del banner en la carpeta de branding. Devuelve (ruta, mensaje_de_error): si hay error, la ruta es None.
    Se comprueba el tamaño y que el archivo sea de verdad un MP4 (la caja `ftyp` al inicio), no solo que termine en .mp4."""
    if not file or not is_promo_banner_video(file.filename):
        return None, 'El video del banner debe ser un archivo MP4.'
    stream = file.stream
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    if size <= 0:
        return None, 'El video está vacío.'
    if size > PROMO_BANNER_VIDEO_MAX_BYTES:
        return None, f'El video pesa {size / (1024 * 1024):.1f} MB: el máximo es {PROMO_BANNER_VIDEO_MAX_MB} MB. Usa uno más corto o más liviano.'
    head = stream.read(12)
    stream.seek(0)
    if len(head) < 12 or head[4:8] != b'ftyp':
        return None, 'Ese archivo no es un MP4 válido.'
    return save_video(file, 'branding'), ''


def _save_page_background(file):
    """Fondo de pantalla (la web o una página especial): una imagen o un VIDEO MP4/WEBM de hasta 1 MB. Devuelve la ruta guardada, o None (y avisa el motivo)."""
    if page_bg.wants_video(file):
        saved, error = page_bg.save_background_video(file, 'branding')
        if error:
            flash(f'Fondo en video: {error}', 'danger')
        return saved
    return save_image(file, 'branding', BACKGROUND_IMAGE_DIMENSION)


def order_supports_delivery_proof(order):
    """¿Se puede adjuntar la foto del comprobante al aprobar? En todas las
    recargas por ID (también las automáticas: cuando la API falla y se hace
    a mano, antes no aparecía la opción). Las tarjetas/códigos entregan un
    PIN y no la usan."""
    if not order or not order.package or not order.game:
        return False
    category_slug = (order.game.category.slug if order.game and order.game.category else '').lower()
    return category_slug != 'tarjetas'


def cleanup_old_orders():
    """Limpieza de las órdenes de más de HOUSEKEEPING_ORDER_RETENTION_DAYS.

    Las órdenes con un pago verificado (o entregadas) ya NO se borran: son el
    registro de qué pagos ya se usaron. Antes se borraban todas, y a los 60
    días la referencia y el id de Pabilo de ese pago quedaban libres para
    cobrar una orden nueva con el mismo pago viejo. De esas solo se borra lo
    que pesa: las fotos de los comprobantes y la respuesta cruda de Pabilo.
    Las que sí se borran son las abandonadas (pendientes o rechazadas sin
    ningún pago confirmado).
    """
    threshold = datetime.utcnow() - timedelta(days=HOUSEKEEPING_ORDER_RETENTION_DAYS)
    changed = False

    heavy_orders = Order.query.filter(
        Order.created_at < threshold,
        or_(
            Order.payment_capture.isnot(None),
            Order.remainder_capture.isnot(None),
            Order.payment_verification_raw_response.isnot(None),
        ),
    ).all()
    for order in heavy_orders:
        for field in ('payment_capture', 'remainder_capture'):
            if getattr(order, field):
                delete_uploaded_file(getattr(order, field))
                setattr(order, field, None)
        order.payment_verification_raw_response = None
        changed = True

    abandoned_orders = Order.query.filter(
        Order.created_at < threshold,
        Order.status.in_(['pending', 'rejected']),
        Order.payment_verified_at.is_(None),
        or_(Order.payment_verification_id.is_(None), Order.payment_verification_id == ''),
        or_(Order.remainder_verification_ids.is_(None), Order.remainder_verification_ids == ''),
        Order.binance_tx_id.is_(None),
        or_(Order.awaiting_payment_completion.is_(None), Order.awaiting_payment_completion.is_(False)),
        or_(Order.payment_method.is_(None), Order.payment_method != 'prize'),
    ).all()
    for order in abandoned_orders:
        # Las oportunidades de minijuego referencian la orden, así que hay que
        # borrarlas primero o el DELETE falla por la restricción de clave ajena.
        for opp in OrderMiniGameOpportunity.query.filter_by(order_id=order.id).all():
            db.session.delete(opp)
        PromoAccumulatedOrderLog.query.filter_by(order_id=order.id).delete()
        db.session.delete(order)
        changed = True

    if changed:
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
    # Red de seguridad para TODO el panel: aunque alguna ruta se agregue sin
    # @login_required, aquí se exige sesión antes de ejecutar la vista. Sin
    # sesión -> al login; con sesión que no es de admin (ej. un cliente) -> fuera.
    if not current_user.is_authenticated:
        return redirect(url_for('admin_bp.login', next=request.url))
    if current_user.__class__.__name__ != 'AdminUser':
        flash('Esta sección es solo para administradores.', 'warning')
        return redirect(url_for('main_bp.index'))
    return None


# ─── Auth ────────────────────────────────────────────────────────────────────

ADMIN_LOGIN_MAX_ATTEMPTS = 10
ADMIN_LOGIN_WINDOW_SECONDS = 15 * 60


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

        if not check_rate_limit(f'admin_login:{client_ip()}', ADMIN_LOGIN_MAX_ATTEMPTS, ADMIN_LOGIN_WINDOW_SECONDS):
            flash('Demasiados intentos. Espera 15 minutos antes de volver a intentar.', 'danger')
            return render_template('admin/login.html'), 429

        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '').strip()

        valid_identifiers = {env_admin_username.lower()}
        if env_admin_email:
            valid_identifiers.add(env_admin_email.lower())

        is_admin_identifier = identifier.lower() in valid_identifiers
        # Freno general (todas las IP juntas) para quien prueba claves del admin; no cuenta los errores de otros usuarios.
        if is_admin_identifier and admin_login_locked():
            flash('Demasiados intentos fallidos con la clave del administrador. Espera 15 minutos.', 'danger')
            return render_template('admin/login.html'), 429

        password_ok = hmac.compare_digest(password.encode('utf-8'), env_admin_password.encode('utf-8'))
        if not is_admin_identifier or not password_ok:
            if is_admin_identifier:
                register_admin_login_failure()
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
            return remember_admin_device(redirect(url_for('admin_bp.dashboard')))

        flash('No se pudo iniciar sesión de administrador.', 'danger')
    return render_template('admin/login.html')


@admin_bp.route('/logout')
@login_required
def logout():
    from ..utils.official_profile import clear_admin_return
    clear_admin_return()
    logout_user()
    return redirect(url_for('admin_bp.login'))


@admin_bp.route('/perfil-oficial', methods=['POST'])
def official_profile_enter():
    """Botón "Perfil" del menú: el administrador entra a la cuenta oficial (3srecargas) para ver y editar su perfil sin escribir ninguna
    clave. Su sesión del panel se cambia por la de esa cuenta y se anota a quién volver (botón "Volver al panel de admin")."""
    from ..utils.official_profile import clear_admin_return, ensure_official_profile, remember_admin_return
    user = ensure_official_profile()
    if user is None or not user.is_active:
        flash('No se pudo abrir el perfil oficial: la cuenta no existe o está desactivada (Admin → Cuentas).', 'danger')
        return redirect(url_for('admin_bp.dashboard'))
    session.pop(STOCK_PINS_SESSION_KEY, None)
    remember_admin_return(current_user)
    if not login_user(user):
        clear_admin_return()
        flash('No se pudo abrir el perfil oficial.', 'danger')
        return redirect(url_for('admin_bp.dashboard'))
    return redirect(url_for('profiles_bp.edit_profile'))


@admin_bp.route('/maintenance/toggle', methods=['POST'])
@login_required
def maintenance_toggle():
    """Prende/apaga la pantalla de mantenimiento para toda la tienda. El
    panel admin sigue funcionando siempre (ver _check_maintenance_mode en
    app/__init__.py), así que apagarlo de nuevo no depende de esto."""
    setting = Setting.query.filter_by(key='maintenance_mode').first()
    currently_on = bool(setting and setting.value == 'true')
    new_value = 'false' if currently_on else 'true'

    if not setting:
        setting = Setting(key='maintenance_mode', value=new_value, description='Pantalla de mantenimiento para toda la tienda.')
        db.session.add(setting)
    else:
        setting.value = new_value
    db.session.commit()

    flash('Modo mantenimiento desactivado. La tienda ya es visible.' if currently_on
          else 'Modo mantenimiento activado. Los visitantes ven la pantalla de mantenimiento; el panel admin sigue funcionando normal.',
          'success')
    return redirect(request.referrer or url_for('admin_bp.dashboard'))


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
            _delete_package_delivery_config(package_ids)
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
        package_sections=PACKAGE_SECTION_LABELS,
    )


PACKAGE_ANNOUNCEMENT_TYPES = {'', 'one_time_purchase', 'redeem_code'}


def _normalize_package_announcement_type(raw_value):
    value = (raw_value or '').strip()
    return value if value in PACKAGE_ANNOUNCEMENT_TYPES else ''


def _normalize_package_section(raw_value):
    """Apartado elegido en el formulario del paquete ('main' | 'pass' | 'card'); cualquier otra cosa = no se eligió (None)."""
    from ..models import PACKAGE_SECTION_LABELS
    value = (raw_value or '').strip()
    return value if value in PACKAGE_SECTION_LABELS else None


def _normalize_package_points(raw_value):
    """Campo "puntos" del paquete: vacío = usar el rate global de puntos.

    Devuelve None para vacío/basura, que es lo que la tarjeta y el abono
    interpretan como "calcúlalo con el rate"."""
    value = (raw_value or '').strip()
    if not value:
        return None
    try:
        points = int(float(value))
    except (TypeError, ValueError):
        return None
    return points if points >= 0 else None


@admin_bp.route('/packages/add', methods=['POST'])
@login_required
def package_add():
    game_id = request.form.get('game_id')
    name = request.form.get('name', '').strip()
    price = request.form.get('price', '0').strip()
    usd_price_raw = request.form.get('usd_price', '').strip()
    bs_price_raw = request.form.get('bs_price', '').strip()
    cost_usd_raw = request.form.get('cost_usd', '').strip()
    description = request.form.get('description', '').strip()
    is_automated = bool(request.form.get('is_automated'))
    sort_order = int(request.form.get('sort_order', 100))
    announcement_type = _normalize_package_announcement_type(request.form.get('announcement_type'))
    section = _normalize_package_section(request.form.get('section'))
    show_support_fallback = bool(request.form.get('show_support_fallback'))

    if not game_id or not name or not price:
        flash('Juego, nombre y precio son obligatorios.', 'danger')
        return redirect(url_for('admin_bp.packages'))

    if section is None:
        flash('Elige en qué apartado va el paquete (Moneda principal, Pases o Tarjetas).', 'danger')
        return redirect(url_for('admin_bp.packages', game_id=game_id) if str(game_id).isdigit() else url_for('admin_bp.packages'))

    try:
        base_price = float(price)
        usd_price = float(usd_price_raw) if usd_price_raw else None
        bs_price = float(bs_price_raw) if bs_price_raw else None
        cost_usd = float(cost_usd_raw) if cost_usd_raw else None
    except ValueError:
        flash('Los precios deben ser números válidos.', 'danger')
        return redirect(url_for('admin_bp.packages'))

    if base_price <= 0 or (usd_price is not None and usd_price <= 0) or (bs_price is not None and bs_price <= 0):
        flash('Los precios deben ser mayores a 0.', 'danger')
        return redirect(url_for('admin_bp.packages'))

    if cost_usd is not None and cost_usd < 0:
        flash('El costo no puede ser negativo.', 'danger')
        return redirect(url_for('admin_bp.packages'))

    image = save_image(request.files.get('image'), 'packages')
    pkg = Package(
        game_id=int(game_id), name=name, price=base_price, usd_price=usd_price,
        bs_price=bs_price, cost_usd=cost_usd,
        description=description, is_automated=is_automated,
        sort_order=sort_order, image=image, announcement_type=announcement_type or None,
        points_reward=_normalize_package_points(request.form.get('points_reward')),
        show_support_fallback=show_support_fallback, section=section,
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
    bs_price_raw = request.form.get('bs_price', '')
    cost_usd_raw = request.form.get('cost_usd', '')
    try:
        pkg.price = float(price_raw)
        pkg.usd_price = float(usd_price_raw) if str(usd_price_raw).strip() else None
        pkg.bs_price = float(bs_price_raw) if str(bs_price_raw).strip() else None
        pkg.cost_usd = float(cost_usd_raw) if str(cost_usd_raw).strip() else None
    except ValueError:
        flash('Los precios deben ser números válidos.', 'danger')
        return redirect(url_for('admin_bp.packages', game_id=return_game_id))

    if (
        float(pkg.price or 0) <= 0
        or (pkg.usd_price is not None and float(pkg.usd_price) <= 0)
        or (pkg.bs_price is not None and float(pkg.bs_price) <= 0)
    ):
        flash('Los precios deben ser mayores a 0.', 'danger')
        return redirect(url_for('admin_bp.packages', game_id=return_game_id))

    if pkg.cost_usd is not None and float(pkg.cost_usd) < 0:
        flash('El costo no puede ser negativo.', 'danger')
        return redirect(url_for('admin_bp.packages', game_id=return_game_id))

    pkg.description = request.form.get('description', pkg.description or '').strip()
    pkg.is_automated = bool(request.form.get('is_automated'))
    pkg.sort_order = int(request.form.get('sort_order', pkg.sort_order))
    pkg.is_active = bool(request.form.get('is_active'))
    pkg.announcement_type = _normalize_package_announcement_type(request.form.get('announcement_type')) or None
    pkg.points_reward = _normalize_package_points(request.form.get('points_reward'))
    pkg.show_support_fallback = bool(request.form.get('show_support_fallback'))
    # El apartado se pregunta siempre al editar; si por algún motivo no llega uno válido se conserva el que tenía.
    pkg.section = _normalize_package_section(request.form.get('section')) or pkg.section or 'main'

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
        _delete_package_delivery_config([pkg.id])
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
    game_id=None,
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

    try:
        game_id = int(game_id) if game_id not in (None, '') else None
    except (TypeError, ValueError):
        game_id = None

    # "Por entregar" no es un estado guardado sino una vista: órdenes cuyo
    # pago ya se verificó solo (Binance/Pabilo) pero cuyo producto se recarga
    # a mano, así que siguen pendientes esperando que un admin las haga.
    # Antes se perdían entre el resto de las pendientes y el cliente terminaba
    # reclamando por WhatsApp.
    #
    # "Pendientes" sí vuelve a ser literal (todas las 'pending'): compartir el
    # criterio de 'to_deliver' escondía las pendientes cuyo pago aún no se
    # verificó — salían en "Todas" con su badge Pendiente pero la pestaña
    # aparecía vacía, que es justo donde el admin las va a buscar.
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

    if game_id:
        query = query.filter(Order.game_id == game_id)

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
    game_id = request.args.get('game_id', type=int)
    query = Order.query.order_by(Order.created_at.desc())
    query = _apply_order_filters(
        query,
        status_filter=status_filter,
        search_query=search_query,
        date_from=date_from,
        date_to=date_to,
        package_id=package_id,
        service_id=service_id,
        game_id=game_id,
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
    games = Game.query.order_by(Game.name.asc()).all()
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
        game_id=game_id,
        games=games,
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
        pending_count=Order.query.filter(Order.status == 'pending').count(),
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
    game_id = request.args.get('game_id', type=int)
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
        game_id=game_id,
    )
    if since_id:
        query = query.filter(Order.id > since_id)

    newest = query.order_by(Order.id.desc()).limit(20).all()
    newest.reverse()

    return jsonify({'ok': True, 'orders': [_order_row_payload(o) for o in newest]})


def _order_row_payload(o):
    return {
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
        'payment_verified': bool(o.payment_verified_at),
        'created_at': format_ve(o.created_at, '%d/%m/%Y %H:%M'),
        'automation_response': o.automation_response or '',
        'pin_delivered': o.pin_delivered or '',
        'can_send_delivery_proof': order_supports_delivery_proof(o),
    }


@admin_bp.route('/orders/refresh')
@login_required
def orders_refresh():
    """Estado fresco de las órdenes que la tabla tiene en pantalla.

    El polling de /orders/latest solo AÑADE filas nuevas (nacen 'pending');
    cuando la recarga automática las completa segundos después, la fila
    quedaba con el badge "Pendiente" para siempre y el admin veía muchas
    pendientes que el filtro (que sí consulta la DB) no encontraba."""
    ids = []
    for part in (request.args.get('ids') or '').split(','):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    ids = ids[:60]
    if not ids:
        return jsonify({'ok': True, 'orders': []})

    rows = Order.query.filter(Order.id.in_(ids)).all()
    return jsonify({'ok': True, 'orders': [_order_row_payload(o) for o in rows]})


@admin_bp.route('/orders/<int:order_id>')
@login_required
def order_detail(order_id):
    from .verify import verifiable_game_ids

    order = Order.query.get_or_404(order_id)
    payment_method_config = PaymentMethod.query.filter_by(code=(order.payment_method or '').strip().lower()).first()
    same_game_packages = Package.query.filter_by(
        game_id=order.game_id, is_active=True
    ).order_by(Package.sort_order.asc(), Package.id.asc()).all()

    # Pago restante: solo las referencias que de verdad se sumaron. Las
    # órdenes de antes de existir esa lista guardaban solo la última.
    remainder_refs = [r for r in (order.remainder_references or '').split(',') if r.strip()]
    if not remainder_refs and order.remainder_reference and not order.awaiting_payment_completion and order.paid_amount_bs:
        remainder_refs = [order.remainder_reference]
    from ..utils.payment_verification import _get_expected_order_amount, remainders_matching_first_payment
    remainder_missing = None
    if order.awaiting_payment_completion:
        expected, error = _get_expected_order_amount(order)
        if not error and expected is not None:
            remainder_missing = (expected - Decimal(str(order.paid_amount_bs or 0))).quantize(Decimal('0.01'))

    # Qué proveedor entregó (o está entregando) la orden, y si fue por respaldo.
    delivery_state = _json_dict(order.automation_response)
    delivery_code = str(delivery_state.get('provider') or '').strip()
    if delivery_code not in PROVIDER_LABELS and delivery_state.get('source') == 'revendedores_api':
        delivery_code = 'revendedores'
    delivery_info = None
    if delivery_code in PROVIDER_LABELS:
        costs = [Decimal(str(s['cost_usd'])) for s in delivery_state.get('steps') or []
                 if isinstance(s, dict) and s.get('cost_usd') and s.get('success')]
        delivery_info = {
            'provider': PROVIDER_LABELS[delivery_code],
            'fallback_from': PROVIDER_LABELS.get(delivery_state.get('fallback_from')),
            'cost': str(sum(costs, Decimal('0')).quantize(Decimal('0.0001'))) if costs else '',
        }

    # Central One ya entregó pero el código nunca llegó a la orden (llave sin el
    # permiso «ver códigos», o una orden de antes de que la tienda los pidiera).
    can_fetch_codes = bool(
        delivery_code == 'centralone'
        and any(
            isinstance(s, dict) and s.get('remote_order_id') and not s.get('codes_saved')
            and (s.get('awaiting_codes') or 'código(s)' in (order.notes or ''))
            for s in delivery_state.get('steps') or []
        )
    )

    # Órdenes de antes del arreglo: un "pago restante" que en realidad era el
    # mismo primer pago se sumó dos veces. Se calcula lo que de verdad pagó.
    remainder_repeats = remainders_matching_first_payment(order, remainder_refs)
    repeat_real_paid = repeat_real_missing = None
    if remainder_repeats and order.paid_amount_bs:
        counted_twice = sum(
            (Decimal(amount) for amount, ref in re.findall(
                r'\[Pabilo\] Pago restante verificado: Bs ([\d.]+) \(ref: ([^)]+)\)', order.notes or '')
             if ref.strip() in remainder_repeats),
            Decimal('0'),
        )
        if counted_twice:
            repeat_real_paid = (Decimal(str(order.paid_amount_bs)) - counted_twice).quantize(Decimal('0.01'))
            expected, error = _get_expected_order_amount(order)
            if not error and expected is not None:
                repeat_real_missing = (expected - repeat_real_paid).quantize(Decimal('0.01'))

    return render_template(
        'admin/order_detail.html',
        order=order,
        payment_method_config=payment_method_config,
        can_send_delivery_proof=order_supports_delivery_proof(order),
        can_upload_delivery_proof=order.status != 'rejected',
        delivery_info=delivery_info,
        can_fetch_codes=can_fetch_codes,
        order_messages=order.order_notifications[:10],
        push_available=is_push_configured(),
        remainder_refs=remainder_refs,
        remainder_missing=remainder_missing,
        remainder_repeats=remainder_repeats,
        repeat_real_paid=repeat_real_paid,
        repeat_real_missing=repeat_real_missing,
        same_game_packages=same_game_packages,
        can_verify_player=order.game_id in verifiable_game_ids(),
    )


@admin_bp.route('/orders/<int:order_id>/delivery-proof', methods=['POST'])
@login_required
def order_upload_delivery_proof(order_id):
    """Sube o reemplaza la foto del comprobante de la recarga en cualquier
    orden (también en las automáticas o las forzadas a mano, donde antes no
    había dónde subirla)."""
    order = Order.query.get_or_404(order_id)
    redirect_target = url_for('admin_bp.order_detail', order_id=order.id)
    file = request.files.get('delivery_proof')
    if not file or not file.filename:
        flash('Elige una imagen para subir.', 'warning')
        return redirect(redirect_target)
    if not allowed_file(file.filename):
        flash('El comprobante debe ser una imagen PNG, JPG, JPEG, GIF o WEBP.', 'danger')
        return redirect(redirect_target)
    new_path = save_image(file, 'delivery_proofs')
    old_path = order.delivery_proof
    order.delivery_proof = new_path
    db.session.commit()
    if old_path and old_path != new_path:
        delete_uploaded_file(old_path)
    flash('Foto del comprobante guardada en la orden.', 'success')
    return redirect(redirect_target)


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

    if updated_player_id != original_player_id:
        blocker = delivery_reset_blocker(order)
        if blocker:
            flash(f'No se puede cambiar el ID: {blocker}. Revisa la orden con el proveedor; si ya se resolvió por '
                  'fuera, usa "Forzar aprobación manual" o recházala.', 'danger')
            return redirect(redirect_target)

    try:
        if updated_player_id != original_player_id:
            order.player_id = updated_player_id
            order.player_nickname = None
            order.automation_response = None
            order.updated_at = datetime.utcnow()

            note = f'[Admin] {order.game.player_label} actualizado de {original_player_id or "(vacío)"} a {updated_player_id}.'
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
            flash(f'{order.game.player_label} actualizado correctamente.', 'success')

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


@admin_bp.route('/orders/<int:order_id>/verify-player', methods=['POST'])
@login_required
def order_verify_player_id(order_id):
    """Consulta el nombre real detrás del ID de la orden antes de aprobarla,
    por el mismo camino que usa la tienda (mismo endpoint que gift_code_verify_id)."""
    from .verify import verify_player_nick

    order = Order.query.get_or_404(order_id)
    player_id = (order.player_id or '').strip()
    if not player_id:
        return jsonify({'ok': False, 'error': 'Esta orden no tiene un ID de jugador.'}), 400

    payload, status = verify_player_nick(player_id, str(order.game_id))
    nick = (payload.get('nick') or '').strip()[:120] if payload.get('ok') else ''
    if nick and nick != (order.player_nickname or ''):
        order.player_nickname = nick
        db.session.commit()
    return jsonify(payload), status


@admin_bp.route('/orders/<int:order_id>/package', methods=['POST'])
@login_required
def order_update_package(order_id):
    """Corrige el paquete de una orden pendiente (p.ej. el cliente pidió 341
    diamantes pero quería 110). Solo se permite cambiar a otro paquete del
    MISMO juego, y el monto de la orden se ajusta al precio del paquete
    nuevo para que la verificación de pago siga comparando contra el monto
    correcto."""
    order = Order.query.get_or_404(order_id)
    redirect_target = url_for('admin_bp.order_detail', order_id=order.id)

    if order.status != 'pending':
        flash('Solo puedes cambiar el paquete en órdenes pendientes.', 'warning')
        return redirect(redirect_target)

    try:
        new_package_id = int(request.form.get('package_id', 0))
    except (TypeError, ValueError):
        new_package_id = 0

    if not new_package_id:
        flash('Elige un paquete válido.', 'danger')
        return redirect(redirect_target)

    new_package = Package.query.get(new_package_id)
    if not new_package or new_package.game_id != order.game_id:
        flash('Ese paquete no pertenece al mismo juego de la orden.', 'danger')
        return redirect(redirect_target)

    if new_package.id == order.package_id:
        flash('El paquete no cambió.', 'info')
        return redirect(redirect_target)

    blocker = delivery_reset_blocker(order)
    if blocker:
        flash(f'No se puede cambiar el paquete: {blocker}.', 'danger')
        return redirect(redirect_target)

    try:
        old_package_name = order.package.name if order.package else '?'
        old_amount = order.amount
        order.package_id = new_package.id
        order.amount = new_package.price
        recompute_order_payment_amount(order)
        order.updated_at = datetime.utcnow()

        note = (
            f'[Admin] Paquete corregido de "{old_package_name}" (${old_amount}) '
            f'a "{new_package.name}" (${new_package.price}).'
        )
        existing_notes = order.notes or ''
        order.notes = (existing_notes + '\n' + note).strip()

        db.session.commit()
        flash(f'Paquete actualizado a "{new_package.name}".', 'success')
        return redirect(redirect_target)
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            'Error al actualizar el paquete de la orden %s',
            getattr(order, 'id', None),
        )
        flash('Ocurrió un error al actualizar el paquete.', 'danger')
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
        # Si la re-verificación falla, se vuelve a esto: antes el pago ya
        # verificado quedaba borrado de la orden (y su id de Pabilo libre
        # para usarse en otra orden) solo porque el reintento no funcionó.
        previous_verification = {
            'payment_verified_at': order.payment_verified_at,
            'payment_verification_id': order.payment_verification_id,
            'payment_reference': order.payment_reference,
            'payment_reference_last5': order.payment_reference_last5,
        }
        clear_pabilo_verification_state(order)
        if not uses_payer_identity:
            order.payment_reference = reference
            order.payment_reference_last5 = normalize_reference_last5(reference)
        order.updated_at = datetime.utcnow()

        verification = verify_order_payment(order, force_reference=force_reference)
        order.payment_verification_attempts = int(order.payment_verification_attempts or 0) + 1
        order.payment_last_verification_at = datetime.utcnow()
        response_raw = verification.get('response')
        if response_raw is not None:
            try:
                order.payment_verification_raw_response = json.dumps(response_raw, default=str)[:20000]
            except Exception:
                pass

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

        if verification.get('underpaid'):
            # El pago existe pero no alcanza: la orden pasa a esperar el pago
            # restante (al cliente le aparece el aviso para completarlo en la
            # página de su orden), sin perder lo que ya hubiera sumado antes.
            covered, missing = apply_underpaid_verification(order, verification)
            if covered:
                note = f'[Admin] Pago re-verificado: con los pagos restantes ya sumados (total Bs {order.paid_amount_bs}) la orden está cubierta.'
                category, message = 'success', 'El pago está completo sumando el pago restante. Ya puedes aprobar la orden.'
            else:
                note = ('[Admin] Re-verificación: '
                        + underpaid_payment_note(order, verification, missing).replace('[Pabilo] ', '', 1)
                        + ' Se le pidió al cliente completar el pago.')
                category = 'warning'
                message = (f'💰 Al pago le faltan Bs {missing}. La orden quedó esperando el pago restante: '
                           'al cliente le aparece el aviso para completarlo en la página de su orden.')
            if note not in (order.notes or ''):
                order.notes = ((order.notes or '') + '\n' + note).strip()
            db.session.commit()
            flash(message, category)
            return redirect(url_for('admin_bp.order_detail', order_id=order.id))

        note = verification.get('message') or 'No se pudo re-verificar el pago en Pabilo.'
        if previous_verification['payment_verified_at'] or previous_verification['payment_verification_id']:
            for field, value in previous_verification.items():
                setattr(order, field, value)
            note += ' Se conservó el pago verificado que ya tenía la orden.'
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
            if order.status in ('approved', 'completed'):
                # Se completó por la vía automática (que no guarda la foto):
                # la foto que subió el admin igual queda en la orden.
                order.delivery_proof = delivery_proof_path
                db.session.commit()
            else:
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


@admin_bp.route('/orders/<int:order_id>/force-approve', methods=['POST'])
@login_required
def order_force_approve(order_id):
    order = Order.query.get_or_404(order_id)
    admin_note = request.form.get('admin_note', '').strip()
    # Foto opcional del comprobante de la recarga hecha por fuera de la web.
    proof_path = None
    proof_file = request.files.get('delivery_proof')
    if proof_file and proof_file.filename:
        if not allowed_file(proof_file.filename):
            flash('El comprobante debe ser una imagen PNG, JPG, JPEG, GIF o WEBP.', 'danger')
            return redirect(url_for('admin_bp.order_detail', order_id=order.id))
        proof_path = save_image(proof_file, 'delivery_proofs')
    try:
        result = force_approve_order_manually(order, admin_note=admin_note)
        if proof_path:
            if result.get('ok'):
                order.delivery_proof = proof_path
                db.session.commit()
            else:
                delete_uploaded_file(proof_path)
        flash(result['message'], result['category'])
    except Exception:
        db.session.rollback()
        if proof_path:
            delete_uploaded_file(proof_path)
        current_app.logger.exception(
            'Error al forzar la aprobación manual de la orden %s',
            getattr(order, 'id', None),
        )
        flash('Ocurrió un error interno al forzar la aprobación. Revisa el log del servidor para el detalle.', 'danger')
    return redirect(url_for('admin_bp.order_detail', order_id=order.id))


@admin_bp.route('/orders/<int:order_id>/reject', methods=['POST'])
@login_required
def order_reject(order_id):
    order = Order.query.get_or_404(order_id)
    if order.status == 'completed':
        # Una orden rechazada deja de reservar su referencia de pago: rechazar
        # una ya entregada dejaba ese mismo pago libre para cobrar otra orden.
        flash('Esta orden ya fue completada (entregada): no se puede rechazar.', 'warning')
        return redirect(url_for('admin_bp.order_detail', order_id=order.id))
    notes = request.form.get('notes', '').strip()
    order.status = 'rejected'
    if notes:
        existing_notes = order.notes or ''
        order.notes = (existing_notes + '\n' + notes).strip()
    order.updated_at = datetime.utcnow()
    db.session.commit()
    try:
        notify_order_rejected(order, order.package, order.game, reason=notes)
    except Exception:
        pass
    flash(f'Orden #{order.order_number} rechazada.', 'warning')
    return redirect(url_for('admin_bp.orders'))


@admin_bp.route('/orders/<int:order_id>/add-note', methods=['POST'])
@login_required
def order_add_note(order_id):
    order = Order.query.get_or_404(order_id)
    note_text = request.form.get('note', '').strip()
    if not note_text:
        flash('Escribe una nota antes de guardar.', 'warning')
        return redirect(url_for('admin_bp.order_detail', order_id=order.id))

    stamp = format_ve(datetime.utcnow(), '%d/%m/%Y %H:%M')
    note = f'[Nota {stamp}] {note_text}'
    existing_notes = order.notes or ''
    order.notes = (existing_notes + '\n' + note).strip()
    order.updated_at = datetime.utcnow()
    db.session.commit()
    flash('Nota guardada.', 'success')
    return redirect(url_for('admin_bp.order_detail', order_id=order.id))


@admin_bp.route('/orders/<int:order_id>/notify', methods=['POST'])
@login_required
def order_notify(order_id):
    """Mensaje del admin para esta orden: sale en la campana de quien hizo la
    compra en ese navegador como "Sobre tu orden #..."."""
    order = Order.query.get_or_404(order_id)
    body = (request.form.get('body') or '').strip()[:BODY_MAX]
    if not body:
        flash('Escribe el mensaje que quieres mandarle al cliente.', 'warning')
        return redirect(url_for('admin_bp.order_detail', order_id=order.id))

    db.session.add(OrderNotification(order_id=order.id, body=body))
    db.session.commit()
    pushed = False
    if request.form.get('as_push') == '1' and is_push_configured():
        send_push_to_order_subscribers_async(
            current_app._get_current_object(), order.id,
            f'Sobre tu orden #{order.order_number}', body[:180], url=f'/order/{order.order_number}',
        )
        pushed = True
    flash('Notificación enviada: el cliente la verá en la campana de la tienda' + (' y por push.' if pushed else '.'), 'success')
    return redirect(url_for('admin_bp.order_detail', order_id=order.id))


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
    'admin_bp.gift_code_verify_id',
}


def get_stock_pins_access_code():
    return (current_app.config.get('STOCK_PINS_ACCESS_CODE') or '').strip()


def _in_admin_area():
    """Los datos del menú lateral del panel (candado, pendientes, mantenimiento) solo se calculan dentro de /admin: antes corrían
    (con sus consultas a la base) en CADA página de la tienda, aunque ningún cliente los use."""
    return request.path.startswith('/admin')


@admin_bp.app_context_processor
def _inject_stock_pins_state():
    """Para pintar el candado en el menú lateral."""
    if not _in_admin_area():
        return {}
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


# Un paquete puede tener cientos de PINs: el inventario se muestra por páginas.
PINS_PAGE_SIZE = 25


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
    pins_total = 0
    page = 1
    total_pages = 1
    page_numbers = [1]
    start_index = end_index = 0

    if package_id:
        selected_package = pin_enabled_query.filter(Package.id == package_id).first()
        if selected_package:
            pins_query = Pin.query.filter_by(package_id=package_id)
            pins_total = pins_query.count()
            page = request.args.get('page', type=int) or 1
            if page < 1:
                page = 1
            total_pages = max(1, (pins_total + PINS_PAGE_SIZE - 1) // PINS_PAGE_SIZE)
            if page > total_pages:
                page = total_pages
            # Los PINs de una misma carga comparten la hora: sin el id como último criterio el orden no sería
            # estable y un PIN podría repetirse o saltarse al pasar de una página a otra.
            pins_list = (
                pins_query
                .order_by(Pin.is_used.asc(), Pin.created_at.asc(), Pin.id.asc())
                .offset((page - 1) * PINS_PAGE_SIZE)
                .limit(PINS_PAGE_SIZE)
                .all()
            )
            page_numbers = list(range(max(1, page - 2), min(total_pages, page + 2) + 1))
            start_index = ((page - 1) * PINS_PAGE_SIZE) + 1 if pins_total else 0
            end_index = min(page * PINS_PAGE_SIZE, pins_total) if pins_total else 0

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
        pins_total=pins_total,
        current_page=page,
        total_pages=total_pages,
        page_numbers=page_numbers,
        start_index=start_index,
        end_index=end_index,
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
    # Se vuelve a la misma página del inventario desde la que se eliminó (si quedó vacía, la ruta pasa a la última).
    page = request.form.get('page', type=int)
    if pin.is_used:
        flash('No se puede eliminar un PIN ya utilizado.', 'danger')
    else:
        db.session.delete(pin)
        db.session.commit()
        flash('PIN eliminado.', 'warning')
    return redirect(url_for('admin_bp.pins', package_id=package_id, page=page if page and page > 1 else None))


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

    # Los códigos canjeados se acumulan: se pagina en vez de cortar en 500.
    page = request.args.get('page', type=int) or 1
    if page < 1:
        page = 1
    page_size = 50
    total_filtered = query.order_by(None).count()
    total_pages = max(1, (total_filtered + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages

    codes = query.offset((page - 1) * page_size).limit(page_size).all()

    page_numbers = list(range(max(1, page - 2), min(total_pages, page + 2) + 1))
    start_index = ((page - 1) * page_size) + 1 if total_filtered else 0
    end_index = min(page * page_size, total_filtered) if total_filtered else 0

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

    # Solo los juegos con verificación configurada (Free Fire / Blood Strike)
    # muestran el botón: en el resto no hay a quién preguntarle el nombre.
    from .verify import verifiable_game_ids
    verify_game_ids = verifiable_game_ids()

    return render_template(
        'admin/gift_codes.html',
        codes=codes,
        verify_game_ids=verify_game_ids,
        current_page=page,
        total_filtered=total_filtered,
        total_pages=total_pages,
        page_numbers=page_numbers,
        start_index=start_index,
        end_index=end_index,
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

    if not package_id or not quantity:
        flash('Elige el paquete y cuántos códigos generar.', 'danger')
        return redirect(url_for('admin_bp.gift_codes'))

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


@admin_bp.route('/gift-codes/<int:code_id>/verify-id', methods=['POST'])
@login_required
def gift_code_verify_id(code_id):
    """Busca el nombre del jugador detrás del ID con el que se canjeó un
    código y lo deja guardado.

    Los canjes viejos —y los de quien escribió el ID sin esperar a que la
    tienda lo verificara— quedaron con el número pelado; desde el panel no
    hay forma de saber a quién se le entregó. Este botón lo resuelve por el
    mismo camino que usa la tienda, así que el nombre es el real del juego.

    No usa @stock_pins_required a propósito: ese decorador redirige, y aquí
    responde JSON a un fetch. La clave se comprueba igual, a mano.
    """
    from .verify import verify_player_nick

    if not stock_pins_is_unlocked():
        return jsonify({
            'ok': False,
            'error': 'Tu acceso a Stock PINs expiró. Vuelve a ingresar el código.',
        }), 403

    gift = GiftCode.query.get_or_404(code_id)
    player_id = (gift.used_player_id or '').strip()
    if not gift.is_used or not player_id:
        return jsonify({'ok': False, 'error': 'Ese código todavía no fue canjeado.'}), 400

    game = gift.package.game if gift.package else None
    if not game:
        return jsonify({'ok': False, 'error': 'El premio ya no tiene un juego asociado.'}), 400

    payload, status = verify_player_nick(player_id, str(game.id))
    nick = (payload.get('nick') or '').strip()[:120] if payload.get('ok') else ''
    if nick and nick != (gift.used_nickname or ''):
        gift.used_nickname = nick
        db.session.commit()
    return jsonify(payload), status


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

    # Este formulario también lo usa /admin/minis (editar % de un mini): sin
    # esto, volvería siempre a /admin/affiliates en vez de a la pestaña de
    # la que vino. Solo se acepta un destino interno de admin, nunca uno
    # que venga tal cual del navegador.
    redirect_to = (request.form.get('redirect_to') or '').strip()
    if redirect_to.startswith('/admin/'):
        return redirect(redirect_to)
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
    view_tiers = MiniViewTier.query.order_by(MiniViewTier.sort_order.asc(), MiniViewTier.min_views.asc()).all()
    mini_payout_methods = MiniPayoutMethod.query.order_by(MiniPayoutMethod.sort_order.asc(), MiniPayoutMethod.name.asc()).all()
    course_videos = MiniCourseVideo.query.order_by(MiniCourseVideo.sort_order.asc(), MiniCourseVideo.id.asc()).all()

    program_settings = {
        s.key: s.value for s in Setting.query.filter(
            Setting.key.in_(['mini_rules_text', 'mini_rules_video_url', 'mini_whatsapp_group_url', 'mini_registration_enabled'])
        ).all()
    }

    return render_template(
        'admin/minis.html',
        mini_rules_text=program_settings.get('mini_rules_text', ''),
        mini_rules_video_url=program_settings.get('mini_rules_video_url', ''),
        mini_whatsapp_group_url=program_settings.get('mini_whatsapp_group_url', ''),
        mini_registration_enabled=program_settings.get('mini_registration_enabled', 'true') != 'false',
        mini_payout_methods=mini_payout_methods,
        course_videos=course_videos,
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
        view_tiers=view_tiers,
        suggested_reward_for_views=suggested_reward_for_views,
    )


@admin_bp.route('/minis/<int:aff_id>/password', methods=['POST'])
@login_required
def mini_set_password(aff_id):
    aff = Affiliate.query.get_or_404(aff_id)
    if not aff.is_mini:
        flash('Ese afiliado no es un mini influencer.', 'danger')
        return redirect(url_for('admin_bp.minis', section='aprobados'))

    new_password = request.form.get('new_password') or ''
    if len(new_password) < 6:
        flash('La contraseña debe tener al menos 6 caracteres.', 'danger')
        return redirect(url_for('admin_bp.minis', section='aprobados'))

    aff.set_password(new_password)
    db.session.commit()
    flash(f'Contraseña de "{aff.name}" actualizada.', 'success')
    return redirect(url_for('admin_bp.minis', section='aprobados'))


@admin_bp.route('/minis/<int:aff_id>/nota', methods=['POST'])
@login_required
def mini_send_note(aff_id):
    aff = Affiliate.query.get_or_404(aff_id)
    if not aff.is_mini:
        flash('Ese afiliado no es un mini influencer.', 'danger')
        return redirect(url_for('admin_bp.minis', section='aprobados'))

    message = (request.form.get('message') or '').strip()
    if not message:
        flash('Escribe la nota que le vas a mandar.', 'danger')
        return redirect(url_for('admin_bp.minis', section='aprobados'))

    notify_mini(aff, f'📩 {message[:280]}')
    db.session.commit()
    flash(f'Nota enviada a "{aff.name}".', 'success')
    return redirect(url_for('admin_bp.minis', section='aprobados'))


# ─── Config: tramos de vistas y rangos ────────────────────────────────────────

@admin_bp.route('/minis/program-config', methods=['POST'])
@login_required
def mini_program_config_save():
    """Reglas del programa (texto + video opcional) y link del grupo de
    WhatsApp que ve el mini en su panel, en 'Ver reglas del programa' y
    'Únete al grupo de WhatsApp de los minis'."""
    updates = {
        'mini_rules_text': (request.form.get('mini_rules_text', '') or '').strip(),
        'mini_rules_video_url': (request.form.get('mini_rules_video_url', '') or '').strip(),
        'mini_whatsapp_group_url': (request.form.get('mini_whatsapp_group_url', '') or '').strip(),
        'mini_registration_enabled': 'true' if request.form.get('mini_registration_enabled') else 'false',
    }
    for key, value in updates.items():
        setting = Setting.query.filter_by(key=key).first()
        if not setting:
            setting = Setting(key=key, value=value, description='Configuración del programa de minis.')
            db.session.add(setting)
        else:
            setting.value = value
    db.session.commit()
    flash('Configuración del programa actualizada.', 'success')
    return redirect(url_for('admin_bp.minis', section='config'))


@admin_bp.route('/minis/payout-methods/add', methods=['POST'])
@login_required
def mini_payout_method_add():
    name = (request.form.get('name') or '').strip()[:100]
    if not name:
        flash('El método necesita un nombre.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))
    if MiniPayoutMethod.query.filter(db.func.lower(MiniPayoutMethod.name) == name.lower()).first():
        flash('Ya existe un método de pago con ese nombre.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    try:
        sort_order = int(request.form.get('sort_order') or 100)
    except ValueError:
        sort_order = 100

    db.session.add(MiniPayoutMethod(name=name, is_active=True, sort_order=sort_order))
    db.session.commit()
    flash('Método de pago agregado.', 'success')
    return redirect(url_for('admin_bp.minis', section='config'))


@admin_bp.route('/minis/payout-methods/<int:method_id>/toggle', methods=['POST'])
@login_required
def mini_payout_method_toggle(method_id):
    method = MiniPayoutMethod.query.get_or_404(method_id)
    method.is_active = not method.is_active
    db.session.commit()
    flash('Método de pago actualizado.', 'success')
    return redirect(url_for('admin_bp.minis', section='config'))


@admin_bp.route('/minis/payout-methods/<int:method_id>/delete', methods=['POST'])
@login_required
def mini_payout_method_delete(method_id):
    method = MiniPayoutMethod.query.get_or_404(method_id)
    db.session.delete(method)
    db.session.commit()
    flash('Método de pago eliminado.', 'warning')
    return redirect(url_for('admin_bp.minis', section='config'))


@admin_bp.route('/minis/tiers/add', methods=['POST'])
@login_required
def mini_tier_add():
    try:
        min_views = int(request.form.get('min_views') or 0)
        max_views_raw = (request.form.get('max_views') or '').strip()
        max_views = int(max_views_raw) if max_views_raw else None
        reward_amount = float(request.form.get('reward_amount') or 0)
        sort_order = int(request.form.get('sort_order') or 100)
    except ValueError:
        flash('Revisa los números del tramo.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    if min_views < 0 or reward_amount < 0 or (max_views is not None and max_views < min_views):
        flash('Ese tramo no es válido.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    db.session.add(MiniViewTier(
        min_views=min_views, max_views=max_views, reward_amount=reward_amount, sort_order=sort_order,
    ))
    db.session.commit()
    flash('Tramo agregado.', 'success')
    return redirect(url_for('admin_bp.minis', section='config'))


@admin_bp.route('/minis/tiers/<int:tier_id>/edit', methods=['POST'])
@login_required
def mini_tier_edit(tier_id):
    tier = MiniViewTier.query.get_or_404(tier_id)
    try:
        min_views = int(request.form.get('min_views') or 0)
        max_views_raw = (request.form.get('max_views') or '').strip()
        max_views = int(max_views_raw) if max_views_raw else None
        reward_amount = float(request.form.get('reward_amount') or 0)
        sort_order = int(request.form.get('sort_order') or tier.sort_order or 100)
    except ValueError:
        flash('Revisa los números del tramo.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    if min_views < 0 or reward_amount < 0 or (max_views is not None and max_views < min_views):
        flash('Ese tramo no es válido.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    tier.min_views = min_views
    tier.max_views = max_views
    tier.reward_amount = reward_amount
    tier.sort_order = sort_order
    db.session.commit()
    flash('Tramo actualizado.', 'success')
    return redirect(url_for('admin_bp.minis', section='config'))


@admin_bp.route('/minis/tiers/<int:tier_id>/delete', methods=['POST'])
@login_required
def mini_tier_delete(tier_id):
    tier = MiniViewTier.query.get_or_404(tier_id)
    db.session.delete(tier)
    db.session.commit()
    flash('Tramo eliminado.', 'success')
    return redirect(url_for('admin_bp.minis', section='config'))


@admin_bp.route('/minis/course-videos/add', methods=['POST'])
@login_required
def mini_course_video_add():
    title = (request.form.get('title') or '').strip()[:150]
    youtube_url = (request.form.get('youtube_url') or '').strip()[:500]
    description = (request.form.get('description') or '').strip()
    if not title or not youtube_url:
        flash('El video necesita título y link.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    try:
        sort_order = int(request.form.get('sort_order') or 100)
    except ValueError:
        flash('Revisa el orden del video.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    db.session.add(MiniCourseVideo(
        title=title, youtube_url=youtube_url, description=description, sort_order=sort_order,
    ))
    db.session.commit()
    flash('Video agregado al curso.', 'success')
    return redirect(url_for('admin_bp.minis', section='config'))


@admin_bp.route('/minis/course-videos/<int:video_id>/edit', methods=['POST'])
@login_required
def mini_course_video_edit(video_id):
    video = MiniCourseVideo.query.get_or_404(video_id)
    title = (request.form.get('title') or '').strip()[:150]
    youtube_url = (request.form.get('youtube_url') or '').strip()[:500]
    if not title or not youtube_url:
        flash('El video necesita título y link.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    try:
        sort_order = int(request.form.get('sort_order') or video.sort_order or 100)
    except ValueError:
        flash('Revisa el orden del video.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    video.title = title
    video.youtube_url = youtube_url
    video.description = (request.form.get('description') or '').strip()
    video.sort_order = sort_order
    db.session.commit()
    flash('Video actualizado.', 'success')
    return redirect(url_for('admin_bp.minis', section='config'))


@admin_bp.route('/minis/course-videos/<int:video_id>/delete', methods=['POST'])
@login_required
def mini_course_video_delete(video_id):
    video = MiniCourseVideo.query.get_or_404(video_id)
    db.session.delete(video)
    db.session.commit()
    flash('Video eliminado del curso.', 'success')
    return redirect(url_for('admin_bp.minis', section='config'))


@admin_bp.route('/minis/ranks/add', methods=['POST'])
@login_required
def mini_rank_add():
    name = (request.form.get('name') or '').strip()[:50]
    if not name:
        flash('El rango necesita un nombre.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))
    if MiniRank.query.filter(db.func.lower(MiniRank.name) == name.lower()).first():
        flash('Ya existe un rango con ese nombre.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    try:
        uses_required = int(request.form.get('uses_required') or 0)
        bonus_amount = float(request.form.get('bonus_amount') or 0)
        sort_order = int(request.form.get('sort_order') or 100)
    except ValueError:
        flash('Revisa los números del rango.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    if uses_required <= 0 or bonus_amount < 0:
        flash('Ese rango no es válido.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    db.session.add(MiniRank(
        name=name, uses_required=uses_required, bonus_amount=bonus_amount, sort_order=sort_order,
    ))
    db.session.commit()
    flash('Rango agregado.', 'success')
    return redirect(url_for('admin_bp.minis', section='config'))


@admin_bp.route('/minis/ranks/<int:rank_id>/edit', methods=['POST'])
@login_required
def mini_rank_edit(rank_id):
    rank = MiniRank.query.get_or_404(rank_id)
    name = (request.form.get('name') or '').strip()[:50]
    if not name:
        flash('El rango necesita un nombre.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))
    if MiniRank.query.filter(db.func.lower(MiniRank.name) == name.lower(), MiniRank.id != rank.id).first():
        flash('Ya existe un rango con ese nombre.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    try:
        uses_required = int(request.form.get('uses_required') or 0)
        bonus_amount = float(request.form.get('bonus_amount') or 0)
        sort_order = int(request.form.get('sort_order') or rank.sort_order or 100)
    except ValueError:
        flash('Revisa los números del rango.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    if uses_required <= 0 or bonus_amount < 0:
        flash('Ese rango no es válido.', 'danger')
        return redirect(url_for('admin_bp.minis', section='config'))

    # Si se renombra un rango ya pagado a alguien, Affiliate.ranks_paid guarda
    # el nombre viejo: ese mini se vería "sin pagar" bajo el nombre nuevo. No
    # hay forma de saberlo sin recorrer todos los afiliados, así que queda
    # como advertencia en el hint del formulario, no como bloqueo aquí.
    rank.name = name
    rank.uses_required = uses_required
    rank.bonus_amount = bonus_amount
    rank.sort_order = sort_order
    db.session.commit()
    flash('Rango actualizado.', 'success')
    return redirect(url_for('admin_bp.minis', section='config'))


@admin_bp.route('/minis/ranks/<int:rank_id>/delete', methods=['POST'])
@login_required
def mini_rank_delete(rank_id):
    rank = MiniRank.query.get_or_404(rank_id)
    db.session.delete(rank)
    db.session.commit()
    flash('Rango eliminado.', 'success')
    return redirect(url_for('admin_bp.minis', section='config'))


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
        client_discount_rate = float(request.form.get('client_discount_rate', 3.0))
        commission_rate = float(request.form.get('commission_rate', 0.0))
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
    notify_mini(aff, f'🎉 ¡Tu solicitud fue aprobada! Ya puedes compartir tu código {code} y empezar a ganar.')
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
    reason_txt = f' Motivo: {aff.rejection_reason}' if aff.rejection_reason else ''
    notify_mini(aff, f'❌ Tu solicitud fue rechazada.{reason_txt}')
    db.session.commit()
    flash(f'Solicitud de "{aff.name}" rechazada.', 'success')
    return redirect(url_for('admin_bp.minis'))


@admin_bp.route('/minis/<int:aff_id>/delete', methods=['POST'])
@login_required
def mini_delete(aff_id):
    aff = Affiliate.query.get_or_404(aff_id)
    if not aff.is_mini:
        flash('Ese afiliado no es un mini influencer.', 'danger')
        return redirect(url_for('admin_bp.minis', section='aprobados'))

    name = aff.name

    # Las órdenes que entraron con el código de este mini son historial real
    # de la tienda: no se borran, solo se les quita el vínculo con la cuenta
    # que se está eliminando.
    Order.query.filter_by(affiliate_id=aff.id).update({'affiliate_id': None})

    for video in MiniVideo.query.filter_by(affiliate_id=aff.id).all():
        db.session.delete(video)
    for commission in AffiliateCommission.query.filter_by(affiliate_id=aff.id).all():
        db.session.delete(commission)
    for withdrawal in AffiliateWithdrawal.query.filter_by(affiliate_id=aff.id).all():
        db.session.delete(withdrawal)
    for notif in MiniNotification.query.filter_by(affiliate_id=aff.id).all():
        db.session.delete(notif)

    db.session.delete(aff)
    db.session.commit()
    flash(f'Cuenta mini "{name}" eliminada.', 'success')
    return redirect(url_for('admin_bp.minis', section='aprobados'))


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

    proof_path = None
    proof_file = request.files.get('payment_proof')
    if proof_file and proof_file.filename:
        if not allowed_file(proof_file.filename):
            flash('El comprobante debe ser una imagen PNG, JPG, JPEG, GIF o WEBP.', 'danger')
            return redirect(url_for('admin_bp.minis', section='retiros'))
        proof_path = save_image(proof_file, 'withdrawal_proofs')

    ok, error = approve_mini_withdrawal(withdrawal, proof_path=proof_path)
    if not ok and proof_path:
        delete_uploaded_file(proof_path)
    flash(error if not ok else 'Retiro aprobado.', 'danger' if not ok else 'success')
    return redirect(url_for('admin_bp.minis', section='retiros'))


@admin_bp.route('/minis/retiros/<int:w_id>/comprobante', methods=['POST'])
@login_required
def mini_withdrawal_proof(w_id):
    """Adjunta (o cambia) el comprobante de pago de un retiro que ya se aprobó, para que el mini lo vea en su panel."""
    withdrawal = AffiliateWithdrawal.query.get_or_404(w_id)
    back = redirect(url_for('admin_bp.minis', section='retiros'))
    proof_file = request.files.get('payment_proof')
    if not proof_file or not proof_file.filename:
        flash('Elige la foto o captura del comprobante.', 'danger')
        return back
    if not allowed_file(proof_file.filename):
        flash('El comprobante debe ser una imagen PNG, JPG, JPEG, GIF o WEBP.', 'danger')
        return back
    proof_path = save_image(proof_file, 'withdrawal_proofs')
    ok, error, previous = attach_withdrawal_proof(withdrawal, proof_path)
    if not ok:
        delete_uploaded_file(proof_path)
        flash(error, 'danger')
        return back
    if previous and previous != proof_path:
        delete_uploaded_file(previous)
    flash('Comprobante guardado. El mini ya lo ve en su panel.', 'success')
    return back


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
    if not _in_admin_area():
        return {}
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


@admin_bp.app_context_processor
def _inject_maintenance_state():
    """Para que el botón de mantenimiento del sidebar sepa si ya está
    prendido y se pinte distinto (y diga "Desactivar")."""
    if not _in_admin_area():
        return {}
    try:
        setting = Setting.query.filter_by(key='maintenance_mode').first()
        return {'maintenance_mode_on': bool(setting and setting.value == 'true')}
    except Exception:
        return {'maintenance_mode_on': False}


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
    binance_verify = bool(request.form.get('binance_verify'))
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
        binance_verify=binance_verify,
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
    method.binance_verify = bool(request.form.get('binance_verify'))
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
    """"Ganadores provisionales" del admin: de cada ranking encendido, el top actual del mes (con los datos completos) y los meses ya archivados."""
    from .main import _get_archive_history_payload, _get_previous_archive_payload
    from ..utils import ranking_boards as RB

    month_label = now_ve().strftime('%m/%Y')
    rankings = []

    for board in RB.enabled_boards():
        game = board.game if board.kind == 'game' else None
        enabled = board.kind != 'game' or game is not None
        prizes = RB.prize_map(board)
        places = RB.places(board)
        item = {
            'key': board.key,
            'kind': board.kind,
            'label': board.name,
            'units_label': RB.units_label(board),
            'month_label': month_label,
            'enabled': enabled,
            'game_id': game.id if game else None,
            'game_name': game.name if game else board.name,
            'leaders': [],
            'total_players': 0,
            'archive_history': _get_archive_history_payload(board.key, include_private=True),
            'previous_winners': _get_previous_archive_payload(board.key),
        }

        if enabled and board.kind == 'game':
            entries = RB.game_entries(game.id)
            item['total_players'] = len(entries)
            for index, entry in enumerate(entries[:10], start=1):
                label = RB.prize_label(prizes.get(index)) if index <= places else ''
                item['leaders'].append({
                    'position': index,
                    'player_id': (entry.get('player_id') or '').strip() or '----',
                    'nickname': (entry.get('nickname') or '').strip() or 'Sin nickname',
                    'total_units': int(entry.get('total_units') or 0),
                    'total_spent': round(float(entry.get('total_spent') or 0), 2),
                    'reward_value': label or None,
                    'prize_label': label or 'Sin premio',
                    'is_prize_eligible': index <= places,
                })
        elif enabled:
            rows = profile_social.level_ranking(limit=10, reveal=True) if board.kind == 'levels' else profile_social.account_month_ranking(limit=10, reveal=True)
            item['total_players'] = len(rows)
            for row in rows:
                index = row['position']
                owner = db.session.get(User, row['user_id'])
                label = RB.prize_label(prizes.get(index)) if index <= places else ''
                item['leaders'].append({
                    'position': index,
                    'player_id': f"@{row['handle']}",
                    'nickname': row['name'],
                    'email': (owner.email or owner.account_identifier or '') if owner is not None else '',
                    'total_units': int(row['total_units'] or 0),
                    'total_spent': 0,
                    'reward_value': label or None,
                    'prize_label': label or 'Sin premio',
                    'is_prize_eligible': index <= places,
                })

        rankings.append(item)

    return rankings


def _board_view(board, form=None):
    """Un ranking tal como se pinta en el formulario: lo guardado o, si no se pudo guardar, lo que el administrador acababa de escribir."""
    from ..utils import ranking_boards as RB

    prizes = RB.prize_map(board)
    rows = []
    for position in range(1, RB.MAX_PLACES + 1):
        if form is not None:
            rows.append({
                'position': position, 'type': (form.get(f'prize_type_{position}') or 'none'),
                'package_id': (form.get(f'prize_package_{position}') or ''), 'points': (form.get(f'prize_points_{position}') or ''),
                'text': (form.get(f'prize_text_{position}') or ''), 'auto': form.get(f'prize_auto_{position}') in ('1', 'on', 'true'),
            })
            continue
        prize = prizes.get(position)
        rows.append({
            'position': position, 'type': prize.prize_type if prize else 'none', 'package_id': str(prize.package_id or '') if prize else '',
            'points': str(prize.points) if (prize and prize.points) else '', 'text': (prize.text or '') if prize else '',
            'auto': bool(prize and prize.auto),
        })
    if form is not None:
        places = form.get('prize_places') or RB.places(board)
        return {
            'board': board, 'name': form.get('name') or board.name, 'units_label': form.get('units_label') or RB.units_label(board),
            'enabled': form.get('enabled') in ('1', 'on', 'true'), 'game_id': form.get('game_id') or str(board.game_id or ''),
            'places': places, 'rows': rows,
        }
    return {
        'board': board, 'name': board.name, 'units_label': RB.units_label(board), 'enabled': bool(board.enabled),
        'game_id': str(board.game_id or ''), 'places': RB.places(board), 'rows': rows,
    }


def _rankings_page(form_board=None, form=None):
    from ..utils import ranking_boards as RB

    games = Game.query.filter_by(is_active=True).order_by(Game.name.asc()).all()
    packages_by_game = {}
    for package in Package.query.filter_by(is_active=True).order_by(Package.game_id.asc(), Package.sort_order.asc(), Package.name.asc()).all():
        packages_by_game.setdefault(str(package.game_id), []).append({'id': package.id, 'name': package.name, 'is_automated': bool(package.is_automated)})
    views = [
        _board_view(board, form if (form is not None and form_board is not None and board.id == form_board.id) else None)
        for board in RB.all_boards()
    ]
    return render_template(
        'admin/rankings.html', boards=views, games=games, packages_by_game=packages_by_game, kinds=RB.KINDS, prize_type_labels=RB.PRIZE_TYPE_LABELS,
        prize_types=RB.PRIZE_TYPES, max_places=RB.MAX_PLACES, text_max=RB.TEXT_MAX, name_max=RB.NAME_MAX, units_max=RB.UNITS_MAX,
        points_max=RB.POINTS_MAX, max_boards=RB.MAX_BOARDS,
    )


@admin_bp.route('/rankings')
@login_required
def rankings():
    return _rankings_page()


@admin_bp.route('/rankings/crear', methods=['POST'])
@login_required
def ranking_create():
    from ..utils import ranking_boards as RB

    board, error = RB.create_board(request.form.get('name'), (request.form.get('kind') or '').strip(), request.form.get('game_id'))
    if error:
        db.session.rollback()
        flash(error, 'danger')
        return redirect(url_for('admin_bp.rankings') + '#crearRanking')
    flash(f'Ranking "{board.name}" creado. Está apagado: ponle los premios y enciéndelo para que salga en la tienda.', 'success')
    return redirect(url_for('admin_bp.rankings') + f'#board-{board.id}')


@admin_bp.route('/rankings/<int:board_id>/guardar', methods=['POST'])
@login_required
def ranking_save(board_id):
    from ..utils import ranking_boards as RB

    board = RankingBoard.query.get_or_404(board_id)
    error = RB.save_board(board, request.form)
    if error:
        db.session.rollback()
        flash(error, 'danger')
        board = RankingBoard.query.get_or_404(board_id)   # sin lo que no se alcanzó a guardar
        return _rankings_page(form_board=board, form=request.form)
    db.session.commit()
    flash(f'Ranking "{board.name}" guardado.' + (' Ya sale en la tienda.' if board.enabled else ' Está apagado: no se ve en la tienda.'), 'success')
    return redirect(url_for('admin_bp.rankings') + f'#board-{board.id}')


@admin_bp.route('/rankings/<int:board_id>/borrar', methods=['POST'])
@login_required
def ranking_delete(board_id):
    from ..utils import ranking_boards as RB

    board = RankingBoard.query.get_or_404(board_id)
    name = board.name
    RB.delete_board(board)
    db.session.commit()
    flash(f'Ranking "{name}" borrado. Los ganadores de meses anteriores ya archivados no se tocan.', 'warning')
    return redirect(url_for('admin_bp.rankings'))


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


_SETTINGS_SECTION_RE = re.compile(r'^sec-[a-z0-9_-]{1,40}$')


def _settings_redirect():
    """Vuelve a Configuración. Si se guardó con el botón de una sección ("Guardar esta sección"), regresa a esa misma sección: la página la abre sola."""
    section = (request.form.get('__section') or '').strip()
    target = url_for('admin_bp.settings')
    return redirect(f'{target}#{section}' if _SETTINGS_SECTION_RE.match(section) else target)


@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    usd_rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
    usd_rate = usd_rate_setting.value if usd_rate_setting else ''
    default_pkg_setting = Setting.query.filter_by(key='default_auto_package_id').first()
    default_auto_package_id = default_pkg_setting.value if default_pkg_setting else ''
    site_logo_setting = Setting.query.filter_by(key='site_logo').first()
    site_logo_value = site_logo_setting.value if site_logo_setting else ''
    support_avatar_setting = Setting.query.filter_by(key='support_avatar').first()
    support_avatar_value = support_avatar_setting.value if support_avatar_setting else ''
    site_background_setting = Setting.query.filter_by(key='site_background_image').first()
    site_background_value = site_background_setting.value if site_background_setting else ''
    order_status_image_setting = Setting.query.filter_by(key='order_status_image').first()
    order_status_image_value = order_status_image_setting.value if order_status_image_setting else ''
    promo_banner_image_setting = Setting.query.filter_by(key='promo_banner_image').first()
    promo_banner_image_value = promo_banner_image_setting.value if promo_banner_image_setting else ''
    promo_banner_link_setting = Setting.query.filter_by(key='promo_banner_link').first()
    promo_banner_link_value = promo_banner_link_setting.value if promo_banner_link_setting else ''
    drawer_bg_settings = {
        s.key: s for s in Setting.query.filter(
            Setting.key.in_([key for key, _, _ in DRAWER_BG_ITEMS])
        ).all()
    }
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
    site_tutorial_video_file_setting = Setting.query.filter_by(key='site_tutorial_video_file').first()
    site_tutorial_video_file_value = site_tutorial_video_file_setting.value if site_tutorial_video_file_setting else ''
    site_tutorial_video_title_setting = Setting.query.filter_by(key='site_tutorial_video_title').first()
    site_tutorial_video_title_value = site_tutorial_video_title_setting.value if site_tutorial_video_title_setting else ''

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
    community_popup_keys = {
        'community_popup_enabled': 'Activa el popup recurrente de "Únete a nuestra comunidad"',
        'community_popup_interval_hours': 'Cada cuántas horas puede reaparecer (por pestaña/visita)',
        'community_popup_whatsapp_url': 'Link del canal de WhatsApp al que invita el popup',
    }
    manual_schedule_keys = {
        'manual_open_hour': 'Hora (0-23, Venezuela) en que abren los paquetes de recarga manual',
        'manual_close_hour': 'Hora (0-23, Venezuela) en que cierran los paquetes de recarga manual',
        'manual_service_force_closed': 'Cierra los paquetes de recarga manual ya mismo, sin importar el horario',
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

    # Entrar con Google: la llave secreta nunca se devuelve a la pantalla, solo si ya hay una guardada.
    google_config = google_login.get_config()
    google_login_settings = {
        'enabled': (Setting.query.filter_by(key='google_login_enabled').first() or Setting(value='')).value != '0',
        'client_id': (Setting.query.filter_by(key='google_client_id').first() or Setting(value='')).value or '',
        'has_secret': bool((Setting.query.filter_by(key='google_client_secret').first() or Setting(value='')).value),
        'active': google_config['enabled'],
    }

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
        'manual_service_force_closed': '1' if manual_schedule.get('force_closed') else '0',
    }

    active_payment_methods = PaymentMethod.query.filter_by(is_active=True).order_by(PaymentMethod.sort_order.asc(), PaymentMethod.name.asc()).all()
    if request.method == 'POST':
        new_rate = request.form.get('usd_rate_bs', '').strip()
        default_pkg = request.form.get('default_auto_package_id', '').strip()
        remove_logo = request.form.get('remove_logo')
        logo_file = request.files.get('site_logo')
        remove_support_avatar = request.form.get('remove_support_avatar')
        support_avatar_file = request.files.get('support_avatar')
        remove_site_background = request.form.get('remove_site_background')
        site_background_file = request.files.get('site_background_image')
        remove_order_status_image = request.form.get('remove_order_status_image')
        order_status_image_file = request.files.get('order_status_image')
        remove_promo_banner_image = request.form.get('remove_promo_banner_image')
        promo_banner_image_file = request.files.get('promo_banner_image')
        promo_banner_link = (request.form.get('promo_banner_link', '') or '').strip()
        drawer_bg_removes = {key: request.form.get('remove_' + key) for key, _, _ in DRAWER_BG_ITEMS}
        drawer_bg_files = {key: request.files.get(key) for key, _, _ in DRAWER_BG_ITEMS}
        checkout_payment_video_method = (request.form.get('checkout_payment_video_method', '') or '').strip().lower()
        checkout_payment_video_title = (request.form.get('checkout_payment_video_title', '') or '').strip()
        checkout_payment_video_message = (request.form.get('checkout_payment_video_message', '') or '').strip()
        checkout_payment_video_cta = (request.form.get('checkout_payment_video_cta', '') or '').strip()
        remove_checkout_payment_video_file = request.form.get('remove_checkout_payment_video_file')
        checkout_payment_video_file = request.files.get('checkout_payment_video_file')
        site_tutorial_video_title = (request.form.get('site_tutorial_video_title', '') or '').strip()
        remove_site_tutorial_video_file = request.form.get('remove_site_tutorial_video_file')
        site_tutorial_video_file = request.files.get('site_tutorial_video_file')
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
            'manual_service_force_closed': '1' if request.form.get('manual_service_force_closed') else '0',
        }

        if new_rate:
            try:
                float(new_rate)
            except ValueError:
                flash('La tasa debe ser un número válido.', 'danger')
                return _settings_redirect()

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

        if remove_support_avatar and support_avatar_setting:
            delete_uploaded_file(support_avatar_setting.value)
            support_avatar_setting.value = ''

        if support_avatar_file and support_avatar_file.filename:
            saved_support_avatar = save_image(support_avatar_file, 'branding', AVATAR_IMAGE_DIMENSION)
            if saved_support_avatar:
                if support_avatar_setting and support_avatar_setting.value:
                    delete_uploaded_file(support_avatar_setting.value)
                if not support_avatar_setting:
                    support_avatar_setting = Setting(
                        key='support_avatar',
                        value=saved_support_avatar,
                        description='Foto del soporte en la cabecera del chat'
                    )
                    db.session.add(support_avatar_setting)
                else:
                    support_avatar_setting.value = saved_support_avatar

        if remove_site_background and site_background_setting:
            delete_uploaded_file(site_background_setting.value)
            site_background_setting.value = ''

        if site_background_file and site_background_file.filename:
            saved_background = _save_page_background(site_background_file)
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

        new_promo_banner = None   # el banner recién subido: al guardar se le hace su copia liviana (modo liviano) en segundo plano
        if remove_promo_banner_image and promo_banner_image_setting:
            banner_lite.delete_lite(promo_banner_image_setting.value)
            delete_uploaded_file(promo_banner_image_setting.value)
            promo_banner_image_setting.value = ''

        if promo_banner_image_file and promo_banner_image_file.filename:
            if is_promo_banner_video(promo_banner_image_file.filename):
                saved_promo_banner, video_error = save_promo_banner_video(promo_banner_image_file)
                if video_error:
                    flash(video_error, 'danger')
            else:
                saved_promo_banner = save_image(promo_banner_image_file, 'branding')
                if not saved_promo_banner:
                    flash('Formato no permitido para el banner: usa una imagen (JPG, PNG, WEBP, GIF) o un video MP4.', 'danger')
            if saved_promo_banner:
                new_promo_banner = saved_promo_banner
                if promo_banner_image_setting and promo_banner_image_setting.value:
                    banner_lite.delete_lite(promo_banner_image_setting.value)
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

        for key, description, _label in DRAWER_BG_ITEMS:
            setting = drawer_bg_settings.get(key)
            if drawer_bg_removes.get(key) and setting:
                delete_uploaded_file(setting.value)
                setting.value = ''

            bg_file = drawer_bg_files.get(key)
            if bg_file and bg_file.filename:
                saved_bg = _save_page_background(bg_file)
                if saved_bg:
                    if setting and setting.value:
                        delete_uploaded_file(setting.value)
                    if not setting:
                        setting = Setting(key=key, value=saved_bg, description=description)
                        db.session.add(setting)
                        drawer_bg_settings[key] = setting
                    else:
                        setting.value = saved_bg

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
                return _settings_redirect()
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

        if remove_site_tutorial_video_file and site_tutorial_video_file_setting:
            if site_tutorial_video_file_setting.value:
                delete_uploaded_file(site_tutorial_video_file_setting.value)
            site_tutorial_video_file_setting.value = ''

        if site_tutorial_video_file and site_tutorial_video_file.filename:
            saved_site_tutorial_video = save_video(site_tutorial_video_file, 'tutorial')
            if not saved_site_tutorial_video:
                flash('El video debe estar en formato mp4, webm, mov o m4v.', 'danger')
                return _settings_redirect()
            if site_tutorial_video_file_setting and site_tutorial_video_file_setting.value:
                delete_uploaded_file(site_tutorial_video_file_setting.value)
            if not site_tutorial_video_file_setting:
                site_tutorial_video_file_setting = Setting(
                    key='site_tutorial_video_file',
                    value=saved_site_tutorial_video,
                    description='Video del botón flotante "Tutorial", visible en todas las páginas.',
                )
                db.session.add(site_tutorial_video_file_setting)
            else:
                site_tutorial_video_file_setting.value = saved_site_tutorial_video

        site_tutorial_video_title_row = Setting.query.filter_by(key='site_tutorial_video_title').first()
        if not site_tutorial_video_title_row:
            site_tutorial_video_title_row = Setting(
                key='site_tutorial_video_title', value=site_tutorial_video_title,
                description='Título de la ventana del video tutorial flotante.',
            )
            db.session.add(site_tutorial_video_title_row)
        else:
            site_tutorial_video_title_row.value = site_tutorial_video_title

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

        # Entrar con Google (clientes): el secreto solo se cambia si se escribe uno nuevo.
        if request.form.get('remove_google_client'):
            _upsert_setting('google_client_id', '')
            _upsert_setting('google_client_secret', '')
        else:
            _upsert_setting('google_client_id', (request.form.get('google_client_id', '') or '').strip()[:200])
            new_google_secret = (request.form.get('google_client_secret', '') or '').strip()[:200]
            if new_google_secret:
                _upsert_setting('google_client_secret', new_google_secret)
        _upsert_setting('google_login_enabled', '1' if request.form.get('google_login_enabled') else '0')

        db.session.commit()
        if new_promo_banner:
            banner_lite.ensure_async(current_app._get_current_object(), new_promo_banner)
        flash('Configuración actualizada.', 'success')
        return _settings_redirect()

    return render_template(
        'admin/settings.html',
        usd_rate=usd_rate,
        default_package_id=default_auto_package_id,
        site_logo=site_logo_value,
        support_avatar=support_avatar_value,
        site_background_image=site_background_value,
        order_status_image=order_status_image_value,
        promo_banner_image=promo_banner_image_value,
        promo_banner_link=promo_banner_link_value,
        promo_banner_video_max_mb=PROMO_BANNER_VIDEO_MAX_MB,
        drawer_bg_items=[
            (key, label, (drawer_bg_settings[key].value if key in drawer_bg_settings and drawer_bg_settings[key].value else ''))
            for key, _description, label in DRAWER_BG_ITEMS
        ],
        social_settings=social_settings,
        email_settings=email_settings,
        payment_verify_settings=payment_verify_settings,
        binance_auto_settings=binance_auto_settings,
        binance_keys_ok=bool(current_app.config.get('BINANCE_API_KEY') and current_app.config.get('BINANCE_API_SECRET')),
        google_login_settings=google_login_settings,
        google_redirect_uri=google_login.redirect_uri(),
        active_payment_methods=active_payment_methods,
        checkout_payment_video_method=checkout_payment_video_method_value,
        checkout_payment_video_title=checkout_payment_video_title_value,
        checkout_payment_video_message=checkout_payment_video_message_value,
        checkout_payment_video_cta=checkout_payment_video_cta_value,
        checkout_payment_video_file=checkout_payment_video_file_value,
        site_tutorial_video_file=site_tutorial_video_file_value,
        site_tutorial_video_title=site_tutorial_video_title_value,
        community_popup_settings=community_popup_settings,
        manual_schedule_settings=manual_schedule_settings,
    )


def _prize_package_delivery_status(package):
    """Cómo saldría solo este paquete cuando se entrega como premio: por
    mapeo de proveedor, por stock de PINs (bot) o, si no tiene ninguno, a
    mano por un admin. Es lo que el admin necesita ver junto a cada premio."""
    if not package:
        return None
    routes = []
    if RevendedoresItemMapping.query.filter_by(store_package_id=package.id, active=True, auto_enabled=True).first():
        routes.append('Revendedores')
    if CentralOneItemMapping.query.filter_by(store_package_id=package.id, active=True, auto_enabled=True).first():
        routes.append('Central One')
    free_pins = (
        Pin.query.filter(Pin.package_id == package.id, Pin.is_used.is_(False), Pin.order_id.is_(None)).count()
        if package.is_automated else 0
    )
    return {
        'routes': routes,
        'is_automated': bool(package.is_automated),
        'free_pins': free_pins,
        'automatic': bool(routes) or free_pins > 0,
    }


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

    # Recarga Acumulada no tenía ningún resumen a la vista sin entrar juego
    # por juego más abajo — acá se junta el historial de TODOS los juegos,
    # igual que "Ganadores recientes" hace para la ruleta.
    accumulated_winners = (
        PromoAccumulatedAward.query
        .options(joinedload(PromoAccumulatedAward.game), joinedload(PromoAccumulatedAward.prize_order))
        .order_by(PromoAccumulatedAward.created_at.desc())
        .limit(100)
        .all()
    )

    # ─ Promociones (Recarga Acumulada / Sorteo Diario / Adivina el Número) ─
    # Viven en esta misma página: son minijuegos igual que la ruleta/
    # tragaperras de arriba, solo que configurados por juego en vez de por
    # slot fijo.
    promos_context = {}
    if all_games:
        promos_game_id = request.args.get('game_id', type=int) or all_games[0].id
        promos_selected_game = Game.query.get(promos_game_id) or all_games[0]

        promos_game_packages = (
            Package.query.filter_by(game_id=promos_selected_game.id, is_active=True)
            .order_by(Package.sort_order.asc(), Package.name.asc())
            .all()
        )

        promos_levels = {
            lvl.level_number: lvl
            for lvl in PromoAccumulatedLevel.query.filter_by(game_id=promos_selected_game.id).all()
        }
        promos_raffle_config = PromoRaffleConfig.query.filter_by(game_id=promos_selected_game.id).first()
        promos_guess_config = PromoGuessConfig.query.filter_by(game_id=promos_selected_game.id).first()

        promos_recent_awards = (
            PromoAccumulatedAward.query
            .filter_by(game_id=promos_selected_game.id)
            .order_by(PromoAccumulatedAward.created_at.desc())
            .limit(15).all()
        )
        promos_recent_raffle_winners = (
            PromoRaffleWinner.query
            .filter_by(game_id=promos_selected_game.id)
            .order_by(PromoRaffleWinner.created_at.desc())
            .limit(15).all()
        )
        promos_recent_guess_winners = (
            PromoGuessWinner.query
            .filter_by(game_id=promos_selected_game.id)
            .order_by(PromoGuessWinner.created_at.desc())
            .limit(15).all()
        )

        promos_today_key = today_ve_str()
        promos_guess_round_today = PromoGuessRound.query.filter_by(
            game_id=promos_selected_game.id, day_key=promos_today_key,
        ).first()
        promos_guess_winners_today = (
            PromoGuessWinner.query
            .filter_by(game_id=promos_selected_game.id, day_key=promos_today_key)
            .order_by(PromoGuessWinner.slot_index.asc())
            .all()
        )
        from ..models import PromoGuessLog
        promos_guess_recent_attempts = (
            PromoGuessLog.query.filter_by(game_id=promos_selected_game.id)
            .order_by(PromoGuessLog.id.desc()).limit(40).all()
        )
        promos_guess_today_slots = []
        if promos_guess_config:
            won_by_slot = {w.slot_index: w for w in promos_guess_winners_today}
            for slot in range(1, (promos_guess_config.winners_per_day or 3) + 1):
                winner = won_by_slot.get(slot)
                if winner:
                    promos_guess_today_slots.append({
                        'slot': slot, 'status': 'won',
                        'number': winner.guessed_number, 'player_id': winner.player_id,
                    })
                elif slot == len(promos_guess_winners_today) + 1 and promos_guess_round_today:
                    promos_guess_today_slots.append({
                        # El número vigente NO se muestra (ni se envía a la página):
                        # es el que gana ahora mismo, y verlo en el panel, en una
                        # captura o en una pantalla compartida lo filtraría.
                        'slot': slot, 'status': 'current',
                        'number': None, 'player_id': None,
                    })
                else:
                    promos_guess_today_slots.append({'slot': slot, 'status': 'pending', 'number': None, 'player_id': None})

        from ..utils.promos import _horde_week_key, get_horde_week_ranking
        promos_horde_config = PromoHordeConfig.query.filter_by(game_id=promos_selected_game.id).first()
        promos_horde_week_key = _horde_week_key()
        promos_horde_ranking = get_horde_week_ranking(promos_selected_game.id, promos_horde_week_key)
        promos_recent_horde_winners = (
            PromoHordeWinner.query
            .filter_by(game_id=promos_selected_game.id)
            .order_by(PromoHordeWinner.week_key.desc(), PromoHordeWinner.place.asc())
            .limit(15).all()
        )
        from ..models import PromoHordeBan
        promos_horde_bans = (
            PromoHordeBan.query.filter_by(game_id=promos_selected_game.id)
            .order_by(PromoHordeBan.created_at.desc()).all()
        )

        promos_accumulated_sort_order = next(
            (lvl.sort_order for lvl in promos_levels.values() if lvl.sort_order is not None), 100
        )
        promos_level_delivery = {
            number: _prize_package_delivery_status(lvl.package)
            for number, lvl in promos_levels.items()
        }

        promos_context = dict(
            selected_game=promos_selected_game,
            game_packages=promos_game_packages,
            levels=promos_levels,
            level_delivery=promos_level_delivery,
            accumulated_sort_order=promos_accumulated_sort_order,
            raffle_config=promos_raffle_config,
            guess_config=promos_guess_config,
            recent_awards=promos_recent_awards,
            recent_raffle_winners=promos_recent_raffle_winners,
            recent_guess_winners=promos_recent_guess_winners,
            guess_today_slots=promos_guess_today_slots,
            guess_recent_attempts=promos_guess_recent_attempts,
            today_key=promos_today_key,
            horde_config=promos_horde_config,
            horde_week_key=promos_horde_week_key,
            horde_ranking=promos_horde_ranking,
            recent_horde_winners=promos_recent_horde_winners,
            horde_characters=get_horde_characters(),
            horde_bans=promos_horde_bans,
            horde_pending_count=PromoHordeWinner.query.filter_by(
                game_id=promos_selected_game.id, status='pending').count(),
        )

    # Participación diaria de Sorteo Diario y Adivina el Número: cuántas
    # personas distintas jugaron cada día, por juego. Se arma directo de
    # las tablas de registros/intentos (que ya quedan archivadas por
    # fecha en day_key, nunca se borran), no hace falta una tabla aparte.
    all_game_names = {g.id: g.name for g in Game.query.all()}

    raffle_participation = [
        {'day_key': day_key, 'game_name': all_game_names.get(game_id, f'Juego #{game_id}'), 'participants': count}
        for day_key, game_id, count in (
            db.session.query(
                PromoRaffleEntry.day_key, PromoRaffleEntry.game_id,
                func.count(func.distinct(PromoRaffleEntry.player_id)),
            )
            .group_by(PromoRaffleEntry.day_key, PromoRaffleEntry.game_id)
            .order_by(PromoRaffleEntry.day_key.desc())
            .limit(120)
            .all()
        )
    ]

    guess_participation = [
        {'day_key': day_key, 'game_name': all_game_names.get(game_id, f'Juego #{game_id}'), 'participants': count}
        for day_key, game_id, count in (
            db.session.query(
                PromoGuessAttempt.day_key, PromoGuessAttempt.game_id,
                func.count(func.distinct(PromoGuessAttempt.player_id)),
            )
            .group_by(PromoGuessAttempt.day_key, PromoGuessAttempt.game_id)
            .order_by(PromoGuessAttempt.day_key.desc())
            .limit(120)
            .all()
        )
    ]

    return render_template(
        'admin/minigames.html',
        slot_defs=slot_defs,
        slots_config=slots_config,
        all_games=all_games,
        packages_by_game_id=packages_by_game_id,
        win_interval=win_interval,
        counter_cards=counter_cards,
        winners=winners,
        accumulated_winners=accumulated_winners,
        accumulated_overview=get_accumulated_safety_overview(),
        minigame_dev_mode=is_minigame_dev_mode(),
        raffle_participation=raffle_participation,
        guess_participation=guess_participation,
        **promos_context,
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

        if form_action == 'add_redeem_option':
            game_id = (request.form.get('redeem_game_id', '') or '').strip()
            package_id = (request.form.get('redeem_package_id', '') or '').strip()
            points_cost_raw = (request.form.get('redeem_points_cost', '') or '').strip()
            if not game_id or not package_id:
                flash('Elige un juego y un paquete para el canje directo.', 'danger')
                return redirect(url_for('admin_bp.points'))
            try:
                points_cost = int(float(points_cost_raw))
            except (TypeError, ValueError):
                points_cost = 0
            if points_cost <= 0:
                flash('El costo en puntos debe ser mayor a 0.', 'danger')
                return redirect(url_for('admin_bp.points'))

            db.session.add(PointsRedeemOption(
                game_id=int(game_id), package_id=int(package_id),
                points_cost=points_cost, is_active=True,
            ))
            db.session.commit()
            flash('Paquete de canje directo agregado.', 'success')
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

    redeem_options = (
        PointsRedeemOption.query
        .options(joinedload(PointsRedeemOption.game), joinedload(PointsRedeemOption.package))
        .join(Game, Game.id == PointsRedeemOption.game_id)
        .order_by(Game.name.asc(), PointsRedeemOption.points_cost.asc())
        .all()
    )
    recent_redeems = (
        PointsRedeemLog.query
        .options(joinedload(PointsRedeemLog.game), joinedload(PointsRedeemLog.package), joinedload(PointsRedeemLog.prize_order))
        .order_by(PointsRedeemLog.created_at.desc())
        .limit(30)
        .all()
    )

    mappings = (
        PointsPrizeMapping.query
        .options(joinedload(PointsPrizeMapping.game), joinedload(PointsPrizeMapping.package))
        .join(Game, Game.id == PointsPrizeMapping.game_id)
        .order_by(Game.name.asc())
        .all()
    )

    # Los giros se acumulan sin parar: se muestran por páginas en vez de
    # recortarlos a los últimos 100.
    spins_page = request.args.get('spins_page', type=int) or 1
    if spins_page < 1:
        spins_page = 1
    spins_page_size = 30

    spins_query = (
        PointsSpinLog.query
        .options(joinedload(PointsSpinLog.game), joinedload(PointsSpinLog.prize_order))
        .order_by(PointsSpinLog.created_at.desc())
    )
    spins_total = PointsSpinLog.query.count()
    spins_total_pages = max(1, (spins_total + spins_page_size - 1) // spins_page_size)
    if spins_page > spins_total_pages:
        spins_page = spins_total_pages

    recent_spins = (
        spins_query
        .offset((spins_page - 1) * spins_page_size)
        .limit(spins_page_size)
        .all()
    )
    spins_page_numbers = list(range(
        max(1, spins_page - 2), min(spins_total_pages, spins_page + 2) + 1
    ))
    spins_start_index = ((spins_page - 1) * spins_page_size) + 1 if spins_total else 0
    spins_end_index = min(spins_page * spins_page_size, spins_total) if spins_total else 0

    # Los saldos también se acumulan: se paginan en vez de cortar en 50.
    balances_page = request.args.get('balances_page', type=int) or 1
    if balances_page < 1:
        balances_page = 1
    balances_page_size = 30

    balances_total = PlayerPoints.query.count()
    balances_total_pages = max(1, (balances_total + balances_page_size - 1) // balances_page_size)
    if balances_page > balances_total_pages:
        balances_page = balances_total_pages

    top_balances = (
        PlayerPoints.query
        .options(joinedload(PlayerPoints.game))
        .order_by(PlayerPoints.points_balance.desc())
        .offset((balances_page - 1) * balances_page_size)
        .limit(balances_page_size)
        .all()
    )
    balances_page_numbers = list(range(
        max(1, balances_page - 2), min(balances_total_pages, balances_page + 2) + 1
    ))
    balances_start_index = ((balances_page - 1) * balances_page_size) + 1 if balances_total else 0
    balances_end_index = min(balances_page * balances_page_size, balances_total) if balances_total else 0

    return render_template(
        'admin/points.html',
        all_games=all_games,
        packages_by_game_id=packages_by_game_id,
        mappings=mappings,
        redeem_options=redeem_options,
        recent_redeems=recent_redeems,
        recent_spins=recent_spins,
        spins_page=spins_page,
        spins_total=spins_total,
        spins_total_pages=spins_total_pages,
        spins_page_numbers=spins_page_numbers,
        spins_start_index=spins_start_index,
        spins_end_index=spins_end_index,
        top_balances=top_balances,
        balances_page=balances_page,
        balances_total=balances_total,
        balances_total_pages=balances_total_pages,
        balances_page_numbers=balances_page_numbers,
        balances_start_index=balances_start_index,
        balances_end_index=balances_end_index,
        points_per_dollar=get_points_per_dollar_rate(),
        points_spin_cost=get_points_spin_cost(),
        points_win_interval=get_points_win_interval(),
        gifts_recent=_recent_point_gifts(),
        gift_max=account_points.MAX_POINTS_PER_MOVE,
    )


def _recent_point_gifts(limit=10):
    """Los últimos puntos que el administrador le regaló a cuentas: [(movimiento, cuenta)]."""
    return (
        db.session.query(AccountPointsLog, User)
        .join(User, User.id == AccountPointsLog.user_id)
        .filter(AccountPointsLog.kind == 'admin', AccountPointsLog.delta > 0)
        .order_by(AccountPointsLog.id.desc())
        .limit(limit)
        .all()
    )


@admin_bp.route('/points/regalar', methods=['POST'])
@login_required
def points_gift():
    """Regala puntos de la tienda (los que sirven para girar la ruleta y canjear paquetes) a una CUENTA, buscándola por su usuario o su correo.
    Quedan en la bolsa del perfil; su dueño los pasa al ID que quiera desde "Canjear puntos"."""
    back = url_for('admin_bp.points') + '#regalarPuntos'
    target = (request.form.get('account') or '').strip().lstrip('@')[:255]
    raw_amount = (request.form.get('amount') or '').strip()
    note = clean_text(request.form.get('note'), 150)
    if not target:
        flash('Escribe el usuario o el correo de la cuenta.', 'danger')
        return redirect(back)
    if not raw_amount.isdigit() or not 1 <= int(raw_amount) <= account_points.MAX_POINTS_PER_MOVE:
        flash('Escribe cuántos puntos regalar (un número entero mayor que 0).', 'danger')
        return redirect(back)
    needle = target.lower()
    accounts = User.query.filter(User.account_kind.in_(ACCOUNT_KINDS))
    user = accounts.filter(func.lower(User.username) == needle).first() or accounts.filter(func.lower(User.email) == needle).first()
    if user is None:
        flash(f'No encontré ninguna cuenta con el usuario o correo "{target}". Revisa que esté bien escrito (solo cuentas registradas).', 'danger')
        return redirect(back)
    if not user.is_active:
        flash('Esa cuenta está desactivada: no se le pueden regalar puntos.', 'danger')
        return redirect(back)
    amount = int(raw_amount)
    account_points.credit(user.id, amount, kind='admin', note=note or 'Regalo del administrador')
    db.session.commit()
    flash(f'Le regalaste {amount} puntos a @{user.username}. Ahora tiene {account_points.balance(user.id)} puntos en su perfil; los pasa a un ID desde "Canjear puntos".', 'success')
    return redirect(back)


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


@admin_bp.route('/points/redeem-options/<int:option_id>/toggle', methods=['POST'])
@login_required
def points_redeem_option_toggle(option_id):
    option = PointsRedeemOption.query.get_or_404(option_id)
    option.is_active = not option.is_active
    db.session.commit()
    flash('Paquete de canje directo actualizado.', 'success')
    return redirect(url_for('admin_bp.points'))


@admin_bp.route('/points/redeem-options/<int:option_id>/delete', methods=['POST'])
@login_required
def points_redeem_option_delete(option_id):
    option = PointsRedeemOption.query.get_or_404(option_id)
    db.session.delete(option)
    db.session.commit()
    flash('Paquete de canje directo eliminado.', 'warning')
    return redirect(url_for('admin_bp.points'))


# ─── Promociones: Recarga Acumulada / Sorteo Diario / Adivina el Número ──────
# Viven dentro de la página de Mini Juegos (/admin/minigames); esta ruta
# vieja solo redirige ahí por si quedó algún enlace guardado.

@admin_bp.route('/promos', methods=['GET'])
@login_required
def promos():
    return redirect(url_for('admin_bp.minigames', game_id=request.args.get('game_id', type=int)))


@admin_bp.route('/promos/accumulated', methods=['POST'])
@login_required
def promos_accumulated_save():
    game_id = request.form.get('game_id', type=int)
    game = Game.query.get_or_404(game_id)
    sort_order = request.form.get('sort_order', type=int) or 100

    new_levels = []
    for number in (1, 2, 3):
        name = (request.form.get(f'level_{number}_name', '') or '').strip()
        threshold_raw = (request.form.get(f'level_{number}_threshold', '') or '').strip()
        package_id = request.form.get(f'level_{number}_package_id', type=int)
        if not name or not threshold_raw or not package_id:
            continue
        try:
            threshold = int(float(threshold_raw))
        except ValueError:
            continue
        if threshold <= 0:
            continue
        new_levels.append((number, name, threshold, package_id))

    PromoAccumulatedLevel.query.filter_by(game_id=game.id).delete()
    for number, name, threshold, package_id in new_levels:
        db.session.add(PromoAccumulatedLevel(
            game_id=game.id, level_number=number, level_name=name,
            threshold_amount=threshold, package_id=package_id,
            sort_order=sort_order,
        ))
    db.session.commit()

    if new_levels:
        flash(f'Recarga Acumulada actualizada para {game.name} ({len(new_levels)} nivel(es)).', 'success')
    else:
        flash(f'Recarga Acumulada desactivada para {game.name} (sin niveles completos).', 'warning')
    return redirect(url_for('admin_bp.minigames', game_id=game.id, _anchor='recarga-acumulada'))


@admin_bp.route('/promos/accumulated/toggle', methods=['POST'])
@login_required
def promos_accumulated_toggle():
    """Interruptor general: apaga la promo en toda la tienda sin borrar los
    niveles configurados (antes la única forma era vaciarlos)."""
    enable = request.form.get('enabled') == '1'
    _upsert_setting(ACC_ENABLED_KEY, '1' if enable else '0')
    db.session.commit()
    if enable:
        flash('Recarga Acumulada ENCENDIDA: vuelve a verse en la tienda y las compras suman a la barra.', 'success')
    else:
        flash('Recarga Acumulada APAGADA: ya no se ve en la tienda, las compras no suman y no se entrega ningún premio.', 'warning')
    return redirect(url_for('admin_bp.minigames', game_id=request.form.get('game_id', type=int), _anchor='recarga-acumulada'))


@admin_bp.route('/promos/accumulated/limits', methods=['POST'])
@login_required
def promos_accumulated_limits():
    """Topes de premios automáticos por día. Lo que pase de un tope no se
    pierde: queda RETENIDO en Órdenes para aprobarlo o rechazarlo a mano."""
    def _read_limit(name, default):
        try:
            return max(0, int(float(request.form.get(name, ''))))
        except (TypeError, ValueError):
            return default

    per_id = _read_limit('max_per_id_day', ACC_DEFAULT_MAX_PRIZES_PER_ID_PER_DAY)
    per_day = _read_limit('max_per_day', ACC_DEFAULT_MAX_PRIZES_PER_DAY)
    _upsert_setting(ACC_MAX_PRIZES_PER_ID_PER_DAY_KEY, str(per_id))
    _upsert_setting(ACC_MAX_PRIZES_PER_DAY_KEY, str(per_day))
    db.session.commit()
    flash(f'Seguros guardados: {per_id} premio(s) por ID al día y {per_day} en toda la tienda al día. '
          'Lo que pase de ahí queda retenido para revisión.', 'success')
    return redirect(url_for('admin_bp.minigames', game_id=request.form.get('game_id', type=int), _anchor='recarga-acumulada'))


@admin_bp.route('/promos/raffle', methods=['POST'])
@login_required
def promos_raffle_save():
    game_id = request.form.get('game_id', type=int)
    game = Game.query.get_or_404(game_id)

    is_active = request.form.get('is_active') == 'on'
    draw_hour = request.form.get('draw_hour', type=int)
    draw_minute = request.form.get('draw_minute', type=int)
    winners_per_draw = request.form.get('winners_per_draw', type=int)
    package_id = request.form.get('package_id', type=int)
    require_verification = request.form.get('require_verification') == 'on'
    sort_order = request.form.get('sort_order', type=int) or 100

    config = PromoRaffleConfig.query.filter_by(game_id=game.id).first()
    if not config:
        config = PromoRaffleConfig(game_id=game.id)
        db.session.add(config)

    config.is_active = bool(is_active and package_id)
    config.draw_hour = draw_hour if draw_hour is not None and 0 <= draw_hour <= 23 else 21
    config.draw_minute = draw_minute if draw_minute is not None and 0 <= draw_minute <= 59 else 0
    config.winners_per_draw = winners_per_draw if winners_per_draw and winners_per_draw > 0 else 5
    config.package_id = package_id
    config.require_verification = require_verification
    config.sort_order = sort_order
    db.session.commit()

    flash(f'Sorteo Diario {"activado" if config.is_active else "guardado (inactivo)"} para {game.name}.', 'success')
    return redirect(url_for('admin_bp.minigames', game_id=game.id))


@admin_bp.route('/promos/raffle/reset', methods=['POST'])
@login_required
def promos_raffle_reset():
    """Borra los registros y ganadores de HOY del Sorteo Diario de este
    juego, para poder probarlo de nuevo sin esperar a que empiece el día
    siguiente (cuando ya hay ganadores hoy, run_daily_raffle_draws no
    vuelve a sortear hasta que cambie day_key). No toca los premios que ya
    se entregaron de verdad — esos quedan repartidos igual; esto solo
    reabre el sorteo de hoy para que se pueda correr otra vez."""
    game_id = request.form.get('game_id', type=int)
    game = Game.query.get_or_404(game_id)

    day_key = today_ve_str()
    winners_deleted = PromoRaffleWinner.query.filter_by(game_id=game.id, day_key=day_key).delete()
    entries_deleted = PromoRaffleEntry.query.filter_by(game_id=game.id, day_key=day_key).delete()
    db.session.commit()

    flash(
        f'Sorteo Diario de {game.name} reiniciado para hoy: '
        f'{entries_deleted} registro(s) y {winners_deleted} ganador(es) borrados. '
        'Los premios ya entregados no se revirtieron.',
        'success',
    )
    return redirect(url_for('admin_bp.minigames', game_id=game.id))


@admin_bp.route('/promos/guess', methods=['POST'])
@login_required
def promos_guess_save():
    game_id = request.form.get('game_id', type=int)
    game = Game.query.get_or_404(game_id)

    is_active = request.form.get('is_active') == 'on'
    number_max = request.form.get('number_max', type=int)
    max_attempts = request.form.get('max_attempts', type=int)
    winners_per_day = request.form.get('winners_per_day', type=int)
    package_id = request.form.get('package_id', type=int)
    require_verification = request.form.get('require_verification') == 'on'
    sort_order = request.form.get('sort_order', type=int) or 100

    config = PromoGuessConfig.query.filter_by(game_id=game.id).first()
    if not config:
        config = PromoGuessConfig(game_id=game.id)
        db.session.add(config)

    config.is_active = bool(is_active and package_id)
    config.number_max = number_max if number_max and number_max > 1 else 50
    config.max_attempts = max_attempts if max_attempts and max_attempts > 0 else 2
    config.winners_per_day = winners_per_day if winners_per_day and winners_per_day > 0 else 3
    config.package_id = package_id
    config.require_verification = require_verification
    config.sort_order = sort_order
    db.session.commit()

    flash(f'Adivina el Número {"activado" if config.is_active else "guardado (inactivo)"} para {game.name}.', 'success')
    return redirect(url_for('admin_bp.minigames', game_id=game.id))


@admin_bp.route('/promos/horde', methods=['POST'])
@login_required
def promos_horde_save():
    game_id = request.form.get('game_id', type=int)
    game = Game.query.get_or_404(game_id)

    is_active = request.form.get('is_active') == 'on'
    package_id = request.form.get('package_id', type=int)
    package_id_2 = request.form.get('package_id_2', type=int) or None
    package_id_3 = request.form.get('package_id_3', type=int) or None
    runs_per_day = request.form.get('runs_per_day', type=int)
    require_verification = request.form.get('require_verification') == 'on'
    sort_order = request.form.get('sort_order', type=int) or 100

    config = PromoHordeConfig.query.filter_by(game_id=game.id).first()
    if not config:
        config = PromoHordeConfig(game_id=game.id)
        db.session.add(config)

    config.is_active = bool(is_active and package_id)
    config.package_id = package_id
    config.package_id_2 = package_id_2
    config.package_id_3 = package_id_3
    config.winners_per_week = 1 + bool(package_id_2) + bool(package_id_3)
    config.runs_per_day = runs_per_day if runs_per_day and runs_per_day > 0 else 5
    extra_cost = request.form.get('points_per_extra_run', type=int)
    config.points_per_extra_run = extra_cost if extra_cost and extra_cost > 0 else 0
    config.require_verification = require_verification
    config.sort_order = sort_order
    db.session.commit()

    flash(f'Hordas de Diamantes {"activado" if config.is_active else "guardado (inactivo)"} para {game.name}.', 'success')
    return redirect(url_for('admin_bp.minigames', game_id=game.id))


def _upsert_setting(key, value):
    row = Setting.query.filter_by(key=key).first()
    if row:
        row.value = value
    else:
        db.session.add(Setting(key=key, value=value))


@admin_bp.route('/promos/horde/image', methods=['POST'])
@login_required
def promos_horde_image_save():
    game_id = request.form.get('game_id', type=int)
    slot = request.form.get('slot', type=int) or 1
    if not 1 <= slot <= HORDE_CHARACTER_SLOTS:
        slot = 1
    keys = horde_character_keys(slot)
    mode = request.form.get('image_mode', 'flip')
    if mode not in HORDE_CHARACTER_MODES:
        mode = 'flip'
    name = (request.form.get('name') or '').strip()[:24]

    def current_value(key):
        row = Setting.query.filter_by(key=key).first()
        return row.value if row else ''

    if request.form.get('remove_image') == 'on':
        delete_uploaded_file(current_value(keys['image']))
        _upsert_setting(keys['image'], '')
        db.session.commit()
        flash(f'Diseño del personaje {slot} eliminado: ya no aparece para elegir.', 'success')
        return redirect(url_for('admin_bp.minigames', game_id=game_id))

    # Diseño (el que se ve jugando) y portada (la que se ve al elegir).
    # Primero se guardan los archivos nuevos; si alguno no es imagen, no se
    # cambia nada. Los archivos viejos se borran solo después del commit.
    new_files = {}
    for field, key in (('player_image', keys['image']), ('cover_image', keys['cover'])):
        file = request.files.get(field)
        if file and file.filename:
            new_path = save_image(file, 'hordas')
            if not new_path:
                for path in new_files.values():
                    delete_uploaded_file(path)
                flash('La imagen debe ser PNG, JPG, GIF o WEBP (mejor PNG con fondo transparente).', 'danger')
                return redirect(url_for('admin_bp.minigames', game_id=game_id))
            new_files[key] = new_path

    old_files = []
    if request.form.get('remove_cover') == 'on' and keys['cover'] not in new_files:
        new_files[keys['cover']] = ''
    for key, new_path in new_files.items():
        old_files.append(current_value(key))
        _upsert_setting(key, new_path)

    _upsert_setting(keys['mode'], mode)
    _upsert_setting(keys['name'], name)
    db.session.commit()
    for path in old_files:
        if path:
            delete_uploaded_file(path)
    flash(f'Personaje {slot} de Hordas de Diamantes actualizado.', 'success')
    return redirect(url_for('admin_bp.minigames', game_id=game_id))


# ─── Asistente de IA del soporte ──────────────────────────────────────────────

# ─── Bloqueo de IDs en Hordas de Diamantes ────────────────────────────────────

def _horde_back(game_id):
    """Vuelve a la página desde donde se bloqueó (Mini Juegos o Repeticiones)."""
    back = request.form.get('back') or ''
    if back.startswith('/admin/') and not back.startswith('//'):
        return redirect(back)
    return redirect(url_for('admin_bp.minigames', game_id=game_id))


@admin_bp.route('/promos/horde/ban', methods=['POST'])
@login_required
def promos_horde_ban():
    from ..models import PromoHordeBan

    game_id = request.form.get('game_id', type=int)
    game = Game.query.get_or_404(game_id)
    from ..utils.promos import canonical_player_id
    player_id = canonical_player_id(request.form.get('player_id'))[:100]   # el mismo ID canónico con el que juegan: '0123' y '123' son uno
    reason = (request.form.get('reason') or '').strip()[:200] or 'Trampa'
    if not player_id:
        flash('Escribe el ID que quieres bloquear.', 'danger')
        return _horde_back(game.id)
    if PromoHordeBan.query.filter_by(game_id=game.id, player_id=player_id).first():
        flash(f'El ID {player_id} ya estaba bloqueado en Hordas de {game.name}.', 'info')
        return _horde_back(game.id)
    db.session.add(PromoHordeBan(game_id=game.id, player_id=player_id, reason=reason))
    db.session.commit()
    try:
        from ..utils.promos import refresh_pending_horde_winners
        refresh_pending_horde_winners(game.id)   # si ese ID era ganador pendiente, sale y suben los siguientes
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Hordas: no se pudieron rearmar los ganadores tras bloquear un ID')
    flash(f'ID {player_id} bloqueado en Hordas de {game.name}: ya no puede jugar ni sale en el ranking.', 'success')
    return _horde_back(game.id)


@admin_bp.route('/promos/horde/unban/<int:ban_id>', methods=['POST'])
@login_required
def promos_horde_unban(ban_id):
    from ..models import PromoHordeBan

    ban = PromoHordeBan.query.get_or_404(ban_id)
    game_id, player_id = ban.game_id, ban.player_id
    db.session.delete(ban)
    db.session.commit()
    flash(f'ID {player_id} desbloqueado: puede volver a jugar y vuelve a salir en el ranking.', 'success')
    return _horde_back(game_id)


# ─── Aprobación de premios de Hordas ──────────────────────────────────────────

def _horde_winner_unverified_summary(winner):
    """Texto con lo que NO se ha comprobado de las partidas de este ganador en la semana ('' si todo está en orden).
    Las partidas se aceptan por lo que dice el teléfono (con topes); lo que las respalda es la repetición que el admin reproduce."""
    from ..models import PromoHordeReplay, PromoHordeRun

    runs = (
        PromoHordeRun.query
        .filter(PromoHordeRun.game_id == winner.game_id, PromoHordeRun.week_key == winner.week_key,
                PromoHordeRun.player_id == winner.player_id, PromoHordeRun.finished_at.isnot(None),
                PromoHordeRun.diamonds > 0)
        .all()
    )
    if not runs:
        return ''
    with_replay = {
        run_id for (run_id,) in db.session.query(PromoHordeReplay.run_id)
        .filter(PromoHordeReplay.run_id.in_([r.id for r in runs]))
    }
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


@admin_bp.route('/promos/horde/winner/<int:winner_id>/approve', methods=['POST'])
@login_required
def promos_horde_winner_approve(winner_id):
    from ..utils.promos import approve_horde_winner

    winner = PromoHordeWinner.query.get_or_404(winner_id)
    game_id = winner.game_id
    # Aviso de dos pasos: si hay partidas sin respaldo, la primera pulsación solo avisa y la segunda aprueba.
    unverified = _horde_winner_unverified_summary(winner)
    warn_key = f'horde_approve_warned:{winner_id}'
    if unverified and not session.get(warn_key):
        session[warn_key] = True
        flash(f'Antes de aprobar a {winner.player_id}: {unverified}. Revisa sus repeticiones en "Premios y repeticiones". '
              'Si aun así quieres aprobarlo, pulsa Aprobar otra vez.', 'warning')
        return _horde_back(game_id)
    session.pop(warn_key, None)
    try:
        winner = approve_horde_winner(winner_id)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return _horde_back(game_id)
    except Exception as exc:
        flash(f'No se pudo entregar el premio: {exc}. Sigue pendiente, intenta de nuevo.', 'danger')
        return _horde_back(game_id)
    if winner.prize_order:
        flash(f'Premio aprobado: {winner.player_id} (puesto #{winner.place}). Orden #{winner.prize_order.order_number} creada.', 'success')
    else:
        flash(f'Ganador aprobado: {winner.player_id} (puesto #{winner.place}). Ese puesto no tenía paquete asignado.', 'success')
    return _horde_back(game_id)


@admin_bp.route('/promos/horde/winner/<int:winner_id>/reject', methods=['POST'])
@login_required
def promos_horde_winner_reject(winner_id):
    from ..utils.promos import reject_horde_winner

    winner = PromoHordeWinner.query.get_or_404(winner_id)
    game_id, player_id = winner.game_id, winner.player_id
    reason = (request.form.get('reason') or '').strip()
    try:
        reject_horde_winner(winner_id, reason)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return _horde_back(game_id)
    flash(f'Premio rechazado: el ID {player_id} quedó bloqueado en este juego y los siguientes del ranking subieron de puesto.', 'success')
    return _horde_back(game_id)


# ─── Repeticiones de Hordas de Diamantes ──────────────────────────────────────

HORDE_REPLAY_TOP = 5


@admin_bp.route('/promos/horde/replays')
@login_required
def promos_horde_replays():
    """Top 5 de una semana con todas sus partidas y sus repeticiones."""
    from ..models import PromoHordeReplay, PromoHordeRun
    from ..utils.promos import _horde_week_key, get_horde_week_ranking

    game_id = request.args.get('game_id', type=int)
    game = Game.query.get_or_404(game_id)
    current_week = _horde_week_key()
    weeks = sorted(
        {w for (w,) in db.session.query(PromoHordeRun.week_key).filter(PromoHordeRun.game_id == game.id).distinct()},
        reverse=True,
    )
    # Por defecto, la semana con premios por aprobar; si no hay, la última cerrada.
    pending = (
        PromoHordeWinner.query.filter_by(game_id=game.id, status='pending')
        .order_by(PromoHordeWinner.week_key.desc()).first()
    )
    default_week = pending.week_key if pending else next((w for w in weeks if w < current_week), current_week)
    week = request.args.get('week') or default_week

    ranking = get_horde_week_ranking(game.id, week, limit=HORDE_REPLAY_TOP)
    players = []
    for place, (pid, nick, total) in enumerate(ranking, start=1):
        runs = (
            PromoHordeRun.query
            .filter(PromoHordeRun.game_id == game.id, PromoHordeRun.week_key == week,
                    PromoHordeRun.player_id == pid, PromoHordeRun.finished_at.isnot(None),
                    PromoHordeRun.diamonds > 0)
            .order_by(PromoHordeRun.diamonds.desc())
            .all()
        )
        with_replay = {
            run_id for (run_id,) in db.session.query(PromoHordeReplay.run_id)
            .filter(PromoHordeReplay.run_id.in_([r.id for r in runs] or [0]))
        }
        players.append({
            'place': place, 'player_id': pid, 'nick': nick, 'total': total,
            'runs': [{'run': r, 'has_replay': r.id in with_replay,
                      'seconds': int((r.finished_at - r.started_at).total_seconds())} for r in runs],
            'bad': sum(1 for r in runs if r.replay_check == 'no_coincide' or r.flag),
            'unchecked': sum(1 for r in runs if r.id in with_replay and not r.replay_check),
        })
    week_winners = (
        PromoHordeWinner.query.filter_by(game_id=game.id, week_key=week)
        .order_by(PromoHordeWinner.place.asc()).all()
    )
    player_info = {pl['player_id']: pl for pl in players}
    return render_template(
        'admin/horde_replays.html', game=game, week=week, weeks=weeks,
        current_week=current_week, players=players,
        week_winners=week_winners, player_info=player_info,
    )


def _horde_replay_run(run_id):
    from ..models import PromoHordeReplay, PromoHordeRun

    run = PromoHordeRun.query.get_or_404(run_id)
    replay = PromoHordeReplay.query.filter_by(run_id=run.id).first()
    if not replay or run.seed is None:
        abort(404)
    return run, replay


@admin_bp.route('/promos/horde/replay/<int:run_id>')
@login_required
def promos_horde_replay_view(run_id):
    """Abre el juego en modo repetición (o comprobación rápida con ?check=1)."""
    run, replay = _horde_replay_run(run_id)
    characters = [
        {'slot': c['slot'], 'name': c['name'], 'mode': c['mode'],
         'url': url_for('static', filename='uploads/' + c['image']),
         'cover_url': url_for('static', filename='uploads/' + (c['cover'] or c['image']))}
        for c in get_horde_characters() if c['image']
    ]
    seconds = int((run.finished_at - run.started_at).total_seconds()) if run.finished_at else 0
    replay_data = {
        'run_id': run.id,
        'check': request.args.get('check') == '1',
        'seed': run.seed,
        'W': replay.width, 'H': replay.height, 'ch': replay.character_slot or 0,
        'rules': replay.rules or 1,
        'data': replay.data,
        'reported': {'wave': run.wave or 0, 'claimed': run.claimed_diamonds or 0},
        'verdict_url': url_for('admin_bp.promos_horde_replay_verdict', run_id=run.id),
        'player_label': f'{run.player_nick or run.player_id} · {seconds // 60}:{seconds % 60:02d}',
    }
    game = Game.query.get(run.game_id)
    return render_template(
        'promos/hordas.html', games=[game], game=game,
        verifiable=False, characters=characters, horde_extra_cost=0, replay=replay_data,
    )


@admin_bp.route('/promos/horde/replay/<int:run_id>/verdict', methods=['POST'])
@login_required
def promos_horde_replay_verdict(run_id):
    run, _replay = _horde_replay_run(run_id)
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


# ─── Notificaciones (campana de la tienda + push) ─────────────────────────────

@admin_bp.route('/notifications', methods=['GET', 'POST'])
@login_required
def notifications():
    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()[:TITLE_MAX]
        body = (request.form.get('body') or '').strip()[:BODY_MAX]
        link = clean_link(request.form.get('url'))
        in_bell = request.form.get('in_bell') == '1'
        as_push = request.form.get('as_push') == '1'

        if not title or not body:
            flash('Título y mensaje son obligatorios.', 'danger')
            return redirect(url_for('admin_bp.notifications'))
        if link is None:
            flash('El link tiene que ser una página de la tienda (empieza con /) o una dirección https://.', 'danger')
            return redirect(url_for('admin_bp.notifications'))
        if not in_bell and not as_push:
            flash('Elige dónde mostrarla: en la campana de la tienda, por push, o en las dos.', 'danger')
            return redirect(url_for('admin_bp.notifications'))

        if in_bell:
            db.session.add(SiteNotification(title=title, body=body, url=link or None))
            db.session.commit()
            flash('Notificación publicada en la campana de la tienda: la verán todos.', 'success')
        if as_push:
            if not is_push_configured():
                flash('Las notificaciones push todavía no están listas en el servidor.', 'danger')
            else:
                result = send_push_broadcast(title[:60], body[:180], url=link or None)
                flash(
                    f'Push enviado: {result["sent"]} entregada(s), {result["failed"]} fallida(s).',
                    'success' if result['sent'] else 'warning',
                )
        return redirect(url_for('admin_bp.notifications'))

    published = (
        SiteNotification.query
        .order_by(SiteNotification.created_at.desc(), SiteNotification.id.desc())
        .limit(100)
        .all()
    )
    return render_template(
        'admin/notifications.html',
        subscriber_count=PushSubscription.query.count(),
        push_configured=is_push_configured(),
        published=published,
        title_max=TITLE_MAX,
        body_max=BODY_MAX,
    )


@admin_bp.route('/notifications/<int:notification_id>/toggle', methods=['POST'])
@login_required
def notification_toggle(notification_id):
    row = SiteNotification.query.get_or_404(notification_id)
    row.is_active = not row.is_active
    db.session.commit()
    flash('Notificación visible en la campana.' if row.is_active else 'Notificación oculta: ya no sale en la campana.', 'success')
    return redirect(url_for('admin_bp.notifications'))


@admin_bp.route('/notifications/<int:notification_id>/delete', methods=['POST'])
@login_required
def notification_delete(notification_id):
    row = SiteNotification.query.get_or_404(notification_id)
    db.session.delete(row)
    db.session.commit()
    flash('Notificación eliminada.', 'success')
    return redirect(url_for('admin_bp.notifications'))


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


def _delete_package_delivery_config(package_ids):
    """Borra todo lo que entrega estos paquetes: los mapeos de Revendedores y
    de Central One (con sus items) y su plan de principal/respaldo."""
    if not package_ids:
        return
    for mapping_model, item_model in (
        (RevendedoresItemMapping, RevendedoresMappingItem),
        (CentralOneItemMapping, CentralOneMappingItem),
    ):
        mapping_ids = [m.id for m in mapping_model.query.filter(mapping_model.store_package_id.in_(package_ids)).all()]
        if mapping_ids:
            item_model.query.filter(item_model.mapping_id.in_(mapping_ids)).delete(synchronize_session=False)
            mapping_model.query.filter(mapping_model.id.in_(mapping_ids)).delete(synchronize_session=False)
    PackageDeliveryPlan.query.filter(PackageDeliveryPlan.store_package_id.in_(package_ids)).delete(synchronize_session=False)


def _central_one_error_text(exc):
    code = getattr(exc, 'code', '')
    if code == 'invalid_api_key':
        return 'La llave de Central One no es válida, fue revocada o la cuenta está inactiva.'
    if code == 'insufficient_scope':
        return 'La llave de Central One no tiene permiso para esto (necesita catalog:read, balance:read, orders:read y orders:write).'
    if code == 'rate_limited':
        return 'Central One pidió esperar un momento. Intenta de nuevo en unos segundos.'
    return str(exc)


@admin_bp.route('/centralone/sync', methods=['POST'])
@login_required
def centralone_sync_catalog():
    _, api_key = get_centralone_env()
    if not api_key:
        return jsonify({'ok': False, 'error': 'Falta CENTRALONE_API_KEY en el servidor (archivo .env).'}), 400
    try:
        summary = sync_central_one_catalog()
    except (CentralOneError, CentralOneUnknown) as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'error': f'No se pudo sincronizar Central One: {_central_one_error_text(exc)}'}), 502
    return jsonify({'ok': True, 'source': 'api', **summary})


@admin_bp.route('/centralone/balance', methods=['GET'])
@login_required
def centralone_balance():
    _, api_key = get_centralone_env()
    if not api_key:
        return jsonify({'ok': False, 'error': 'Falta CENTRALONE_API_KEY en el servidor (archivo .env).'}), 400
    try:
        balance = fetch_central_one_balance()
    except (CentralOneError, CentralOneUnknown) as exc:
        return jsonify({'ok': False, 'error': _central_one_error_text(exc)}), 502
    payload = {
        'ok': True,
        'available': balance.get('available_balance'),
        'held': balance.get('held_balance'),
        'total': balance.get('total_balance'),
        'currency': balance.get('currency') or 'USD',
    }
    if request.args.get('check_codes'):
        # False = la llave NO puede ver códigos: los productos de código no llegarían al cliente.
        payload['codes_permission'] = probe_codes_permission()
    return jsonify(payload)


def _json_dict(text):
    try:
        value = json.loads(text or '')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@admin_bp.route('/revendedores/mapping-data', methods=['GET'])
@login_required
def revendedores_mapping_data():
    game_id = request.args.get('game_id', type=int)

    games = Game.query.filter_by(is_active=True).order_by(Game.name).all()
    packages_query = Package.query.filter_by(is_active=True)
    if game_id:
        packages_query = packages_query.filter_by(game_id=game_id)
    store_packages = packages_query.order_by(Package.sort_order.asc(), Package.id.asc()).all()
    package_ids = [p.id for p in store_packages]

    rev_by_pkg = {m.store_package_id: m for m in RevendedoresItemMapping.query.filter(
        RevendedoresItemMapping.store_package_id.in_(package_ids)).all()} if package_ids else {}
    co_by_pkg = {m.store_package_id: m for m in CentralOneItemMapping.query.filter(
        CentralOneItemMapping.store_package_id.in_(package_ids)).all()} if package_ids else {}
    plan_by_pkg = {p.store_package_id: p for p in PackageDeliveryPlan.query.filter(
        PackageDeliveryPlan.store_package_id.in_(package_ids)).all()} if package_ids else {}

    def _extract_price(raw_json_str):
        try:
            obj = json.loads(raw_json_str or '{}')
            p = obj.get('price') or obj.get('precio') or obj.get('cost')
            if p is not None:
                return str(round(float(p), 2))
        except Exception:
            pass
        return None

    rev_catalog = [
        {
            'catalog_id': r.id,
            'group': (r.remote_product_name or '').strip() or 'Otro',
            'name': (r.remote_package_name or '').strip() or f'Paquete {r.remote_package_id}',
            'price': _extract_price(r.raw_json),
        }
        for r in RevendedoresCatalogItem.query.filter_by(active=True).order_by(
            RevendedoresCatalogItem.remote_product_name.asc(),
            RevendedoresCatalogItem.remote_package_name.asc(),
            RevendedoresCatalogItem.id.asc(),
        ).all()
    ]

    co_catalog = []
    for r in CentralOneCatalogItem.query.filter_by(active=True).order_by(
            CentralOneCatalogItem.family_name.asc(), CentralOneCatalogItem.name.asc(), CentralOneCatalogItem.id.asc()).all():
        try:
            fields = json.loads(r.target_fields or '[]')
            schema = json.loads(r.target_schema or '[]')
        except ValueError:
            fields, schema = [], []
        co_catalog.append({
            'catalog_id': r.id,
            'group': (r.family_name or '').strip() or 'Otro',
            'name': (r.name or '').strip() or r.sku or r.product_id,
            'price': str(r.price) if r.price is not None else None,
            'in_stock': bool(r.in_stock),
            'available_stock': r.available_stock,
            'fields': fields,
            # Sin datos de jugador = producto de CÓDIGO (gift card / PIN), no recarga directa.
            'delivers_code': not r.requires_target and not fields,
            # Campos de lista (servidor): la tienda necesita saber su valor.
            'selects': [
                {'key': f.get('key'), 'label': f.get('label') or f.get('key'), 'options': f.get('options') or []}
                for f in schema if isinstance(f, dict) and f.get('type') == 'select'
            ],
        })

    def _route(provider, mapping):
        if provider == 'revendedores':
            items = (
                [{'catalog_id': it.catalog_item_id, 'quantity': it.quantity or 1} for it in mapping.items]
                if mapping.items else
                [{'catalog_id': cid, 'quantity': 1} for cid in (mapping.catalog_item_id, mapping.catalog_item_id_2) if cid]
            )
        else:
            items = [
                {'catalog_id': it.catalog_item_id, 'quantity': it.quantity or 1, 'target_defaults': _json_dict(it.target_defaults)}
                for it in mapping.items
            ]
        return {'provider': provider, 'items': items}

    deliveries = []
    for pid in package_ids:
        mappings = {'revendedores': rev_by_pkg.get(pid), 'centralone': co_by_pkg.get(pid)}
        if not any(mappings.values()):
            continue
        plan = plan_by_pkg.get(pid)
        order = [plan.primary_provider, plan.backup_provider] if plan else ['revendedores', 'centralone']
        routes = [_route(code, mappings[code]) for code in order if code and mappings.get(code)]
        if not plan or not routes:
            # Sin plan (o con un plan que apunta a un mapeo que ya no existe):
            # se muestra el único mapeo que haya, como lo entrega el sistema.
            routes = (routes or [_route(code, mappings[code]) for code in ('revendedores', 'centralone') if mappings.get(code)])[:1]
        deliveries.append({
            'store_package_id': pid,
            'auto_enabled': bool(mappings[routes[0]['provider']].auto_enabled),
            'primary': routes[0],
            'backup': routes[1] if len(routes) > 1 else None,
        })

    rev_base, rev_key = get_revendedores_env()[:2]
    co_base, co_key = get_centralone_env()
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
                # Sección de códigos de la web: el cliente recibe el código en su orden.
                'is_code_section': bool(p.game and p.game.category and (p.game.category.slug or '').lower() == 'tarjetas'),
            }
            for p in store_packages
        ],
        'providers': [
            {'code': 'revendedores', 'name': PROVIDER_LABELS['revendedores'], 'configured': bool(rev_base and rev_key)},
            {'code': 'centralone', 'name': PROVIDER_LABELS['centralone'], 'configured': bool(co_base and co_key)},
        ],
        'catalogs': {'revendedores': rev_catalog, 'centralone': co_catalog},
        'deliveries': deliveries,
    })


def _parse_mapping_items(raw_items, max_quantity=None):
    items = []
    for it in raw_items or []:
        cid_str = str((it or {}).get('catalog_id') or '').strip()
        if not cid_str:
            continue
        qty = max(1, int((it or {}).get('quantity') or 1))
        if max_quantity:
            qty = min(qty, max_quantity)
        items.append((int(cid_str), qty, (it or {}).get('target_defaults')))
    return items


def _save_revendedores_mapping(store_pkg_id, items, auto_enabled):
    """Guarda el mapeo de Revendedores de un paquete (o lo borra si no hay
    items). `items` = [(catalog_id, cantidad), ...]. Devuelve 'saved',
    'removed' o '' si no había nada que hacer."""
    existing = RevendedoresItemMapping.query.filter_by(store_package_id=store_pkg_id).first()
    if not items:
        if existing:
            db.session.delete(existing)
            return 'removed'
        return ''

    # catalog_item_id/catalog_item_id_2 se mantienen como espejo del primer y
    # segundo item para no romper mapeos leídos por código viejo; la lista
    # completa (con cantidad) vive en mapping.items.
    catalog_id = items[0][0]
    catalog_id_2 = items[1][0] if len(items) > 1 else None

    if existing:
        mapping = existing
        mapping.catalog_item_id = catalog_id
        mapping.catalog_item_id_2 = catalog_id_2
        mapping.auto_enabled = auto_enabled
        mapping.active = True
    else:
        mapping = RevendedoresItemMapping(
            store_package_id=store_pkg_id,
            catalog_item_id=catalog_id,
            catalog_item_id_2=catalog_id_2,
            active=True,
            auto_enabled=auto_enabled,
        )
        db.session.add(mapping)

    mapping.items = [
        RevendedoresMappingItem(catalog_item_id=cid, quantity=qty, sort_order=idx)
        for idx, (cid, qty, *_rest) in enumerate(items)
    ]
    return 'saved'


def _clean_target_defaults(defaults, catalog_item):
    """Solo se guardan valores fijos de campos que el producto de verdad pide."""
    if not isinstance(defaults, dict):
        return {}
    try:
        allowed = {str(f) for f in json.loads(catalog_item.target_fields or '[]')}
    except ValueError:
        allowed = set()
    return {str(k): str(v).strip()[:100] for k, v in defaults.items() if str(k) in allowed and str(v or '').strip()}


def _save_centralone_mapping(store_pkg_id, items, auto_enabled):
    """Igual que _save_revendedores_mapping pero para Central One. `items` =
    [(catalog_id, cantidad, valores_fijos), ...]."""
    existing = CentralOneItemMapping.query.filter_by(store_package_id=store_pkg_id).first()
    if not items:
        if existing:
            db.session.delete(existing)
            return 'removed'
        return ''

    if existing:
        mapping = existing
        mapping.auto_enabled = auto_enabled
        mapping.active = True
    else:
        mapping = CentralOneItemMapping(store_package_id=store_pkg_id, active=True, auto_enabled=auto_enabled)
        db.session.add(mapping)

    catalog = {c.id: c for c in CentralOneCatalogItem.query.filter(
        CentralOneCatalogItem.id.in_([cid for cid, *_ in items])).all()}
    rows = []
    for idx, (cid, qty, defaults) in enumerate(items):
        clean = _clean_target_defaults(defaults, catalog[cid])
        rows.append(CentralOneMappingItem(
            catalog_item_id=cid, quantity=min(qty, 50), sort_order=idx,
            target_defaults=json.dumps(clean, ensure_ascii=False) if clean else '',
        ))
    mapping.items = rows
    return 'saved'


@admin_bp.route('/providers/mapping/save', methods=['POST'])
@login_required
def provider_mapping_save():
    """Guarda cómo se entrega un paquete: proveedor principal y, si se quiere,
    otro de respaldo. Cada ruta trae sus items del catálogo de ESE proveedor."""
    data = request.get_json(silent=True) or {}
    try:
        store_pkg_id = int(data.get('store_package_id') or 0)
    except (TypeError, ValueError):
        store_pkg_id = 0
    if not store_pkg_id or not Package.query.get(store_pkg_id):
        return jsonify({'ok': False, 'error': 'Elige un paquete válido.'}), 400
    auto_enabled = bool(data.get('auto_enabled'))

    def read_route(key):
        route = data.get(key)
        if not isinstance(route, dict):
            return None
        provider = str(route.get('provider') or '').strip()
        if provider not in PROVIDER_LABELS:
            return None
        try:
            items = _parse_mapping_items(route.get('items'), max_quantity=50 if provider == 'centralone' else None)
        except (TypeError, ValueError):
            return None
        return {'provider': provider, 'items': items} if items else None

    primary = read_route('primary')
    backup = read_route('backup')
    if backup and not primary:
        return jsonify({'ok': False, 'error': 'Elige primero el paquete del proveedor principal.'}), 400
    if primary and backup and primary['provider'] == backup['provider']:
        return jsonify({'ok': False, 'error': 'El respaldo tiene que ser otro proveedor distinto al principal.'}), 400

    used = {route['provider']: route for route in (primary, backup) if route}
    for provider, model in (('revendedores', RevendedoresCatalogItem), ('centralone', CentralOneCatalogItem)):
        route = used.get(provider)
        if route:
            wanted = {cid for cid, *_ in route['items']}
            found = {row.id for row in model.query.filter(model.id.in_(wanted)).all()}
            if wanted - found:
                return jsonify({'ok': False, 'error': f'Algún paquete de {PROVIDER_LABELS[provider]} ya no existe. Sincroniza su catálogo y vuelve a elegir.'}), 400

    try:
        revendedores = used.get('revendedores')
        centralone = used.get('centralone')
        _save_revendedores_mapping(
            store_pkg_id, [(cid, qty) for cid, qty, *_ in revendedores['items']] if revendedores else [], auto_enabled)
        _save_centralone_mapping(store_pkg_id, centralone['items'] if centralone else [], auto_enabled)

        plan = PackageDeliveryPlan.query.filter_by(store_package_id=store_pkg_id).first()
        if primary:
            if not plan:
                plan = PackageDeliveryPlan(store_package_id=store_pkg_id)
                db.session.add(plan)
            plan.primary_provider = primary['provider']
            plan.backup_provider = backup['provider'] if backup else None
        elif plan:
            db.session.delete(plan)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return jsonify({'ok': False, 'error': str(exc)}), 500

    return jsonify({'ok': True, 'removed': primary is None})


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
            auto_enabled = bool(entry.get('auto_enabled'))

            # Formato nuevo: lista de items con cantidad. Si no viene (algún
            # caller viejo), se arma desde las 2 casillas fijas de antes.
            raw_items = entry.get('items')
            if raw_items is None:
                raw_items = [
                    {'catalog_id': entry.get('catalog_id'), 'quantity': 1},
                    {'catalog_id': entry.get('catalog_id_2'), 'quantity': 1},
                ]

            if not store_pkg_id:
                continue

            outcome = _save_revendedores_mapping(
                store_pkg_id, [(cid, qty) for cid, qty, *_ in _parse_mapping_items(raw_items)], auto_enabled)
            if outcome == 'saved':
                saved += 1
            elif outcome == 'removed':
                removed += 1

        db.session.commit()
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({'ok': False, 'error': str(exc)}), 500

    return jsonify({'ok': True, 'saved': saved, 'removed': removed})


@admin_bp.route('/orders/<int:order_id>/centralone-codes', methods=['POST'])
@login_required
def order_fetch_centralone_codes(order_id):
    """Trae el código de un pedido que Central One ya entregó y lo guarda en la
    orden (y avisa al cliente si es de la sección de códigos)."""
    order = Order.query.get_or_404(order_id)
    result = attach_delivered_codes(order)
    flash(result['message'], 'success' if result['ok'] else 'danger')
    return redirect(url_for('admin_bp.order_detail', order_id=order.id))


@admin_bp.route('/orders/<int:order_id>/verify-recharge', methods=['POST'])
@login_required
def order_verify_recharge(order_id):
    """Revisa/continúa la cola de recargas (Revendedores o Central One) de una orden.

    Delega toda la lógica (confirmar el intento previo, decidir si hay que
    reintentar, avanzar al siguiente paso o completar la orden) a
    process_order_delivery, que es la misma función que usa el
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
        result = process_order_delivery(order, base_state=auto_resp, force=False)
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

def _order_revenue_usd(order, current_global_rate):
    """Ingreso de la orden en USD para Estadísticas.

    Los paquetes con precio fijo en Bs (bs_price) cobran un monto en Bs que
    no tiene por qué coincidir con order.amount (que sigue el precio base
    en USD del paquete y no se toca — ver get_package_bs_fixed_price en
    routes/checkout.py). Para reflejar cuánto vale HOY en dólares lo que se
    cobró (la tasa cambia todos los días), se reconvierte con la tasa
    global actual en vez de usar order.amount. En todo lo demás (métodos en
    USD, Bs por tasa normal) order.amount ya es el valor correcto en USD."""
    package = order.package
    bs_price = getattr(package, 'bs_price', None) if package else None
    if (
        bs_price is not None
        and (order.payment_currency or '').lower() == 'bs'
        and order.payment_amount is not None
        and current_global_rate > 0
    ):
        return float(order.payment_amount) / current_global_rate
    return float(order.amount or 0)


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

    # Recargas de $0: no son ventas, son premios entregados en automático
    # (sorteos, regalos, canjes de puntos, etc.) — cada una nace con un
    # prefijo propio en payment_reference (ver deliver_prize_to_player en
    # cada punto donde se entregan). "CANJE" lo comparten códigos regalo y
    # canje directo por puntos, así que ahí se distingue por el texto de
    # las notas, que sí difiere entre los dos.
    _ZERO_AMOUNT_LABELS = {
        'RANKING': 'Premio de Ranking Mensual',
        'MINIJUEGO': 'Premio de Mini Juego (ruleta/tragaperras)',
        'PUNTOS': 'Premio canjeado con puntos (ruleta)',
        'RECARGA-ACUM': 'Premio de Recarga Acumulada',
        'SORTEO': 'Premio de Sorteo Diario',
        'ADIVINA': 'Premio de Adivina el Número',
        'HORDAS': 'Premio de Hordas',
    }

    def _zero_amount_category(order):
        reference = (order.payment_reference or '').strip().upper()
        notes = (order.notes or '').strip().lower()
        if reference.startswith('CANJE-'):
            if 'código de regalo' in notes or 'codigo de regalo' in notes:
                return 'Código regalo canjeado'
            if 'canjeado con puntos' in notes:
                return 'Canje directo por puntos'
            return 'Canje (código regalo o puntos)'
        for prefix, label in _ZERO_AMOUNT_LABELS.items():
            if reference.startswith(prefix + '-'):
                return label
        return 'Otra recarga gratuita'

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

    payment_methods_by_code = {(m.code or '').strip().lower(): m for m in PaymentMethod.query.all()}

    def _format_money_amount(amount, currency):
        if (currency or '').lower() == 'usd':
            return '$' + format(float(amount), '.2f')
        return 'Bs ' + format(int(round(float(amount))), ',').replace(',', '.')

    usd_rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
    try:
        current_global_rate = float(usd_rate_setting.value) if usd_rate_setting and usd_rate_setting.value else 0.0
    except (TypeError, ValueError):
        current_global_rate = 0.0

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
            'cost_total': 0.0,
            'sold_without_cost': 0,
        }
        for day in history_days
    }

    package_rows = {}
    daily_orders = []
    zero_amount_breakdown = defaultdict(int)
    zero_amount_total = 0
    method_totals = {}

    for order in orders:
        created_at_ve = to_ve(order.created_at)
        if created_at_ve is None:
            continue

        day_iso = created_at_ve.date().isoformat()
        if day_iso not in history_index:
            continue

        amount_value = _order_revenue_usd(order, current_global_rate)
        discount_value = float(order.discount_amount or 0)
        coupon_code = _coupon_code_for_order(order)
        has_coupon = bool(coupon_code)
        sold_order = order.status in ('approved', 'completed')
        package_cost = getattr(order.package, 'cost_usd', None) if order.package else None
        cost_value = float(package_cost) if package_cost is not None else 0.0

        day_bucket = daily_stats[day_iso]
        day_bucket['total_orders'] += 1
        day_bucket['discount_total'] += discount_value
        if sold_order:
            day_bucket['sold_orders'] += 1
            day_bucket['revenue'] += amount_value
            day_bucket['cost_total'] += cost_value
            if package_cost is None:
                day_bucket['sold_without_cost'] += 1
        elif order.status == 'pending':
            day_bucket['pending_orders'] += 1

        if has_coupon:
            day_bucket['coupon_orders'] += 1
        else:
            day_bucket['no_coupon_orders'] += 1

        if day_iso != selected_date.isoformat():
            continue

        if sold_order and amount_value == 0:
            zero_amount_total += 1
            zero_amount_breakdown[_zero_amount_category(order)] += 1

        # Lo que de verdad entró al banco/billetera por cada método, en su
        # propia moneda y SIN convertir — para que puedas cuadrarlo contra
        # el banco tal cual, en vez de contra el ingreso ya pasado a USD.
        if sold_order:
            method_code = (order.payment_method or '').strip().lower()
            method_obj = payment_methods_by_code.get(method_code)
            method_name = method_obj.name if method_obj else (order.payment_method or 'Desconocido').strip().upper()
            raw_currency = (
                (order.payment_currency or '').strip().lower()
                or (method_obj.account_currency if method_obj else '')
                or 'bs'
            )
            raw_amount = float(order.payment_amount) if order.payment_amount is not None else float(order.amount or 0)

            mt_key = (method_code or 'desconocido', raw_currency)
            mt_row = method_totals.get(mt_key)
            if mt_row is None:
                mt_row = {'method_name': method_name, 'currency': raw_currency, 'total': 0.0, 'count': 0}
                method_totals[mt_key] = mt_row
            mt_row['total'] += raw_amount
            mt_row['count'] += 1

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
                'cost_total': 0.0,
                'sold_without_cost': 0,
                'coupon_breakdown': defaultdict(int),
                'no_coupon_orders': 0,
            }
            package_rows[pkg_key] = row

        row['total_orders'] += 1
        row['discount_total'] += discount_value
        if sold_order:
            row['sold_orders'] += 1
            row['revenue'] += amount_value
            row['cost_total'] += cost_value
            if package_cost is None:
                row['sold_without_cost'] += 1
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
            'cost_total': row['cost_total'],
            'profit': row['revenue'] - row['cost_total'],
            'sold_without_cost': row['sold_without_cost'],
            'coupon_breakdown': coupons,
            'no_coupon_orders': row['no_coupon_orders'],
        })

    package_stats.sort(key=lambda row: (
        row['game_position'],
        _sort_text(row['game_name']),
        row['package_sort_order'],
        _sort_text(row['package_name']),
    ))

    package_stats_totals = {
        'total_orders': sum(row['total_orders'] for row in package_stats),
        'sold_orders': sum(row['sold_orders'] for row in package_stats),
        'pending_orders': sum(row['pending_orders'] for row in package_stats),
        'revenue': sum(row['revenue'] for row in package_stats),
        'cost_total': sum(row['cost_total'] for row in package_stats),
        'discount_total': sum(row['discount_total'] for row in package_stats),
        'profit': sum(row['profit'] for row in package_stats),
    }

    payment_method_totals = sorted(
        method_totals.values(), key=lambda row: (-row['total'], row['method_name'])
    )
    for row in payment_method_totals:
        row['total_label'] = _format_money_amount(row['total'], row['currency'])

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

    for bucket in daily_stats.values():
        bucket['profit'] = bucket['revenue'] - bucket['cost_total']

    selected_summary = daily_stats[selected_date.isoformat()]
    daily_history = [daily_stats[day.isoformat()] for day in reversed(history_days)]

    zero_amount_summary = [
        {'label': label, 'count': count}
        for label, count in sorted(zero_amount_breakdown.items(), key=lambda item: (-item[1], item[0]))
    ]

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
        package_stats_totals=package_stats_totals,
        payment_method_totals=payment_method_totals,
        coupon_summary=coupon_summary,
        no_coupon_total=no_coupon_total,
        daily_orders=daily_orders,
        zero_amount_total=zero_amount_total,
        zero_amount_summary=zero_amount_summary,
    )


# ─── Cuentas registradas ─────────────────────────────────────────────────────
# Las personas que crearon una cuenta (con correo y contraseña, o con Google). Desde aquí se les corrigen los
# datos, se les restablece la contraseña si la olvidan, se les desactiva la cuenta y se ven/quitan los IDs
# que enlazaron. Los clientes que solo compran con su ID NO salen aquí (no tienen cuenta).

ACCOUNTS_PAGE_SIZE = 25
TEMP_PASSWORD_ALPHABET = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # sin letras que se confunden (l, I, O, 0, 1)


def _generate_temp_password(length=10):
    import secrets
    return ''.join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(length))


def _like_pattern(text):
    escaped = text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


def _account_or_404(user_id):
    user = db.session.get(User, user_id)
    if user is None or (user.account_kind or '') not in ACCOUNT_KINDS:
        abort(404)
    return user


@admin_bp.route('/cuentas')
@login_required
def accounts():
    search = (request.args.get('q') or '').strip()[:100]
    kind_filter = request.args.get('tipo') if request.args.get('tipo') in ACCOUNT_KINDS else ''
    state_filter = request.args.get('estado') if request.args.get('estado') in {'activas', 'desactivadas'} else ''

    base = User.query.filter(User.account_kind.in_(ACCOUNT_KINDS))
    totals = {
        'all': base.count(),
        'registered': base.filter(User.account_kind == 'registered').count(),
        'google': base.filter(User.account_kind == 'google_email').count(),
        'inactive': base.filter(User.is_active.is_(False)).count(),
    }

    query = base
    if kind_filter:
        query = query.filter(User.account_kind == kind_filter)
    if state_filter == 'activas':
        query = query.filter(User.is_active.is_(True))
    elif state_filter == 'desactivadas':
        query = query.filter(User.is_active.is_(False))
    if search:
        like = _like_pattern(search)
        conditions = [
            User.username.ilike(like, escape='\\'),
            User.email.ilike(like, escape='\\'),
            User.contact_email.ilike(like, escape='\\'),
            User.account_identifier.ilike(like, escape='\\'),
        ]
        if search.isdigit():
            conditions.append(User.id == int(search))
        query = query.filter(or_(*conditions))

    total = query.count()
    total_pages = max(1, (total + ACCOUNTS_PAGE_SIZE - 1) // ACCOUNTS_PAGE_SIZE)
    page = min(max(request.args.get('page', type=int) or 1, 1), total_pages)
    rows = (
        query.order_by(User.created_at.desc(), User.id.desc())
        .offset((page - 1) * ACCOUNTS_PAGE_SIZE)
        .limit(ACCOUNTS_PAGE_SIZE)
        .all()
    )
    return render_template(
        'admin/accounts.html',
        rows=rows,
        totals=totals,
        search_query=search,
        kind_filter=kind_filter,
        state_filter=state_filter,
        current_page=page,
        total_pages=total_pages,
        page_numbers=list(range(max(1, page - 2), min(total_pages, page + 2) + 1)),
        total=total,
        start_index=((page - 1) * ACCOUNTS_PAGE_SIZE) + 1 if total else 0,
        end_index=min(page * ACCOUNTS_PAGE_SIZE, total) if total else 0,
    )


def _render_account_detail(user, temp_password=None, status=200):
    return render_template(
        'admin/account_detail.html',
        account=user,
        orders=linked_orders(user, limit=15),
        temp_password=temp_password,
        profile=get_profile(user),
        awards=ProfileAward.query.filter_by(user_id=user.id).order_by(ProfileAward.awarded_at.desc(), ProfileAward.id.desc()).all(),
        points_balance=account_points.balance(user.id),
        points_log=account_points.recent_log(user.id, 10),
        points_max=account_points.MAX_POINTS_PER_MOVE,
    ), status


@admin_bp.route('/cuentas/<int:user_id>')
@login_required
def account_detail(user_id):
    return _render_account_detail(_account_or_404(user_id))


@admin_bp.route('/cuentas/<int:user_id>/datos', methods=['POST'])
@login_required
def account_update(user_id):
    user = _account_or_404(user_id)
    errors = []

    username_raw = (request.form.get('username') or '').strip()
    if username_raw != (user.username or '') or not user.has_chosen_username:
        if username_raw:
            username, error = validate_username(username_raw, exclude_user_id=user.id)
            if error:
                errors.append(error)
            else:
                user.username = username
        elif user.is_registered_account:
            errors.append('El nombre de usuario no puede quedar vacío.')

    if user.is_registered_account:
        email_raw = (request.form.get('email') or '').strip().lower()
        if email_raw != (user.email or ''):
            email, error = validate_email_address(email_raw, exclude_user_id=user.id)
            if error:
                errors.append(error)
            else:
                user.email = email
                user.contact_email = email
                user.account_identifier = email
                user.account_identifier_normalized = email

    phone = (request.form.get('phone') or '').strip()
    if len(phone) > 20:
        errors.append('El teléfono es demasiado largo (máximo 20 caracteres).')
    else:
        user.phone = phone or None

    if errors:
        db.session.rollback()
        for message in errors:
            flash(message, 'danger')
        return redirect(url_for('admin_bp.account_detail', user_id=user.id))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('No se pudo guardar: ese usuario o correo ya está en uso.', 'danger')
        return redirect(url_for('admin_bp.account_detail', user_id=user.id))
    flash('Datos de la cuenta actualizados.', 'success')
    return redirect(url_for('admin_bp.account_detail', user_id=user.id))


@admin_bp.route('/cuentas/<int:user_id>/clave', methods=['POST'])
@login_required
def account_password(user_id):
    """Restablece la contraseña de quien la olvidó: el admin escribe una nueva o genera una temporal."""
    user = _account_or_404(user_id)
    if not user.is_registered_account:
        flash('Esa cuenta entra con Google y no tiene contraseña.', 'warning')
        return redirect(url_for('admin_bp.account_detail', user_id=user.id))

    if request.form.get('mode') == 'generate':
        new_password = _generate_temp_password()
    else:
        new_password = (request.form.get('new_password') or '').strip()
        error = validate_new_password(new_password, None, username=user.username, email=user.email)
        if error:
            flash(error, 'danger')
            return redirect(url_for('admin_bp.account_detail', user_id=user.id))

    user.set_password(new_password)
    db.session.commit()
    # Se muestra UNA vez, directamente en la respuesta (no se guarda en la sesión ni en ningún mensaje guardado).
    return _render_account_detail(user, temp_password=new_password)


def _refresh_ranks_after_moderation(user=None):
    """Tras ocultar, desactivar, restablecer o mover un ID, los puestos del Top se rehacen al momento (antes esperaban hasta 15 minutos)."""
    try:
        if user is not None:
            profile_social.refresh_user(user)
        profile_social.recompute_ranks()
    except Exception:
        db.session.rollback()


@admin_bp.route('/cuentas/<int:user_id>/estado', methods=['POST'])
@login_required
def account_toggle(user_id):
    user = _account_or_404(user_id)
    user.is_active = not bool(user.is_active)
    db.session.commit()
    _refresh_ranks_after_moderation()
    flash('Cuenta activada.' if user.is_active else 'Cuenta desactivada: ya no puede iniciar sesión.', 'success' if user.is_active else 'warning')
    return redirect(url_for('admin_bp.account_detail', user_id=user.id))


@admin_bp.route('/cuentas/<int:user_id>/perfil/ocultar', methods=['POST'])
@login_required
def account_profile_toggle(user_id):
    """Moderación: oculta (o vuelve a mostrar) el perfil público de la cuenta, aunque su dueño lo tenga público."""
    user = _account_or_404(user_id)
    profile = get_or_create_profile(user)
    profile.moderation_hidden = not bool(profile.moderation_hidden)
    db.session.commit()
    _refresh_ranks_after_moderation()
    flash('Perfil público ocultado: nadie más lo ve.' if profile.moderation_hidden else 'Perfil público visible otra vez.', 'warning' if profile.moderation_hidden else 'success')
    return redirect(url_for('admin_bp.account_detail', user_id=user.id))


@admin_bp.route('/cuentas/<int:user_id>/perfil/restablecer', methods=['POST'])
@login_required
def account_profile_reset(user_id):
    """Moderación: borra fotos, banner, fondo, marcos, textos y colores del perfil (queda como recién creado)."""
    user = _account_or_404(user_id)
    profile = get_profile(user)
    if profile is not None:
        reset_profile(profile, keep_privacy=True)   # lo que el dueño apagó (perfil público, números, rankings) se queda apagado
        db.session.commit()
        _refresh_ranks_after_moderation()
    flash('Perfil público restablecido: se borraron sus imágenes y textos (sus opciones de privacidad no se tocaron).', 'success')
    return redirect(url_for('admin_bp.account_detail', user_id=user.id))


@admin_bp.route('/cuentas/<int:user_id>/puntos', methods=['POST'])
@login_required
def account_points_adjust(user_id):
    """Puntos de la tienda del perfil: el administrador se los pone (una dinámica de perfil) o se los quita. Los perfiles no ganan puntos recargando."""
    user = _account_or_404(user_id)
    raw_amount = (request.form.get('amount') or '').strip()
    note = clean_text(request.form.get('note'), 150)
    action = (request.form.get('action') or '').strip()
    if not raw_amount.isdigit() or not 1 <= int(raw_amount) <= account_points.MAX_POINTS_PER_MOVE:
        flash('Escribe cuántos puntos (un número entero mayor que 0).', 'danger')
        return redirect(url_for('admin_bp.account_detail', user_id=user.id) + '#accountPointsCard')
    amount = int(raw_amount)
    if action == 'remove':
        if not account_points.debit(user.id, amount, kind='admin', note=note or 'Puntos quitados por el administrador'):
            db.session.rollback()
            flash(f'El perfil solo tiene {account_points.balance(user.id)} puntos: no se le pueden quitar {amount}.', 'danger')
            return redirect(url_for('admin_bp.account_detail', user_id=user.id) + '#accountPointsCard')
        db.session.commit()
        flash(f'Se quitaron {amount} puntos del perfil. Ahora tiene {account_points.balance(user.id)}.', 'success')
    else:
        account_points.credit(user.id, amount, kind='admin', note=note or 'Puntos del administrador')
        db.session.commit()
        flash(f'Se le dieron {amount} puntos al perfil. Ahora tiene {account_points.balance(user.id)}. Los usa desde "Canjear puntos", pasándolos a un ID.', 'success')
    return redirect(url_for('admin_bp.account_detail', user_id=user.id) + '#accountPointsCard')


@admin_bp.route('/cuentas/<int:user_id>/premios', methods=['POST'])
@login_required
def account_award_add(user_id):
    """Vitrina de premios del perfil: el admin carga a mano un premio (sorteo, evento, etc.) con imagen opcional."""
    user = _account_or_404(user_id)
    title = clean_text(request.form.get('title'), 80)
    description = clean_text(request.form.get('description'), 200)
    if not title:
        flash('Escribe el título del premio.', 'danger')
        return redirect(url_for('admin_bp.account_detail', user_id=user.id))
    image_path = ''
    upload = request.files.get('image')
    if upload is not None and (upload.filename or '').strip():
        image_path, error = save_media(upload, 'award', user.id)
        if error:
            flash(error, 'danger')
            return redirect(url_for('admin_bp.account_detail', user_id=user.id))
    db.session.add(ProfileAward(
        user_id=user.id, title=title, description=description, image_path=image_path or '',
        is_title=request.form.get('is_title') in ('1', 'on', 'true'), kind='manual',
    ))
    db.session.commit()
    flash('Premio agregado a la vitrina.', 'success')
    return redirect(url_for('admin_bp.account_detail', user_id=user.id))


@admin_bp.route('/cuentas/<int:user_id>/premios/<int:award_id>/borrar', methods=['POST'])
@login_required
def account_award_delete(user_id, award_id):
    user = _account_or_404(user_id)
    award = ProfileAward.query.filter_by(id=award_id, user_id=user.id).first()
    if award is None:
        flash('Ese premio ya no estaba.', 'warning')
    else:
        delete_media(award.image_path)
        db.session.delete(award)
        db.session.commit()
        flash('Premio quitado de la vitrina.', 'success')
    return redirect(url_for('admin_bp.account_detail', user_id=user.id))


@admin_bp.route('/cuentas/perfiles/ajustes', methods=['GET', 'POST'])
@login_required
def account_social_settings():
    """Niveles del mes (diamantes para cada uno), umbral de "Ballena" y tope de me gusta por día."""
    if request.method == 'POST':
        error = profile_social.save_thresholds({key: request.form.get(f'level_{key}', '') for key in profile_social.LEVEL_KEYS})
        whale = (request.form.get('whale') or '').strip()
        likes = (request.form.get('like_limit') or '').strip()
        top_points = (request.form.get('top_points') or '').strip()
        if not error:
            if not whale.isdigit() or not (1 <= int(whale) <= 10_000_000):
                error = 'El umbral de "Ballena" debe ser un número entre 1 y 10.000.000.'
            elif not likes.isdigit() or not (1 <= int(likes) <= 1000):
                error = 'El tope de me gusta por día debe estar entre 1 y 1000.'
            elif top_points and (not top_points.isdigit() or not (1 <= int(top_points) <= 100000)):
                error = 'Los puntos por dólar deben ser un número entero entre 1 y 100.000.'
        if error:
            db.session.rollback()
            flash(error, 'danger')
        else:
            profile_social._set_setting(profile_social.SETTING_WHALE, int(whale), 'Puntos de compras en un mes para la insignia Ballena')
            if top_points:   # si el formulario no lo trae, se queda como estaba
                profile_social._set_setting(profile_social.SETTING_TOP_POINTS, int(top_points), 'Puntos de "Más compras" por cada dólar comprado')
            profile_social._set_setting(profile_social.SETTING_LIKE_LIMIT, int(likes), 'Me gusta que una cuenta puede dar por día')
            db.session.commit()
            flash('Ajustes guardados. Los niveles se recalculan en unos minutos o con "Recalcular ahora".', 'success')
        return redirect(url_for('admin_bp.account_social_settings'))
    return _social_settings_page()


def _social_settings_page(xp_override=None):
    """La página de ajustes de perfiles. `xp_override` = lo que el administrador acaba de escribir en "Niveles por números" y no se pudo
    guardar (así no pierde lo escrito)."""
    thresholds = profile_social.get_thresholds()
    counts = dict(db.session.query(UserBadge.badge_key, func.count(UserBadge.id)).group_by(UserBadge.badge_key).all())
    total_accounts = User.query.filter(User.account_kind.in_(ACCOUNT_KINDS), User.is_active.is_(True)).count()
    badges = [
        {'key': key, **profile_social.BADGES[key], 'count': counts.get(key, 0), 'percent': round(counts.get(key, 0) * 100 / total_accounts, 1) if total_accounts else 0}
        for key in profile_social.BADGE_ORDER
    ]
    xp_cfg = profile_xp.get_config()
    return render_template(
        'admin/profile_settings.html', levels=profile_social.LEVELS, thresholds=thresholds,
        whale=profile_social.get_int_setting(profile_social.SETTING_WHALE, profile_social.DEFAULT_WHALE),
        like_limit=profile_social.get_int_setting(profile_social.SETTING_LIKE_LIMIT, profile_social.DEFAULT_LIKE_LIMIT),
        top_points=profile_social.top_points_per_dollar(),
        badges=badges, total_accounts=total_accounts,
        refreshed_at=profile_social._get_setting(profile_social.SETTING_REFRESHED, ''),
        xp_cfg=xp_cfg, xp_points=profile_xp.POINT_DEFS, xp_missions=profile_xp.MISSION_DEFS, xp_form=xp_override or {},
        xp_preview=profile_xp.preview_levels(xp_cfg), xp_tiers=profile_xp.TIERS, xp_max=profile_xp.POINT_MAX,
        xp_label=profile_xp.mission_text,
    )


@admin_bp.route('/cuentas/perfiles/ajustes/niveles', methods=['POST'])
@login_required
def account_xp_settings():
    """Niveles por números (como Steam): puntos por cada acción, topes por día, curva de niveles y misiones."""
    error = profile_xp.save_config(request.form)
    if error:
        db.session.rollback()
        flash(error, 'danger')
        return _social_settings_page(xp_override=request.form)
    try:
        profile_social.refresh_all()   # que el cambio se vea ya en los niveles (si ya hay un recálculo en curso, se aplica en unos minutos)
        flash('Niveles por números guardados y recalculados.', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Guardado, pero no se pudo recalcular ahora ({exc}). Usa "Recalcular ahora".', 'warning')
    return redirect(url_for('admin_bp.account_social_settings'))


@admin_bp.route('/cuentas/marcos', methods=['GET', 'POST'])
@login_required
def profile_frames():
    """Tienda de marcos del perfil: se sube la imagen (PNG, WEBP o GIF, también con movimiento), se le pone nombre y tipo (foto o banner).
    Todos son GRATIS: las cuentas los eligen desde su editor de perfil (los puntos son de cada ID y las cuentas no dependen de ningún ID)."""
    from ..models import ProfileFrame, UserFrame
    from ..utils import profile_frames as PF

    if request.method == 'POST':
        name = PF.clean_frame_name(request.form.get('name'))
        kind = request.form.get('kind') if request.form.get('kind') in PF.FRAME_KINDS else 'avatar'
        upload = request.files.get('image')
        if not name:
            flash('Ponle un nombre al marco.', 'danger')
        elif upload is None or not (upload.filename or '').strip():
            flash('Sube la imagen del marco (PNG, WEBP o GIF).', 'danger')
        else:
            rel, error = PF.save_frame_asset(upload, kind)
            if error:
                flash(error, 'danger')
            else:
                db.session.add(ProfileFrame(name=name, kind=kind, image_path=rel, price_points=0, is_active=True,
                                            sort_order=(db.session.query(func.coalesce(func.max(ProfileFrame.sort_order), 0)).scalar() or 0) + 1))
                db.session.commit()
                flash(f'Marco "{name}" agregado (gratis para todas las cuentas).', 'success')
        return redirect(url_for('admin_bp.profile_frames'))

    frames = ProfileFrame.query.order_by(ProfileFrame.kind.asc(), ProfileFrame.sort_order.asc(), ProfileFrame.id.asc()).all()
    owners = dict(db.session.query(UserFrame.frame_id, func.count(UserFrame.id)).group_by(UserFrame.frame_id).all())
    return render_template('admin/profile_frames.html', frames=frames, owners=owners, frame_url=PF.frame_url)


@admin_bp.route('/cuentas/marcos/<int:frame_id>/editar', methods=['POST'])
@login_required
def profile_frame_edit(frame_id):
    from ..models import ProfileFrame
    from ..utils import profile_frames as PF

    frame = ProfileFrame.query.get_or_404(frame_id)
    name = PF.clean_frame_name(request.form.get('name'))
    if not name:
        flash('El nombre no puede quedar vacío.', 'danger')
        return redirect(url_for('admin_bp.profile_frames'))
    frame.name = name
    frame.price_points = 0   # todos los marcos son gratis (los puntos son de cada ID)
    frame.is_active = request.form.get('is_active') == '1'
    try:
        frame.sort_order = max(0, min(100000, int(request.form.get('sort_order') or 0)))
    except ValueError:
        pass
    upload = request.files.get('image')
    if upload is not None and (upload.filename or '').strip():
        rel, error = PF.save_frame_asset(upload, frame.kind)
        if error:
            db.session.rollback()
            flash(error, 'danger')
            return redirect(url_for('admin_bp.profile_frames'))
        PF.delete_frame_asset(frame.image_path)
        frame.image_path = rel
    db.session.commit()
    flash(f'Marco "{frame.name}" guardado.', 'success')
    return redirect(url_for('admin_bp.profile_frames'))


@admin_bp.route('/cuentas/marcos/<int:frame_id>/borrar', methods=['POST'])
@login_required
def profile_frame_delete(frame_id):
    """Borra un marco que nadie compró. Si ya tiene dueños no se borra (se les quitaría lo que pagaron): se desactiva y deja de venderse."""
    from ..models import ProfileFrame, UserFrame, UserProfile
    from ..utils import profile_frames as PF

    frame = ProfileFrame.query.get_or_404(frame_id)
    if UserFrame.query.filter_by(frame_id=frame.id).count():
        frame.is_active = False
        db.session.commit()
        flash(f'El marco "{frame.name}" ya lo compraron: no se borra, se desactivó (deja de venderse y de verse).', 'warning')
        return redirect(url_for('admin_bp.profile_frames'))
    UserProfile.query.filter(UserProfile.avatar_frame_id == frame.id).update({'avatar_frame_id': None}, synchronize_session=False)
    UserProfile.query.filter(UserProfile.banner_frame_id == frame.id).update({'banner_frame_id': None}, synchronize_session=False)
    PF.delete_frame_asset(frame.image_path)
    name = frame.name
    db.session.delete(frame)
    db.session.commit()
    flash(f'Marco "{name}" borrado.', 'success')
    return redirect(url_for('admin_bp.profile_frames'))


@admin_bp.route('/cuentas/perfiles/recalcular', methods=['POST'])
@login_required
def account_social_refresh():
    try:
        ok = profile_social.refresh_all()
        flash('Perfiles recalculados.' if ok else 'Ya se estaba recalculando; espera un momento.', 'success' if ok else 'info')
    except Exception as exc:
        db.session.rollback()
        flash(f'No se pudo recalcular: {exc}', 'danger')
    return redirect(url_for('admin_bp.account_social_settings'))
