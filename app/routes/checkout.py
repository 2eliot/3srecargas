from datetime import datetime
from decimal import Decimal
import os
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, abort, current_app
)
from flask_login import current_user
from werkzeug.utils import secure_filename
from ..models import db, Game, Package, Order, Affiliate, AffiliateCommission, Pin, PaymentMethod, User, Discount
from ..models import Setting

checkout_bp = Blueprint('checkout_bp', __name__)

PAYMENT_METHODS = [
    ('pago_movil', 'Pago Móvil'),
    ('zelle', 'Zelle'),
    ('binance', 'Binance Pay'),
    ('efectivo', 'Efectivo'),
]


def save_capture(file):
    if not file or file.filename == '':
        return None
    filename = secure_filename(file.filename)
    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S%f')
    filename = f"{ts}_{filename}"
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], 'captures')
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, filename))
    return 'captures/' + filename


@checkout_bp.route('/checkout/<int:package_id>', methods=['GET', 'POST'])
def checkout(package_id):
    package = Package.query.filter_by(id=package_id, is_active=True).first_or_404()
    game = package.game
    is_wallet = game.category.slug == 'wallet'

    usd_rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
    usd_rate = float(usd_rate_setting.value) if usd_rate_setting else 0.0

    checkout_data = session.get('checkout_data') or {}
    pkg_key = str(package_id)

    # El código se guarda por paquete en checkout_data cuando vienes desde index
    affiliate_code = ((checkout_data.get(pkg_key) or {}).get('affiliate_code') or '').strip()
    if not affiliate_code:
        affiliate_code = (session.get('affiliate_code', '') or '').strip()

    if request.method == 'POST':
        stage = request.form.get('stage', '').strip()

        # Paso 1: viene desde index y solo guarda datos en sesión
        if stage != 'confirm':
            player_id = request.form.get('player_id', '').strip()
            player_nickname = request.form.get('player_nickname', '').strip()
            zone_id = request.form.get('zone_id', '').strip()
            email = request.form.get('email', '').strip()
            phone = request.form.get('phone', '').strip()
            payment_method = request.form.get('payment_method', '').strip()
            aff_code = request.form.get('affiliate_code', '').strip()

            if not payment_method:
                flash('Debes seleccionar un método de pago.', 'danger')
                return redirect(url_for('main_bp.index'))

            category_slug = (game.category.slug if game.category else '').lower()
            tarjetas_without_id = category_slug == 'tarjetas'

            if is_wallet:
                if not player_id:
                    flash('Debes ingresar tu correo electrónico.', 'danger')
                    return redirect(url_for('main_bp.index'))
            elif not tarjetas_without_id:
                if not player_id:
                    flash(f'{game.player_id_label} es obligatorio.', 'danger')
                    return redirect(url_for('main_bp.index'))

            checkout_data[pkg_key] = {
                'player_id': player_id,
                'player_nickname': player_nickname,
                'zone_id': zone_id,
                'email': email,
                'phone': phone,
                'payment_method': payment_method,
                'affiliate_code': aff_code,
            }
            session['checkout_data'] = checkout_data
            session['last_payment_method'] = payment_method
            return redirect(url_for('checkout_bp.checkout', package_id=package_id))

        # Paso 2: confirmación (solo capture) -> crea la orden
        data = checkout_data.get(pkg_key) or {}
        payment_method = (data.get('payment_method') or '').strip()
        if not payment_method:
            flash('Tu sesión expiró. Por favor repite el proceso desde la tienda.', 'danger')
            return redirect(url_for('main_bp.index'))

        capture_file = request.files.get('payment_capture')
        if not capture_file or not capture_file.filename:
            flash('Debes adjuntar el comprobante de pago antes de confirmar la orden.', 'danger')
            return redirect(url_for('checkout_bp.checkout', package_id=package_id))

        capture_path = save_capture(capture_file)
        if not capture_path:
            flash('Hubo un problema al subir el comprobante. Intenta nuevamente.', 'danger')
            return redirect(url_for('checkout_bp.checkout', package_id=package_id))

        payment_reference_input = (request.form.get('payment_reference') or '').strip()
        if not payment_reference_input:
            flash('Debes ingresar la referencia del pago.', 'danger')
            return redirect(url_for('checkout_bp.checkout', package_id=package_id))

        existing_ref = Order.query.filter_by(payment_reference=payment_reference_input).first()
        if existing_ref:
            flash('Esta referencia ya fue registrada en otra orden. Verifica tu pago e intenta nuevamente.', 'danger')
            return redirect(url_for('checkout_bp.checkout', package_id=package_id))

        aff_code = (data.get('affiliate_code') or '').strip()
        affiliate = None
        if aff_code:
            affiliate = Affiliate.query.filter_by(code=aff_code, is_active=True).first()

        payment_reference = payment_reference_input[:255]

        # Asociar usuario si está autenticado y es un cliente (no admin)
        user_id = None
        if current_user.is_authenticated and current_user.__class__.__name__ == 'User':
            user_id = current_user.id

        # Procesar descuento si hay código (descuento explícito o código de afiliado)
        discount_code = (data.get('affiliate_code') or '').strip().upper()
        discount = None
        discount_amount = 0.0
        original_amount = float(package.price)
        
        if discount_code:
            discount = Discount.query.filter_by(code=discount_code, is_active=True).first()
            if discount and discount.is_valid_for_amount(package.price):
                discount_amount = float(discount.calculate_discount(package.price))
                # Incrementar contador de uso
                discount.used_count += 1
            elif affiliate:
                # Fallback: usar porcentaje de comisión del afiliado como descuento al cliente
                rate = float(affiliate.commission_rate or 0)
                if rate > 0:
                    discount_amount = round(original_amount * rate / 100.0, 2)
        
        final_amount = max(original_amount - discount_amount, 0.0)

        order = Order(
            game_id=game.id,
            package_id=package.id,
            user_id=user_id,
            discount_id=discount.id if discount else None,
            player_id=(data.get('player_id') or '').strip() if not is_wallet else None,
            player_nickname=(data.get('player_nickname') or '').strip() or None,
            zone_id=(data.get('zone_id') or '').strip() if (not is_wallet and game.requires_zone_id) else None,
            email=(data.get('player_id') or '').strip() if is_wallet else (data.get('email') or '').strip(),
            phone=(data.get('phone') or '').strip() if is_wallet else None,
            payment_method=payment_method,
            payment_reference=payment_reference,
            amount=final_amount,
            original_amount=original_amount,
            discount_amount=discount_amount,
            payment_capture=capture_path,
            affiliate_code=aff_code or None,
            affiliate_id=affiliate.id if affiliate else None,
            status='pending',
        )
        db.session.add(order)
        db.session.commit()

        checkout_data.pop(pkg_key, None)
        session['checkout_data'] = checkout_data
        session.pop('affiliate_code', None)
        return redirect(url_for('checkout_bp.order_status', order_number=order.order_number))

    db_methods = PaymentMethod.query.filter_by(is_active=True).order_by(PaymentMethod.sort_order).all()
    if db_methods:
        payment_methods = [(m.code, m.name) for m in db_methods]
    else:
        payment_methods = PAYMENT_METHODS

    selected_method_code = ((checkout_data.get(pkg_key) or {}).get('payment_method') or '').strip().lower()
    if not selected_method_code:
        selected_method_code = (session.get('last_payment_method') or '').strip().lower()
    selected_method = None
    if selected_method_code:
        selected_method = PaymentMethod.query.filter_by(code=selected_method_code).first()

    display_currency = 'bs'
    if selected_method and (selected_method.account_currency or '').lower() == 'usd':
        display_currency = 'usd'

    usd_amount = float(package.price)
    original_amount = usd_amount
    
    # Calcular descuento si hay código (descuento explícito o afiliado)
    discount_code = affiliate_code.strip().upper() if affiliate_code else ''
    discount = None
    discount_amount = 0.0
    
    if discount_code:
        discount = Discount.query.filter_by(code=discount_code, is_active=True).first()
        if discount and discount.is_valid_for_amount(package.price):
            discount_amount = float(discount.calculate_discount(package.price))
        else:
            affiliate = Affiliate.query.filter_by(code=discount_code, is_active=True).first()
            if affiliate:
                rate = float(affiliate.commission_rate or 0)
                if rate > 0:
                    discount_amount = round(original_amount * rate / 100.0, 2)
    
    final_amount = max(original_amount - discount_amount, 0.0)
    
    if display_currency == 'usd':
        display_amount = final_amount
        original_display = original_amount
        discount_display = discount_amount
    else:
        display_amount = final_amount * (usd_rate or 0.0)
        original_display = original_amount * (usd_rate or 0.0)
        discount_display = discount_amount * (usd_rate or 0.0)

    pkg_data = checkout_data.get(pkg_key) or {}
    player_nickname = (pkg_data.get('player_nickname') or '').strip()
    player_id_val = (pkg_data.get('player_id') or '').strip()

    return render_template(
        'checkout.html',
        package=package,
        game=game,
        is_wallet=is_wallet,
        payment_methods=payment_methods,
        affiliate_code=affiliate_code,
        usd_rate=usd_rate,
        selected_method=selected_method,
        display_currency=display_currency,
        display_amount=display_amount,
        original_amount=original_display,
        discount_amount=discount_display,
        has_discount=discount_amount > 0,
        player_nickname=player_nickname,
        player_id_val=player_id_val,
    )


@checkout_bp.route('/order/<order_number>')
def order_status(order_number):
    order = Order.query.filter_by(order_number=order_number).first_or_404()
    usd_rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
    usd_rate = float(usd_rate_setting.value) if usd_rate_setting else 0.0
    method = PaymentMethod.query.filter_by(code=(order.payment_method or '').strip().lower()).first()
    display_currency = 'bs'
    if method and (method.account_currency or '').lower() == 'usd':
        display_currency = 'usd'
    usd_amount = float(order.amount)
    display_amount = usd_amount if display_currency == 'usd' else (usd_amount * (usd_rate or 0.0))
    return render_template(
        'order_status.html',
        order=order,
        usd_rate=usd_rate,
        display_currency=display_currency,
        display_amount=display_amount,
    )
