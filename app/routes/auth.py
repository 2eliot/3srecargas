from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from ..models import db, User, AdminUser, Order
from werkzeug.security import generate_password_hash, check_password_hash

auth_bp = Blueprint('auth_bp', __name__)

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main_bp.index'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()
        phone = request.form.get('phone', '').strip()
        
        # Validaciones
        if not username or not email or not password:
            flash('Todos los campos son obligatorios excepto el teléfono.', 'danger')
            return render_template('auth/register.html')
        
        if password != confirm_password:
            flash('Las contraseñas no coinciden.', 'danger')
            return render_template('auth/register.html')
        
        if len(password) < 6:
            flash('La contraseña debe tener al menos 6 caracteres.', 'danger')
            return render_template('auth/register.html')
        
        # Verificar si ya existe
        if User.query.filter_by(username=username).first():
            flash('El nombre de usuario ya está en uso.', 'danger')
            return render_template('auth/register.html')
        
        if User.query.filter_by(email=email).first():
            flash('El correo electrónico ya está registrado.', 'danger')
            return render_template('auth/register.html')
        
        # Crear usuario
        user = User(
            username=username,
            email=email,
            phone=phone if phone else None
        )
        user.set_password(password)
        
        db.session.add(user)
        db.session.commit()
        
        flash('¡Cuenta creada exitosamente! Ahora puedes iniciar sesión.', 'success')
        return redirect(url_for('auth_bp.login'))
    
    return render_template('auth/register.html')

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        # Redirigir según el tipo de usuario
        if current_user.__class__.__name__ == 'AdminUser':
            return redirect(url_for('admin_bp.dashboard'))
        return redirect(url_for('main_bp.index'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        
        if not username or not password:
            flash('Por favor ingresa usuario y contraseña.', 'danger')
            return render_template('auth/login.html')
        
        # Buscar primero en usuarios normales
        user = User.query.filter(
            (User.username == username) | (User.email == username)
        ).first()
        
        if user and user.check_password(password):
            login_user(user)
            flash('¡Bienvenido de nuevo!', 'success')
            next_page = request.args.get('next')
            return redirect(next_page or url_for('main_bp.index'))
        
        # Si no es usuario normal, buscar en admin
        admin = AdminUser.query.filter_by(username=username).first()
        if admin and admin.check_password(password):
            login_user(admin)
            flash('¡Bienvenido administrador!', 'success')
            return redirect(url_for('admin_bp.dashboard'))
        
        flash('Usuario o contraseña incorrectos.', 'danger')
    
    return render_template('auth/login.html')

@auth_bp.route('/logout')
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
