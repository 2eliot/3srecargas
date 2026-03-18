import os
import json
import requests
from datetime import datetime, timedelta
from functools import wraps
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, current_app, jsonify
)
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import or_
from werkzeug.utils import secure_filename
from ..models import (
    db, AdminUser, Game, Package, Category, Order,
    Pin, Affiliate, AffiliateCommission, PaymentMethod, Setting,
    RevendedoresCatalogItem, RevendedoresItemMapping,
)
from ..utils.notifications import (
    notify_order_approved, notify_order_completed, notify_order_rejected,
)

admin_bp = Blueprint('admin_bp', __name__)

HOUSEKEEPING_ORDER_RETENTION_DAYS = 60  # ~2 months
HOUSEKEEPING_PIN_RETENTION_DAYS = 30
HOUSEKEEPING_INTERVAL_HOURS = 6
_last_housekeeping_run = None

ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


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


def save_image(file, subfolder=''):
    if not file or not allowed_file(file.filename):
        return None
    filename = secure_filename(file.filename)
    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S%f')
    filename = f"{ts}_{filename}"
    folder = current_app.config['UPLOAD_FOLDER']
    if subfolder:
        folder = os.path.join(folder, subfolder)
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, filename))
    return (subfolder + '/' + filename) if subfolder else filename


def cleanup_old_orders():
    threshold = datetime.utcnow() - timedelta(days=HOUSEKEEPING_ORDER_RETENTION_DAYS)
    old_orders = Order.query.filter(Order.created_at < threshold).all()
    removed = 0
    for order in old_orders:
        if order.payment_capture:
            delete_uploaded_file(order.payment_capture)
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


@admin_bp.before_app_request
def admin_housekeeping_hook():
    if not request.path.startswith('/admin'):
        return
    run_housekeeping_if_needed()


def process_affiliate_commission(order):
    if not order.affiliate_id:
        return
    affiliate = Affiliate.query.get(order.affiliate_id)
    if not affiliate or not affiliate.is_active:
        return
    commission_amount = round(float(order.amount) * float(affiliate.commission_rate) / 100, 2)
    commission = AffiliateCommission(
        affiliate_id=affiliate.id,
        order_id=order.id,
        amount=commission_amount,
    )
    affiliate.balance = float(affiliate.balance) + commission_amount
    affiliate.total_earned = float(affiliate.total_earned) + commission_amount
    db.session.add(commission)


# ─── Auth ────────────────────────────────────────────────────────────────────

