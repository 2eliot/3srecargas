from decimal import Decimal
from datetime import datetime, timedelta
import os
import threading
from uuid import uuid4
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, abort, current_app, jsonify
)
from flask_login import current_user, login_user, logout_user
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename
from ..models import db, Game, Package, Order, Affiliate, AffiliateCommission, Pin, PaymentMethod, User, Discount
from ..models import Setting
from ..utils.order_processing import approve_order, get_order_auto_mapping
from ..utils.payment_verification import (
    is_auto_verify_enabled,
    normalize_bs_integer_amount,
    normalize_reference_last5,
    stamp_verified_payment,
    verify_order_payment,
)
from ..utils.binance_pay import (
    is_binance_auto_enabled,
    is_binance_auto_reference,
    generate_binance_auto_code,
    start_order_verification,
)
from ..utils.timezone import now_ve_naive
from ..utils.notifications import notify_order_created
from ..utils.auth_accounts import attach_matching_orders_to_customer, extract_customer_identifier_for_game, get_or_create_scoped_customer

checkout_bp = Blueprint('checkout_bp', __name__)

# 1 intento automático al crear la orden. El botón manual siempre puede reintentar (force=True).
AUTO_VERIFY_MAX_ATTEMPTS = 1
AUTO_VERIFY_COOLDOWN_SECONDS = 60

# Cola simple en memoria: solo 1 solicitud a Pabilo al mismo tiempo.
_PABILO_VERIFY_LOCK = threading.Lock()

PAYMENT_METHODS = [
    ('pago_movil', 'Pago Móvil'),
    ('zelle', 'Zelle'),
    ('binance', 'Binance Pay'),
    ('efectivo', 'Efectivo'),
]


def _normalize_bs_checkout_amount(value):
    normalized = normalize_bs_integer_amount(value)
    return normalized if normalized is not None else 0


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


def order_qualifies_for_auto_verify(order):
    if not order:
        return False

    has_mapping = bool(get_order_auto_mapping(order))
    category_slug = (order.game.category.slug if order.game and order.game.category else '').lower()
    uses_pin_stock = bool(order.package and order.package.is_automated) or category_slug == 'tarjetas'
    return has_mapping or uses_pin_stock


def auto_verify_and_process_order(order, force=False):
    auto_allowed = order_qualifies_for_auto_verify(order)
    if not order or order.status != 'pending' or not is_auto_verify_enabled() or not auto_allowed:
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

    # Si otro pedido ya está verificándose, este queda en cola para el siguiente ciclo.
    if not _PABILO_VERIFY_LOCK.acquire(blocking=False):
        return {
            'checked': False,
            'verified': False,
            'message': 'Hay otra orden verificándose en este momento. Tu orden quedó en cola.',
            'stop_polling': False,
            'next_retry_in_seconds': 10,
        }

    try:
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
    finally:
        _PABILO_VERIFY_LOCK.release()


