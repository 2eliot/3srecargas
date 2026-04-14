import os
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from ..models import db, User, Order, Game
from ..utils.auth_accounts import find_scoped_customer, get_game_account_meta, hydrate_scoped_customer_from_orders, sync_env_admin_user

auth_bp = Blueprint('auth_bp', __name__)

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    flash('Ya no necesitas crear cuenta. Tu acceso se genera automáticamente con tu primer ID o correo de compra.', 'info')
    return redirect(url_for('auth_bp.login'))

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        # Redirigir según el tipo de usuario
        if current_user.__class__.__name__ == 'AdminUser':
            return redirect(url_for('admin_bp.dashboard'))
        return redirect(url_for('main_bp.index'))
    
    env_admin_username = (os.environ.get('ADMIN_USERNAME') or '').strip()
    env_admin_password = (os.environ.get('ADMIN_PASSWORD') or '').strip()
    env_admin_email = (os.environ.get('ADMIN_EMAIL') or '').strip()
    active_games = Game.query.filter_by(is_active=True).order_by(Game.position.asc(), Game.name.asc()).all()

    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        service_id_raw = request.form.get('service_id', '').strip()
        admin_password = request.form.get('admin_password', '').strip()

        if not identifier:
            flash('Ingresa tu ID, correo o cuenta del servicio.', 'danger')
            return render_template('auth/login.html', active_games=active_games, admin_email_hint=env_admin_email)

        if env_admin_email and identifier.lower() == env_admin_email.lower():
            if not admin_password:
                flash('Ingresa la clave del administrador para continuar.', 'danger')
                return render_template('auth/login.html', active_games=active_games, admin_email_hint=env_admin_email)
            if admin_password != env_admin_password:
                flash('Clave de administrador incorrecta.', 'danger')
                return render_template('auth/login.html', active_games=active_games, admin_email_hint=env_admin_email)

            try:
                admin = sync_env_admin_user(env_admin_username, env_admin_email, env_admin_password)
            except Exception as exc:
                db.session.rollback()
                flash(f'No se pudo sincronizar la cuenta admin. {exc}', 'danger')
                return render_template('auth/login.html', active_games=active_games, admin_email_hint=env_admin_email)

            login_user(admin)
            flash('Sesión de administrador iniciada.', 'success')
            return redirect(url_for('admin_bp.dashboard'))

        if not service_id_raw.isdigit():
            flash('Selecciona el juego o servicio para ubicar tu historial.', 'danger')
            return render_template('auth/login.html', active_games=active_games, admin_email_hint=env_admin_email)

        game = Game.query.filter_by(id=int(service_id_raw), is_active=True).first()
        if not game:
            flash('El servicio seleccionado no está disponible.', 'danger')
            return render_template('auth/login.html', active_games=active_games, admin_email_hint=env_admin_email)

        account_meta = get_game_account_meta(game)
        user = find_scoped_customer(account_meta['scope_key'], identifier, account_meta['account_kind'])
        if not user:
            user = hydrate_scoped_customer_from_orders(game, identifier)
        if not user:
            flash('No encontramos compras previas para ese identificador en ese servicio. Haz tu primera recarga y tu acceso se creará automáticamente.', 'warning')
            return render_template('auth/login.html', active_games=active_games, admin_email_hint=env_admin_email)

        login_user(user)
        flash('Sesión iniciada con tu identificador actual.', 'success')
        next_page = request.args.get('next')
        return redirect(next_page or url_for('auth_bp.profile'))

    return render_template('auth/login.html', active_games=active_games, admin_email_hint=env_admin_email)

@auth_bp.route('/logout', methods=['GET', 'POST'])
@login_required
def logout():
    logout_user()
    flash('Has cerrado sesión exitosamente.', 'info')
    return redirect(url_for('main_bp.index'))

@auth_bp.route('/profile')
@login_required
def profile():
    # Solo usuarios normales pueden ver perfil
    if current_user.__class__.__name__ == 'AdminUser':
        return redirect(url_for('admin_bp.dashboard'))
    
    # Obtener órdenes del usuario
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).limit(20).all()
    return render_template('auth/profile.html', orders=orders)