@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_bp.dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        user = AdminUser.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('admin_bp.dashboard'))
        flash('Usuario o contraseña incorrectos.', 'danger')
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

    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(10).all()

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
    )


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
    zone_id_label = request.form.get('zone_id_label', 'Zone ID').strip()
    is_automated = bool(request.form.get('is_automated'))
    position = int(request.form.get('position', 100))
    description = request.form.get('description', '').strip()

    if not name or not category_id:
        flash('Nombre y categoría son obligatorios.', 'danger')
        return redirect(url_for('admin_bp.games'))

    slug = name.lower().replace(' ', '-').replace('/', '-')
    existing = Game.query.filter_by(slug=slug).first()
    if existing:
        slug = f"{slug}-{Game.query.count()}"

    image = save_image(request.files.get('image'), 'games')
    game = Game(
        name=name, slug=slug, category_id=int(category_id),
        requires_zone_id=requires_zone_id, player_id_label=player_id_label,
        zone_id_label=zone_id_label, is_automated=is_automated,
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
    game.zone_id_label = request.form.get('zone_id_label', game.zone_id_label).strip()
    game.is_automated = bool(request.form.get('is_automated'))
    game.position = int(request.form.get('position', game.position))
    game.description = request.form.get('description', game.description or '').strip()
    game.is_active = bool(request.form.get('is_active'))

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


@admin_bp.route('/packages/add', methods=['POST'])
@login_required
def package_add():
    game_id = request.form.get('game_id')
    name = request.form.get('name', '').strip()
    price = request.form.get('price', '0').strip()
    description = request.form.get('description', '').strip()
    is_automated = bool(request.form.get('is_automated'))
    sort_order = int(request.form.get('sort_order', 100))

    if not game_id or not name or not price:
        flash('Juego, nombre y precio son obligatorios.', 'danger')
        return redirect(url_for('admin_bp.packages'))

    image = save_image(request.files.get('image'), 'packages')
    pkg = Package(
        game_id=int(game_id), name=name, price=float(price),
        description=description, is_automated=is_automated,
        sort_order=sort_order, image=image,
    )
    db.session.add(pkg)
    db.session.commit()
    flash(f'Paquete "{name}" creado.', 'success')
    return redirect(url_for('admin_bp.packages'))


@admin_bp.route('/packages/<int:pkg_id>/edit', methods=['POST'])
@login_required
def package_edit(pkg_id):
    pkg = Package.query.get_or_404(pkg_id)
    pkg.name = request.form.get('name', pkg.name).strip()
    pkg.price = float(request.form.get('price', pkg.price))
    pkg.description = request.form.get('description', pkg.description or '').strip()
    pkg.is_automated = bool(request.form.get('is_automated'))
    pkg.sort_order = int(request.form.get('sort_order', pkg.sort_order))
    pkg.is_active = bool(request.form.get('is_active'))

    new_image = save_image(request.files.get('image'), 'packages')
    if new_image:
        delete_uploaded_file(pkg.image)
        pkg.image = new_image

    db.session.commit()
    flash('Paquete actualizado.', 'success')
    return redirect(url_for('admin_bp.packages'))


@admin_bp.route('/packages/<int:pkg_id>/delete', methods=['POST'])
@login_required
def package_delete(pkg_id):
    pkg = Package.query.get_or_404(pkg_id)
    pkg.is_active = False
    db.session.commit()
    flash('Paquete desactivado.', 'warning')
    return redirect(url_for('admin_bp.packages'))


# ─── Orders ──────────────────────────────────────────────────────────────────

@admin_bp.route('/orders')
@login_required
def orders():
    status_filter = request.args.get('status', '')
    query = Order.query.order_by(Order.created_at.desc())
    if status_filter:
        query = query.filter_by(status=status_filter)
    all_orders = query.all()
    return render_template('admin/orders.html', orders=all_orders, status_filter=status_filter)


@admin_bp.route('/orders/<int:order_id>')
@login_required
def order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template('admin/order_detail.html', order=order)


@admin_bp.route('/orders/<int:order_id>/approve', methods=['POST'])
@login_required
def order_approve(order_id):
    order = Order.query.get_or_404(order_id)
    if order.status != 'pending':
        flash('Solo se pueden aprobar órdenes pendientes.', 'warning')
        return redirect(url_for('admin_bp.orders'))

    # ── Revendedores API auto-recharge ──
    rev_mapping = _get_order_auto_mapping(order)
    if rev_mapping:
        try:
            base_url, api_key, _, recharge_path = _revendedores_env()
            catalog_item = rev_mapping.catalog_item
            if base_url and api_key and catalog_item:
                rev_payload = {
                    'product_id': catalog_item.remote_product_id,
                    'package_id': catalog_item.remote_package_id,
                    'player_id': str(order.player_id or '').strip(),
                    'external_order_id': order.order_number,
                }
                if order.zone_id:
                    rev_payload['player_id2'] = str(order.zone_id).strip()

                resp = requests.post(
                    f'{base_url}{recharge_path}',
                    json=rev_payload,
                    headers={'X-API-Key': api_key, 'Content-Type': 'application/json'},
                    timeout=120,
                )
                rev_data = resp.json() if resp.ok else {}
                rev_ok = rev_data.get('ok', False)

                if rev_ok:
                    player_name = rev_data.get('player_name', '')
                    ref_no = rev_data.get('reference_no', '')
                    order.status = 'completed'
                    order.automation_response = json.dumps({
                        'source': 'revendedores_api',
                        'success': True,
                        'player_name': player_name,
                        'reference_no': ref_no,
                        'order_id': rev_data.get('order_id'),
                    })
                    order.notes = (order.notes or '') + f'\n[Revendedores API] Ref: {ref_no}, Player: {player_name}'
                    order.updated_at = datetime.utcnow()
                    process_affiliate_commission(order)
                    db.session.commit()
                    try:
                        notify_order_completed(order, order.package, order.game)
                    except Exception:
                        pass
                    extra = f' (Jugador: {player_name})' if player_name else ''
                    flash(f'Orden #{order.order_number} completada vía Revendedores API.{extra}', 'success')
                    return redirect(url_for('admin_bp.orders'))
                else:
                    rev_error = rev_data.get('error', resp.text[:200] if not resp.ok else 'Error desconocido')
                    order.automation_response = json.dumps({
                        'source': 'revendedores_api',
                        'pending_verification': True,
                        'external_order_id': order.order_number,
                        'error': rev_error,
                    })
                    db.session.commit()
                    flash(
                        f'Revendedores reportó error: {rev_error}. Verificando si la recarga se procesó...',
                        'warning',
                    )
                    return redirect(url_for('admin_bp.orders'))
        except Exception as e:
            order.automation_response = json.dumps({
                'source': 'revendedores_api',
                'pending_verification': True,
                'external_order_id': order.order_number,
                'error': str(e),
            })
            try:
                db.session.commit()
            except Exception:
                pass
            flash(f'Error contactando Revendedores API: {e}. Verificando si se procesó...', 'warning')
            return redirect(url_for('admin_bp.orders'))

    package = order.package
    category_slug = (order.game.category.slug if order.game and order.game.category else '').lower()
    needs_pin_delivery = package.is_automated or category_slug == 'tarjetas'

    pin = None
    if needs_pin_delivery:
        pin = (
            Pin.query
            .filter_by(package_id=package.id, is_used=False)
            .order_by(Pin.created_at.asc())
            .first()
        )
        if not pin:
            flash('Sin stock de códigos para este paquete. Carga PINs primero.', 'danger')
            return redirect(url_for('admin_bp.orders'))

    if package.is_automated:
        vps_url = current_app.config.get('VPS_REDEEM_URL')
        vps_timeout = current_app.config.get('VPS_TIMEOUT', 120)

        payload = {
            'pin_key': str(pin.code).strip(),
            'player_id': str(order.player_id).strip(),
            'full_name': current_app.config.get('VPS_FULL_NAME', 'Usuario Recarga'),
            'birth_date': current_app.config.get('VPS_BIRTH_DATE', '01/01/1995'),
            'country': current_app.config.get('VPS_COUNTRY', 'Venezuela'),
            'request_id': order.order_number,
        }

        try:
            resp = requests.post(
                vps_url,
                json=payload,
                timeout=vps_timeout,
                headers={'Content-Type': 'application/json'},
            )

            try:
                data = resp.json()
            except Exception:
                data = {}

            exito = data.get('success') or data.get('exito') or data.get('status') == 'ok'
            mensaje = data.get('message') or data.get('mensaje') or data.get('error') or ''
            player_name = data.get('player_name') or data.get('nombre_jugador') or ''

            if not exito and resp.status_code != 200:
                exito = False
            elif resp.status_code == 200 and not data:
                exito = True
                mensaje = 'Recarga procesada (VPS)'

            if exito:
                pin.is_used = True
                pin.used_at = datetime.utcnow()
                pin.order_id = order.id
                order.status = 'completed'
                order.pin_id = pin.id
                order.pin_delivered = pin.code
                order.automation_response = json.dumps({
                    'success': True,
                    'message': mensaje,
                    'player_name': player_name,
                })
                order.updated_at = datetime.utcnow()
                process_affiliate_commission(order)
                db.session.commit()
                try:
                    notify_order_completed(order, order.package, order.game)
                except Exception:
                    pass
                extra = f' (Jugador: {player_name})' if player_name else ''
                flash(f'Orden #{order.order_number} completada vía automatización.{extra}', 'success')
            else:
                flash(
                    f'Redención fallida: {mensaje or "Error desconocido del VPS"}. '
                    f'El PIN se mantiene en stock. La orden sigue pendiente.',
                    'danger',
                )
        except requests.exceptions.Timeout:
            flash(
                f'El VPS no respondió en {vps_timeout}s. Reintenta más tarde. '
                f'El PIN no fue consumido.',
                'danger',
            )
        except requests.exceptions.ConnectionError:
            flash(
                'No se pudo conectar al bot de recarga. '
                'Verifica que el servicio esté activo en el VPS.',
                'danger',
            )
        except Exception as e:
            flash(f'Error inesperado al contactar el VPS: {e}', 'danger')
    elif needs_pin_delivery:
        pin.is_used = True
        pin.used_at = datetime.utcnow()
        pin.order_id = order.id
        order.status = 'completed'
        order.pin_id = pin.id
        order.pin_delivered = pin.code
        order.updated_at = datetime.utcnow()
        process_affiliate_commission(order)
        db.session.commit()
        try:
            notify_order_completed(order, order.package, order.game, pin_code=pin.code)
        except Exception:
            pass
        flash(f'Orden #{order.order_number} completada y PIN entregado.', 'success')
    else:
        order.status = 'approved'
        order.updated_at = datetime.utcnow()
        process_affiliate_commission(order)
        db.session.commit()
        try:
            notify_order_approved(order, order.package, order.game)
        except Exception:
            pass
        flash(f'Orden #{order.order_number} aprobada.', 'success')

    return redirect(url_for('admin_bp.orders'))


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

@admin_bp.route('/pins')
@login_required
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

    return render_template(
        'admin/pins.html',
        automated_packages=pin_enabled_packages,
        selected_package=selected_package,
        pins_list=pins_list,
    )


@admin_bp.route('/pins/<int:package_id>/upload', methods=['POST'])
@login_required
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


# ─── Affiliates ──────────────────────────────────────────────────────────────

@admin_bp.route('/affiliates')
@login_required
def affiliates():
    all_affiliates = Affiliate.query.order_by(Affiliate.created_at.desc()).all()
    return render_template('admin/affiliates.html', affiliates=all_affiliates)


@admin_bp.route('/affiliates/add', methods=['POST'])
@login_required
def affiliate_add():
    name = request.form.get('name', '').strip()
    code = request.form.get('code', '').strip().upper()
    email = request.form.get('email', '').strip()
    commission_rate = float(request.form.get('commission_rate', 5.0))

    if not name or not code:
        flash('Nombre y código son obligatorios.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if Affiliate.query.filter_by(code=code).first():
        flash('Ese código ya existe.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    aff = Affiliate(name=name, code=code, email=email, commission_rate=commission_rate)
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
        show_contact_email=show_contact_email,
        show_pay_id=show_pay_id,
        show_contact_phone=show_contact_phone,
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
    method.show_contact_email = bool(request.form.get('show_contact_email'))
    method.show_pay_id = bool(request.form.get('show_pay_id'))
    method.show_contact_phone = bool(request.form.get('show_contact_phone'))

    new_logo = save_image(request.files.get('logo'), 'payments')
    if new_logo:
        delete_uploaded_file(method.logo)
        method.logo = new_logo

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

@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    usd_rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
    usd_rate = usd_rate_setting.value if usd_rate_setting else ''
    default_pkg_setting = Setting.query.filter_by(key='default_auto_package_id').first()
    default_auto_package_id = default_pkg_setting.value if default_pkg_setting else ''
    site_logo_setting = Setting.query.filter_by(key='site_logo').first()
    site_logo_value = site_logo_setting.value if site_logo_setting else ''

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
        'support_site_url': 'URL del sitio o centro de ayuda',
        'privacy_url': 'URL de política de privacidad',
        'unsubscribe_url': 'URL para darse de baja',
        'admin_notify_email': 'Correo para alertas de nuevas órdenes',
    }
    email_settings = {}
    for key in email_keys:
        setting = Setting.query.filter_by(key=key).first()
        email_settings[key] = setting.value if setting else ''

    if request.method == 'POST':
        new_rate = request.form.get('usd_rate_bs', '').strip()
        default_pkg = request.form.get('default_auto_package_id', '').strip()
        remove_logo = request.form.get('remove_logo')
        logo_file = request.files.get('site_logo')
        social_payload = {k: (request.form.get(k, '') or '').strip() for k in social_keys}
        email_payload = {k: (request.form.get(k, '') or '').strip() for k in email_keys}

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

        db.session.commit()
        flash('Configuración actualizada.', 'success')
        return redirect(url_for('admin_bp.settings'))

    return render_template(
        'admin/settings.html',
        usd_rate=usd_rate,
        default_package_id=default_auto_package_id,
        site_logo=site_logo_value,
        social_settings=social_settings,
        email_settings=email_settings,
    )


# ─── Revendedores Whitelabel API ─────────────────────────────────────────────

def _revendedores_env():
    base_url = current_app.config.get('REVENDEDORES_BASE_URL', '').rstrip('/')
    api_key = current_app.config.get('REVENDEDORES_API_KEY', '')
    catalog_path = '/api/v1/products'
    recharge_path = '/api/v1/recharge'
    return base_url, api_key, catalog_path, recharge_path


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


def _get_order_auto_mapping(order_obj):
    try:
        if not order_obj or not order_obj.package_id:
            return None
        return RevendedoresItemMapping.query.filter_by(
            store_package_id=int(order_obj.package_id),
            active=True,
            auto_enabled=True,
        ).first()
    except Exception:
        return None


@admin_bp.route('/revendedores/mapping')
@login_required
def revendedores_mapping():
    return render_template('admin/revendedores_mapping.html')


@admin_bp.route('/revendedores/sync', methods=['POST'])
@login_required
def revendedores_sync_catalog():
    base_url, api_key, catalog_path, _ = _revendedores_env()
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
            auto_enabled = bool(entry.get('auto_enabled'))

            if not store_pkg_id:
                continue

            existing = RevendedoresItemMapping.query.filter_by(store_package_id=store_pkg_id).first()

            if not catalog_id_str:
                if existing:
                    db.session.delete(existing)
                    removed += 1
                continue

            catalog_id = int(catalog_id_str)
            if existing:
                existing.catalog_item_id = catalog_id
                existing.auto_enabled = auto_enabled
                existing.active = True
            else:
                new_map = RevendedoresItemMapping(
                    store_package_id=store_pkg_id,
                    catalog_item_id=catalog_id,
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
    """Verifica en Revendedores51 si la recarga realmente se completó."""
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

    ext_order_id = auto_resp.get('external_order_id') or order.order_number
    base_url, api_key, _, _ = _revendedores_env()

    if not base_url or not api_key:
        return jsonify({'ok': False, 'error': 'Revendedores API no configurada'})

    try:
        resp = requests.get(
            f'{base_url}/api/v1/order-status',
            params={'external_order_id': ext_order_id},
            headers={'X-API-Key': api_key},
            timeout=15,
        )
        data = resp.json() if resp.ok else {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'No se pudo verificar: {e}', 'can_approve': False})

    if not data.get('ok'):
        return jsonify({'ok': False, 'error': data.get('error', 'Error consultando Revendedores'), 'can_approve': False})

    found = data.get('found', False)
    rev_status = data.get('status', '')
    rev_order = data.get('order', {})

    if found and rev_status == 'completada':
        player_name = rev_order.get('player_name', '')
        ref_no = rev_order.get('reference_no', '')
        order.status = 'completed'
        order.automation_response = json.dumps({
            'source': 'revendedores_api',
            'success': True,
            'verified': True,
            'player_name': player_name,
            'reference_no': ref_no,
        })
        order.notes = (order.notes or '') + f'\n[Verificado] Recarga confirmada en Revendedores. Ref: {ref_no}, Player: {player_name}'
        order.updated_at = datetime.utcnow()
        process_affiliate_commission(order)
        db.session.commit()
        try:
            notify_order_completed(order, order.package, order.game)
        except Exception:
            pass
        return jsonify({
            'ok': True,
            'result': 'completed',
            'order_status': 'completed',
            'player_name': player_name,
            'reference_no': ref_no,
        })
    elif found and rev_status == 'fallida':
        order.automation_response = json.dumps({
            'source': 'revendedores_api',
            'pending_verification': False,
            'verified_failed': True,
            'error': rev_order.get('error', ''),
        })
        db.session.commit()
        return jsonify({
            'ok': True,
            'result': 'failed',
            'order_status': 'pending',
            'can_approve': True,
            'message': 'Recarga falló en Revendedores. Puedes reintentar.',
        })
    elif found and rev_status == 'procesando':
        return jsonify({
            'ok': True,
            'result': 'processing',
            'order_status': 'pending',
            'can_approve': False,
            'message': 'La recarga aún se está procesando en Revendedores...',
        })
    else:
        order.automation_response = json.dumps({
            'source': 'revendedores_api',
            'pending_verification': False,
        })
        db.session.commit()
        return jsonify({
            'ok': True,
            'result': 'not_found',
            'order_status': 'pending',
            'can_approve': True,
            'message': 'No se encontró la recarga en Revendedores. Puedes reintentar.',
        })