def find_existing_pending_order(package_id, payment_method, user_id=None, player_id=None, email=None):
    """Return a recent pending order that likely represents the same checkout attempt."""
    cutoff = datetime.utcnow() - timedelta(hours=2)
    query = Order.query.filter(
        Order.package_id == package_id,
        Order.payment_method == payment_method,
        Order.status == 'pending',
        Order.created_at >= cutoff,
    )

    identity_filters = []
    if user_id:
        identity_filters.append(Order.user_id == user_id)
    if player_id:
        identity_filters.append(Order.player_id == player_id)
    if email:
        identity_filters.append(Order.email == email)

    if not identity_filters:
        return None

    return query.filter(or_(*identity_filters)).order_by(Order.id.desc()).first()


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
    checkout_confirm_tokens = session.get('checkout_confirm_tokens') or {}
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
            elif tarjetas_without_id:
                if not email:
                    flash('Debes ingresar el correo de entrega para esta tarjeta o gift card.', 'danger')
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

            identity_meta = extract_customer_identifier_for_game(game, player_id=player_id, email=email)
            identifier_value = (identity_meta.get('identifier') or '').strip()
            if identifier_value and current_user.__class__.__name__ != 'AdminUser':
                scoped_user = get_or_create_scoped_customer(
                    scope_key=identity_meta['scope_key'],
                    scope_label=identity_meta['scope_label'],
                    raw_identifier=identifier_value,
                    account_kind=identity_meta['account_kind'],
                    contact_email=email,
                    phone=phone,
                )
                if scoped_user:
                    attach_matching_orders_to_customer(scoped_user, game.id, identifier_value, identity_meta['account_kind'])
                    if current_user.is_authenticated and current_user.__class__.__name__ == 'User' and current_user.id != scoped_user.id:
                        logout_user()
                    login_user(scoped_user)

            session['checkout_data'] = checkout_data
            session['last_payment_method'] = payment_method
            return redirect(url_for('checkout_bp.checkout', package_id=package_id))

        # Paso 2: confirmación (solo capture) -> crea la orden
        submitted_confirm_token = (request.form.get('confirm_token') or '').strip()
        expected_confirm_token = (checkout_confirm_tokens.get(pkg_key) or '').strip()
        if not submitted_confirm_token or not expected_confirm_token or submitted_confirm_token != expected_confirm_token:
            flash('La confirmación de pago expiró o ya fue usada. Recarga la página e intenta de nuevo.', 'danger')
            return redirect(url_for('checkout_bp.checkout', package_id=package_id))

        existing_by_token = Order.query.filter_by(idempotency_key=submitted_confirm_token).first()
        if existing_by_token:
            return redirect(url_for('checkout_bp.order_status', order_number=existing_by_token.order_number))

        data = checkout_data.get(pkg_key) or {}
        payment_method = (data.get('payment_method') or '').strip()
        if not payment_method:
            flash('Tu sesión expiró. Por favor repite el proceso desde la tienda.', 'danger')
            return redirect(url_for('main_bp.index'))

        # ── Detect Binance Pay auto-verification flow ──────────────────────────
        _binance_auto = (
            payment_method.lower() == 'binance'
            and is_binance_auto_enabled(current_app._get_current_object())
        )

        capture_path = None
        if _binance_auto:
            # For Binance auto, capture is not required.
            # The payment_reference is the 6-char code stored in the session.
            binance_codes = session.get('binance_codes') or {}
            payment_reference_input = (
                binance_codes.get(pkg_key)
                or (request.form.get('payment_reference') or '').strip().upper()
            )
            if not payment_reference_input or not is_binance_auto_reference(payment_reference_input):
                flash('Código Binance inválido. Por favor recarga la página e intenta de nuevo.', 'danger')
                return redirect(url_for('checkout_bp.checkout', package_id=package_id))
        else:
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

        existing_ref = Order.query.filter_by(payment_reference=payment_reference_input, status='pending').first()
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

        # Evita múltiples órdenes pendientes del mismo checkout.
        # Si ya existe una reciente con la misma identidad, se reutiliza.
        existing_pending = find_existing_pending_order(
            package_id=package.id,
            payment_method=payment_method,
            user_id=user_id,
            player_id=(data.get('player_id') or '').strip() if not is_wallet else None,
            email=(data.get('player_id') or '').strip() if is_wallet else (data.get('email') or '').strip(),
        )
        if existing_pending:
            flash('Ya tienes una orden pendiente para esta compra. Te llevamos a su estado.', 'info')
            return redirect(url_for('checkout_bp.order_status', order_number=existing_pending.order_number))

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
        payment_amount = round(final_amount, 2)
        # Binance auto siempre se maneja en USD/USDT.
        if not _binance_auto and method_config and (method_config.account_currency or '').lower() == 'bs':
            payment_currency = 'bs'
            if bool(method_config.uses_rate):
                payment_amount = _normalize_bs_checkout_amount(final_amount * (usd_rate or 0.0))
            else:
                payment_amount = _normalize_bs_checkout_amount(final_amount)

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
            idempotency_key=submitted_confirm_token,
            status='pending',
        )
        db.session.add(order)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing_by_token = Order.query.filter_by(idempotency_key=submitted_confirm_token).first()
            if existing_by_token:
                return redirect(url_for('checkout_bp.order_status', order_number=existing_by_token.order_number))
            flash('No se pudo confirmar la orden. Intenta nuevamente.', 'danger')
            return redirect(url_for('checkout_bp.checkout', package_id=package_id))

        try:
            notify_order_created(order, package, game)
        except Exception:
            pass

        if _binance_auto:
            # Launch a dedicated per-order verification thread.
            # The Binance API is only called from THIS point, never at startup.
            _app = current_app._get_current_object()
            start_order_verification(order, _app)
            # Remove the used code from session so a fresh one is generated next time.
            binance_codes = session.get('binance_codes') or {}
            binance_codes.pop(pkg_key, None)
            session['binance_codes'] = binance_codes
        else:
            auto_verify_and_process_order(order)

        checkout_data.pop(pkg_key, None)
        session['checkout_data'] = checkout_data
        checkout_confirm_tokens.pop(pkg_key, None)
        session['checkout_confirm_tokens'] = checkout_confirm_tokens
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

    # ── Binance Pay auto-verification ──────────────────────────────────────────
    _app = current_app._get_current_object()
    binance_auto = (
        selected_method_code == 'binance'
        and is_binance_auto_enabled(_app)
    )

    display_currency = 'bs'
    if binance_auto:
        # Binance auto se muestra y cobra en USDT.
        display_currency = 'usd'
    elif selected_method and (selected_method.account_currency or '').lower() == 'usd':
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
        if selected_method and not bool(selected_method.uses_rate):
            display_amount = _normalize_bs_checkout_amount(final_amount)
            original_display = _normalize_bs_checkout_amount(original_amount)
            discount_display = _normalize_bs_checkout_amount(discount_amount)
        else:
            # Mantener consistente el monto mostrado con el monto que se guarda en la orden
            # para evitar discrepancias al verificar en Pabilo.
            display_amount = _normalize_bs_checkout_amount(final_amount * (usd_rate or 0.0))
            original_display = _normalize_bs_checkout_amount(original_amount * (usd_rate or 0.0))
            discount_display = _normalize_bs_checkout_amount(discount_amount * (usd_rate or 0.0))

    pkg_data = checkout_data.get(pkg_key) or {}
    player_nickname = (pkg_data.get('player_nickname') or '').strip()
    player_id_val = (pkg_data.get('player_id') or '').strip()

    confirm_token = checkout_confirm_tokens.get(pkg_key)
    if not confirm_token:
        confirm_token = uuid4().hex
        checkout_confirm_tokens[pkg_key] = confirm_token
        session['checkout_confirm_tokens'] = checkout_confirm_tokens

    binance_code = None
    binance_wallet = ''
    binance_codes = session.get('binance_codes') or {}
    if binance_auto:
        # Reuse or generate a code for this package session
        # The payment_reference is the 6-digit code stored in the session.
        existing_code = binance_codes.get(pkg_key)
        if existing_code and is_binance_auto_reference(existing_code):
            binance_code = existing_code
        else:
            binance_code = generate_binance_auto_code(_app)
            binance_codes[pkg_key] = binance_code
            session['binance_codes'] = binance_codes
        # Wallet address to show to customer
        wallet_setting = Setting.query.filter_by(key='binance_wallet_address').first()
        binance_wallet = wallet_setting.value if wallet_setting else ''

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
        confirm_token=confirm_token,
        binance_auto=binance_auto,
        binance_code=binance_code,
        binance_wallet=binance_wallet,
    )


