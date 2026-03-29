from decimal import Decimal
from datetime import datetime
import os
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, abort, current_app, jsonify
)
from flask_login import current_user
from werkzeug.utils import secure_filename
from ..models import db, Game, Package, Order, Affiliate, AffiliateCommission, Pin, PaymentMethod, User, Discount
from ..models import Setting
from ..utils.order_processing import approve_order, get_order_auto_mapping
from ..utils.payment_verification import (
    is_auto_verify_enabled,
    normalize_reference_last5,
    stamp_verified_payment,
    verify_order_payment,
)
from ..utils.timezone import now_ve_naive
from ..utils.notifications import notify_order_created

checkout_bp = Blueprint('checkout_bp', __name__)

AUTO_VERIFY_MAX_ATTEMPTS = 2
AUTO_VERIFY_COOLDOWN_SECONDS = 300

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
    ts = now_ve_naive().strftime('%Y%m%d%H%M%S%f')
    filename = f"{ts}_{filename}"
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], 'captures')
    os.makedirs(folder, exist_ok=True)
    file.save(os.path.join(folder, filename))
    return 'captures/' + filename


def auto_verify_and_process_order(order, force=False):
    auto_mapped = bool(get_order_auto_mapping(order)) if order else False
    if not order or order.status != 'pending' or not is_auto_verify_enabled() or not auto_mapped:
        return {'checked': False, 'verified': False, 'message': '', 'stop_polling': True}

    attempts = int(order.payment_verification_attempts or 0)
    if attempts >= AUTO_VERIFY_MAX_ATTEMPTS and not force and not order.payment_verified_at:
        return {
            'checked': False,
            'verified': False,
            'message': 'Se alcanzó el máximo de intentos automáticos de verificación para esta orden.',
            'stop_polling': True,
        }

    if not force and order.payment_last_verification_at:
        elapsed = (datetime.utcnow() - order.payment_last_verification_at).total_seconds()
        if elapsed < AUTO_VERIFY_COOLDOWN_SECONDS:
            wait_seconds = int(AUTO_VERIFY_COOLDOWN_SECONDS - elapsed)
            if wait_seconds < 5:
                wait_seconds = 5
            return {
                'checked': False,
                'verified': False,
                'message': f'Esperando {wait_seconds}s para el siguiente intento automático.',
                'stop_polling': False,
                'next_retry_in_seconds': wait_seconds,
            }

    verification = verify_order_payment(order)
    order.payment_verification_attempts = attempts + 1
    order.payment_last_verification_at = datetime.utcnow()

    if verification.get('verified'):
        stamp_verified_payment(order, verification)
        pabilo_note = '[Pabilo] Pago verificado automáticamente.'
        if verification.get('verification_id'):
            pabilo_note = f"{pabilo_note} ID: {verification['verification_id']}"
        existing_notes = order.notes or ''
        if pabilo_note not in existing_notes:
            order.notes = (existing_notes + '\n' + pabilo_note).strip()
        db.session.commit()
        approval = approve_order(order)
        approval['checked'] = True
        approval['verified'] = approval.get('ok', False)
        approval['stop_polling'] = True
        return approval

    if verification.get('message'):
        existing_notes = order.notes or ''
        auto_note = f"[Pabilo] {verification['message']}"
        if auto_note not in existing_notes:
            order.notes = (existing_notes + '\n' + auto_note).strip()
            db.session.commit()

    verification['checked'] = True
    verification['stop_polling'] = False
    if verification.get('rate_limited'):
        verification['next_retry_in_seconds'] = AUTO_VERIFY_COOLDOWN_SECONDS
    else:
        verification['next_retry_in_seconds'] = AUTO_VERIFY_COOLDOWN_SECONDS

    if int(order.payment_verification_attempts or 0) >= AUTO_VERIFY_MAX_ATTEMPTS:
        verification['stop_polling'] = True

    db.session.commit()
    return verification


