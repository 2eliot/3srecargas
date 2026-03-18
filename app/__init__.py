import os
from flask import Flask
from flask_login import LoginManager
from sqlalchemy import text
from .models import db, AdminUser, User, Category, Discount, Setting
from config import Config

login_manager = LoginManager()


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'admin_bp.login'
    login_manager.login_message = 'Inicia sesión para acceder al panel.'
    login_manager.login_message_category = 'warning'

    @login_manager.user_loader
    def load_user(user_id):
        # Intentar cargar como User primero, luego como AdminUser
        user = User.query.get(int(user_id))
        if user:
            return user
        return AdminUser.query.get(int(user_id))

    from .routes.main import main_bp
    from .routes.checkout import checkout_bp
    from .routes.admin import admin_bp
    from .routes.affiliates import affiliates_bp
    from .routes.auth import auth_bp
    from .routes.verify import verify_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(checkout_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(affiliates_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(verify_bp)

    @app.context_processor
    def inject_settings():
        site_logo = None
        setting = Setting.query.filter_by(key='site_logo').first()
        if setting and setting.value:
            site_logo = setting.value
        return {'SITE_LOGO': site_logo}

    with app.app_context():
        os.makedirs(app.config.get('DATA_DIR', ''), exist_ok=True)
        db.create_all()
        _ensure_payment_method_columns()
        _ensure_user_columns()
        _ensure_discount_columns()
        _ensure_order_nickname_column()
        _init_default_data(app)

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    return app


def _ensure_payment_method_columns():
    try:
        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(payment_methods)')).fetchall()
        existing = {r[1] for r in rows}
        if 'contact_email' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN contact_email VARCHAR(255)'))
        if 'pay_id' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN pay_id VARCHAR(255)'))
        if 'contact_phone' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN contact_phone VARCHAR(50)'))
        if 'bank_name' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN bank_name VARCHAR(100)'))
        if 'id_number' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN id_number VARCHAR(30)'))
        if 'account_currency' not in existing:
            db.session.execute(text("ALTER TABLE payment_methods ADD COLUMN account_currency VARCHAR(3) DEFAULT 'bs'"))
        if 'show_contact_email' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN show_contact_email BOOLEAN DEFAULT 0'))
        if 'show_pay_id' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN show_pay_id BOOLEAN DEFAULT 0'))
        if 'show_contact_phone' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN show_contact_phone BOOLEAN DEFAULT 0'))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_user_columns():
    try:
        if db.engine.dialect.name != 'sqlite':
            return

        # Agregar columna user_id a la tabla orders si no existe
        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'user_id' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN user_id INTEGER'))
        
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_discount_columns():
    try:
        if db.engine.dialect.name != 'sqlite':
            return

        # Agregar columnas a la tabla orders si no existen
        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'discount_id' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN discount_id INTEGER'))
        if 'original_amount' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN original_amount NUMERIC(10, 2)'))
        if 'discount_amount' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN discount_amount NUMERIC(10, 2) DEFAULT 0'))
        
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_order_nickname_column():
    try:
        if db.engine.dialect.name != 'sqlite':
            return
        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'player_nickname' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN player_nickname VARCHAR(200)'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _init_default_data(app):
    from .models import AdminUser, Category
    import os

    if AdminUser.query.count() == 0:
        admin = AdminUser(
            username=os.environ.get('ADMIN_USERNAME', 'admin'),
            email=os.environ.get('ADMIN_EMAIL', 'admin@3srecargas.com'),
        )
        admin.set_password(os.environ.get('ADMIN_PASSWORD', 'admin123'))
        db.session.add(admin)

    if Category.query.count() == 0:
        for cat in [
            Category(name='Juegos', slug='juegos', icon='🎮'),
            Category(name='Tarjetas', slug='tarjetas', icon='💳'),
            Category(name='Wallet', slug='wallet', icon='👛'),
        ]:
            db.session.add(cat)
    
    # Crear código de descuento de prueba si no existe
    if Discount.query.count() == 0:
        test_discount = Discount(
            code='TEST10',
            description='10% de descuento de prueba',
            discount_type='percentage',
            discount_value=10,
            is_active=True
        )
        db.session.add(test_discount)

    db.session.commit()