@checkout_bp.route('/order/<order_number>')
def order_status(order_number):
    order = Order.query.filter_by(order_number=order_number).first_or_404()
    usd_rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
    usd_rate = float(usd_rate_setting.value) if usd_rate_setting else 0.0
    order_status_image_setting = Setting.query.filter_by(key='order_status_image').first()
    order_status_image = order_status_image_setting.value if order_status_image_setting else ''
    method = PaymentMethod.query.filter_by(code=(order.payment_method or '').strip().lower()).first()
    display_currency = 'bs'
    if method and (method.account_currency or '').lower() == 'usd':
        display_currency = 'usd'
    if order.payment_amount is not None and (order.payment_currency or '').lower() == display_currency:
        display_amount = float(order.payment_amount)
    else:
        usd_amount = float(order.amount)
        display_amount = usd_amount if display_currency == 'usd' else (usd_amount * (usd_rate or 0.0))

    # Binance auto order: has a 6-digit numeric reference
    is_binance_auto_order = (
        (order.payment_method or '').lower() == 'binance'
        and is_binance_auto_reference(order.payment_reference or '')
    )

    auto_verify_enabled = is_auto_verify_enabled()
    # Don't run Pabilo auto-verify on Binance auto orders; the background thread handles them
    auto_verify_allowed = (
        auto_verify_enabled
        and order_qualifies_for_auto_verify(order)
        and not is_binance_auto_order
    )

    return render_template(
        'order_status.html',
        order=order,
        usd_rate=usd_rate,
        display_currency=display_currency,
        display_amount=display_amount,
        auto_verify_enabled=auto_verify_enabled,
        auto_verify_allowed=auto_verify_allowed,
        is_manual_order=not auto_verify_allowed and not is_binance_auto_order,
        is_binance_auto_order=is_binance_auto_order,
        order_status_image=order_status_image,
    )


@checkout_bp.route('/order/<order_number>/auto-verify', methods=['POST'])
def order_auto_verify(order_number):
    order = Order.query.filter_by(order_number=order_number).first_or_404()
    auto_verify_allowed = is_auto_verify_enabled() and order_qualifies_for_auto_verify(order)
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

    # force=True: el usuario lo pidió manualmente, ignorar límite de intentos.
    result = auto_verify_and_process_order(order, force=True)
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


@checkout_bp.route('/order/<order_number>/status-json')
def order_status_json(order_number):
    """Lightweight endpoint used by Binance auto-verify polling on the order status page."""
    order = Order.query.filter_by(order_number=order_number).first_or_404()
    return jsonify({'status': order.status, 'status_label': order.status_label})
