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
from ..utils.timezone import format_ve, now_ve, now_ve_naive, to_ve, ve_day_start_utc_naive
from ..utils.notifications import (
    notify_order_approved, notify_order_completed, notify_order_rejected,
)
from ..utils.order_processing import approve_order, get_revendedores_env, process_affiliate_commission

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
    ts = now_ve_naive().strftime('%Y%m%d%H%M%S%f')
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


# ─── Auth ────────────────────────────────────────────────────────────────────

@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_bp.dashboard'))

    env_admin_username = (os.environ.get('ADMIN_USERNAME') or '').strip()
    env_admin_password = (os.environ.get('ADMIN_PASSWORD') or '').strip()
    env_admin_email = (os.environ.get('ADMIN_EMAIL') or '').strip()

    if request.method == 'POST':
        if not env_admin_username or not env_admin_password:
            flash('Acceso admin no disponible: faltan ADMIN_USERNAME/ADMIN_PASSWORD en entorno.', 'danger')
            return render_template('admin/login.html')

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        if username != env_admin_username or password != env_admin_password:
            flash('Usuario o contraseña incorrectos.', 'danger')
            return render_template('admin/login.html')

        try:
            user = AdminUser.query.filter_by(username=env_admin_username).first()
            if not user and env_admin_email:
                user = AdminUser.query.filter_by(email=env_admin_email).first()

            if not user and AdminUser.query.count() == 1:
                user = AdminUser.query.first()

            if not user:
                user = AdminUser(
                    username=env_admin_username,
                    email=env_admin_email or f'{env_admin_username}@localhost',
                )
                user.set_password(env_admin_password)
                db.session.add(user)
                db.session.commit()
            else:
                needs_commit = False
                if user.username != env_admin_username:
                    username_taken = (
                        AdminUser.query
                        .filter(AdminUser.username == env_admin_username, AdminUser.id != user.id)
                        .first()
                    )
                    if username_taken:
                        flash('ADMIN_USERNAME ya existe en otro registro admin.', 'danger')
                        return render_template('admin/login.html')
                    user.username = env_admin_username
                    needs_commit = True
                if env_admin_email and user.email != env_admin_email:
                    email_taken = (
                        AdminUser.query
                        .filter(AdminUser.email == env_admin_email, AdminUser.id != user.id)
                        .first()
                    )
                    if email_taken:
                        flash('ADMIN_EMAIL ya existe en otro registro admin.', 'danger')
                        return render_template('admin/login.html')
                    user.email = env_admin_email
                    needs_commit = True
                if not user.check_password(env_admin_password):
                    user.set_password(env_admin_password)
                    needs_commit = True
                if needs_commit:
                    db.session.commit()
        except Exception:
            db.session.rollback()
            flash('No se pudo sincronizar la cuenta de administrador. Revisa ADMIN_USERNAME/ADMIN_EMAIL.', 'danger')
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


@admin_bp.route('/orders/latest')
@login_required
def orders_latest():
    status_filter = (request.args.get('status') or '').strip()
    since_id_raw = (request.args.get('since_id') or '').strip()
    try:
        since_id = int(since_id_raw) if since_id_raw else 0
    except Exception:
        since_id = 0

    query = Order.query
    if status_filter:
        query = query.filter_by(status=status_filter)
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
        })

    return jsonify({'ok': True, 'orders': payload})


@admin_bp.route('/orders/<int:order_id>')
@login_required
def order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template('admin/order_detail.html', order=order)


@admin_bp.route('/orders/<int:order_id>/approve', methods=['POST'])
@login_required
def order_approve(order_id):
    order = Order.query.get_or_404(order_id)
    result = approve_order(order)
    flash(result['message'], result['category'])

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
    commission_rate = float(request.form.get('commission_rate', 1.0))
    client_discount_rate = float(request.form.get('client_discount_rate', 2.0))

    if not name or not code:
        flash('Nombre y código son obligatorios.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    if Affiliate.query.filter_by(code=code).first():
        flash('Ese código ya existe.', 'danger')
        return redirect(url_for('admin_bp.affiliates'))

    aff = Affiliate(name=name, code=code, email=email, commission_rate=commission_rate, client_discount_rate=client_discount_rate)
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
        pabilo_user_bank_id=pabilo_user_bank_id,
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
    method.pabilo_user_bank_id = request.form.get('pabilo_user_bank_id', '').strip() or None
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
    payment_verify_keys = {
        'auto_verify_payments': 'Habilita la verificación automática de pagos con Pabilo',
        'pabilo_api_key': 'API key privada de Pabilo para validar pagos',
    }
    binance_auto_keys = {
        'binance_auto_enabled': 'Activa verificación automática de Binance Pay',
        'binance_wallet_address': 'Dirección/email de Binance Pay que se muestra al cliente',
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

    if request.method == 'POST':
        new_rate = request.form.get('usd_rate_bs', '').strip()
        default_pkg = request.form.get('default_auto_package_id', '').strip()
        remove_logo = request.form.get('remove_logo')
        logo_file = request.files.get('site_logo')
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
        payment_verify_settings=payment_verify_settings,
        binance_auto_settings=binance_auto_settings,
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
    base_url, api_key, _, _ = get_revendedores_env()

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


# ─── Statistics ──────────────────────────────────────────────────────────────

@admin_bp.route('/stats')
@login_required
def stats():
    today = now_ve().date()

    days = [today, today - timedelta(days=1), today - timedelta(days=2)]

    day_keys = [d.isoformat() for d in days]

    # Keep the same order used in admin lists: game.position then package.sort_order
    games = Game.query.filter_by(is_active=True).order_by(Game.position.asc(), Game.id.asc()).all()

    ordered_rows = []
    row_map = {}
    for game in games:
        packages = game.packages.filter_by(is_active=True).order_by(Package.sort_order.asc(), Package.id.asc()).all()
        for pkg in packages:
            cells = {k: {'total': 0, 'completed': 0, 'pending': 0} for k in day_keys}
            row = {
                'game_name': game.name,
                'pkg_name': pkg.name,
                'cells': cells,
            }
            ordered_rows.append(row)
            row_map[(game.id, pkg.id)] = row

    # Convert Venezuela day window to UTC for querying stored timestamps.
    window_start = ve_day_start_utc_naive(days[2])
    window_end = ve_day_start_utc_naive(today + timedelta(days=1))

    rows = Order.query.filter(
        Order.created_at >= window_start,
        Order.created_at < window_end,
        Order.status.in_(['pending', 'approved', 'completed']),
    ).all()

    for order in rows:
        created_at_ve = to_ve(order.created_at)
        if created_at_ve is None:
            continue

        day_iso = created_at_ve.date().isoformat()
        data_row = row_map.get((order.game_id, order.package_id))
        if not data_row or day_iso not in data_row['cells']:
            continue

        cell = data_row['cells'][day_iso]
        cell['total'] += 1
        if order.status in ('completed', 'approved'):
            cell['completed'] += 1
        elif order.status == 'pending':
            cell['pending'] += 1

    return render_template(
        'admin/stats.html',
        days=days,
        ordered_rows=ordered_rows,
    )