@checkout_bp.route('/checkout/<int:package_id>', methods=['GET', 'POST'])
def checkout(package_id):
    package = Package.query.filter_by(id=package_id, is_active=True).first_or_404()
    game = package.game
    is_wallet = game.category.slug == 'wallet'

    aff_from_query = (request.args.get('aff') or request.args.get('affiliate_code') or '').strip()
    if aff_from_query:
        aff_match = Affiliate.query.filter_by(code=aff_from_query, is_active=True).first()
        if aff_match:
            session['affiliate_code'] = aff_match.code

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
            if not aff_code:
                aff_code = (session.get('affiliate_code', '') or '').strip()

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
        if not aff_code:
            aff_code = (session.get('affiliate_code', '') or '').strip()
        affiliate = None
        if aff_code:
            affiliate = Affiliate.query.filter_by(code=aff_code, is_active=True).first()

        payment_reference = payment_reference_input[:255]
        payment_reference_last5 = normalize_reference_last5(payment_reference)

        # Asociar usuario si está autenticado y es un cliente (no admin)
        user_id = None
        if current_user.is_authenticated and current_user.__class__.__name__ == 'User':
            user_id = current_user.id

        customer_phone = (data.get('phone') or '').strip()
        if not customer_phone and user_id:
            customer_phone = (current_user.phone or '').strip()

        # Procesar descuento si hay código (descuento explícito o código de afiliado)
        discount_code = ((data.get('affiliate_code') or aff_code or '').strip()).upper()
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
                # Usar % de descuento al cliente; fallback para afiliados antiguos
                rate = float(affiliate.client_discount_rate or 0)
                if rate <= 0:
                    rate = float(affiliate.commission_rate or 0)
                if rate > 0:
                    raw_discount = original_amount * rate / 100.0
                    discount_amount = round(raw_discount, 2)
                    if raw_discount > 0 and discount_amount <= 0:
                        discount_amount = 0.01
                    if discount_amount > original_amount:
                        discount_amount = round(original_amount, 2)
        
        final_amount = max(original_amount - discount_amount, 0.0)

        method_config = PaymentMethod.query.filter_by(code=payment_method.lower()).first()
        payment_currency = 'usd'
        payment_amount = final_amount
        if method_config and (method_config.account_currency or '').lower() == 'bs':
            payment_currency = 'bs'
            payment_amount = round(final_amount * (usd_rate or 0.0), 2)

        order = Order(
            game_id=game.id,
            package_id=package.id,
            user_id=user_id,
            discount_id=discount.id if discount else None,
            player_id=(data.get('player_id') or '').strip() if not is_wallet else None,
            player_nickname=(data.get('player_nickname') or '').strip() or None,
            zone_id=(data.get('zone_id') or '').strip() if (not is_wallet and game.requires_zone_id) else None,
            email=(data.get('player_id') or '').strip() if is_wallet else (data.get('email') or '').strip(),
            phone=customer_phone or None,
            payment_method=payment_method,
            payment_reference=payment_reference,
            payment_reference_last5=payment_reference_last5 or None,
            payment_amount=payment_amount,
            payment_currency=payment_currency,
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

        try:
            notify_order_created(order, package, game)
        except Exception:
            pass

        auto_verify_and_process_order(order)

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
    discount_code = ((affiliate_code or session.get('affiliate_code', '') or '').strip()).upper()
    discount = None
    discount_amount = 0.0
    
    if discount_code:
        discount = Discount.query.filter_by(code=discount_code, is_active=True).first()
        if discount and discount.is_valid_for_amount(package.price):
            discount_amount = float(discount.calculate_discount(package.price))
        else:
            affiliate = Affiliate.query.filter_by(code=discount_code, is_active=True).first()
            if affiliate:
                rate = float(affiliate.client_discount_rate or 0)
                if rate <= 0:
                    rate = float(affiliate.commission_rate or 0)
                if rate > 0:
                    raw_discount = original_amount * rate / 100.0
                    discount_amount = round(raw_discount, 2)
                    if raw_discount > 0 and discount_amount <= 0:
                        discount_amount = 0.01
                    if discount_amount > original_amount:
                        discount_amount = round(original_amount, 2)
    
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
    if order.payment_amount is not None and (order.payment_currency or '').lower() == display_currency:
        display_amount = float(order.payment_amount)
    else:
        usd_amount = float(order.amount)
        display_amount = usd_amount if display_currency == 'usd' else (usd_amount * (usd_rate or 0.0))

    auto_verify_enabled = is_auto_verify_enabled()
    auto_mapped = bool(get_order_auto_mapping(order))
    auto_verify_allowed = auto_verify_enabled and auto_mapped

    return render_template(
        'order_status.html',
        order=order,
        usd_rate=usd_rate,
        display_currency=display_currency,
        display_amount=display_amount,
        auto_verify_enabled=auto_verify_enabled,
        auto_verify_allowed=auto_verify_allowed,
        is_manual_order=not auto_verify_allowed,
    )


@checkout_bp.route('/order/<order_number>/auto-verify', methods=['POST'])
def order_auto_verify(order_number):
    order = Order.query.filter_by(order_number=order_number).first_or_404()
    auto_verify_allowed = is_auto_verify_enabled() and bool(get_order_auto_mapping(order))
    if not auto_verify_allowed:
        return jsonify({
            'ok': True,
            'checked': False,
            'verified': False,
            'message': 'Esta orden se procesa manualmente por operador.',
            'stop_polling': True,
            'next_retry_in_seconds': 0,
            'status': order.status,
            'status_label': order.status_label,
            'auto_verify_enabled': is_auto_verify_enabled(),
            'auto_verify_allowed': False,
        })

    result = auto_verify_and_process_order(order)
    db.session.refresh(order)
    return jsonify({
        'ok': result.get('ok', True),
        'checked': result.get('checked', False),
        'verified': result.get('verified', False),
        'message': result.get('message', ''),
        'stop_polling': result.get('stop_polling', False),
        'next_retry_in_seconds': result.get('next_retry_in_seconds', 0),
        'status': order.status,
        'status_label': order.status_label,
        'auto_verify_enabled': is_auto_verify_enabled(),
        'auto_verify_allowed': auto_verify_allowed,
    })
