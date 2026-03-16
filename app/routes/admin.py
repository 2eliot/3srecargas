import os
import json
import requests
from datetime import datetime
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
    Pin, Affiliate, AffiliateCommission, PaymentMethod, Setting
)

admin_bp = Blueprint('admin_bp', __name__)

ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT


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
        try:
            resp = requests.post(
                current_app.config['AUTOMATION_SERVICE_URL'],
                json={
                    'order_number': order.order_number,
                    'player_id': order.player_id,
                    'zone_id': order.zone_id,
                    'pin': pin.code if pin else None,
                    'package': package.name,
                    'game': order.game.name,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                pin.is_used = True
                pin.used_at = datetime.utcnow()
                pin.order_id = order.id
                order.status = 'completed'
                order.pin_id = pin.id
                order.pin_delivered = pin.code
                order.automation_response = resp.text
                order.updated_at = datetime.utcnow()
                process_affiliate_commission(order)
                db.session.commit()
                flash(f'Orden #{order.order_number} completada vía automatización.', 'success')
            else:
                flash(f'Error en automatización ({resp.status_code}): {resp.text}', 'danger')
        except requests.exceptions.RequestException as e:
            flash(f'Error de conexión con el servicio de automatización: {e}', 'danger')
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
        flash(f'Orden #{order.order_number} completada y PIN entregado.', 'success')
    else:
        order.status = 'approved'
        order.updated_at = datetime.utcnow()
        process_affiliate_commission(order)
        db.session.commit()
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

    if request.method == 'POST':
        new_rate = request.form.get('usd_rate_bs', '').strip()
        default_pkg = request.form.get('default_auto_package_id', '').strip()
        remove_logo = request.form.get('remove_logo')
        logo_file = request.files.get('site_logo')

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
            site_logo_setting.value = ''

        if logo_file and logo_file.filename:
            saved_logo = save_image(logo_file, 'branding')
            if saved_logo:
                if not site_logo_setting:
                    site_logo_setting = Setting(
                        key='site_logo',
                        value=saved_logo,
                        description='Logo personalizado para el header'
                    )
                    db.session.add(site_logo_setting)
                else:
                    site_logo_setting.value = saved_logo

        db.session.commit()
        flash('Configuración actualizada.', 'success')
        return redirect(url_for('admin_bp.settings'))

    return render_template(
        'admin/settings.html',
        usd_rate=usd_rate,
        default_package_id=default_auto_package_id,
        site_logo=site_logo_value,
    )
