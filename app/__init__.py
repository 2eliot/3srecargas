import os
from flask import Flask
from flask_login import LoginManager
from sqlalchemy import text
from .models import db, AdminUser, User, Category, Discount, Setting
from .utils.timezone import VENEZUELA_TIMEZONE, format_ve
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
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            return None

        user = User.query.get(user_id_int)
        if user:
            return user
        return AdminUser.query.get(user_id_int)

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

    @app.template_filter('datetime_ve')
    def datetime_ve_filter(value, fmt='%d/%m/%Y %H:%M'):
        return format_ve(value, fmt)

    @app.context_processor
    def inject_settings():
        site_logo = None
        setting = Setting.query.filter_by(key='site_logo').first()
        if setting and setting.value:
            site_logo = setting.value

        social_keys = ['social_facebook', 'social_instagram', 'social_tiktok', 'social_whatsapp']
        social_links = {}
        for key in social_keys:
            val_setting = Setting.query.filter_by(key=key).first()
            social_links[key.upper()] = val_setting.value if val_setting and val_setting.value else ''

        return {
            'SITE_LOGO': site_logo,
            'SOCIAL_LINKS': social_links,
            'APP_TIMEZONE': 'GMT-4',
            'APP_TIMEZONE_NAME': 'Venezuela',
            'APP_TIMEZONE_OFFSET': VENEZUELA_TIMEZONE.utcoffset(None),
        }

    with app.app_context():
        os.makedirs(app.config.get('DATA_DIR', ''), exist_ok=True)
        db.create_all()
        _ensure_payment_method_columns()
        _ensure_user_columns()
        _ensure_discount_columns()
        _ensure_order_nickname_column()
        _ensure_affiliate_columns()
        _ensure_payment_verification_columns()
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
        if 'uses_rate' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN uses_rate BOOLEAN DEFAULT 1'))
        if 'pabilo_user_bank_id' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN pabilo_user_bank_id VARCHAR(100)'))
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


def _ensure_affiliate_columns():
    try:
        if db.engine.dialect.name != 'sqlite':
            return
        rows = db.session.execute(text('PRAGMA table_info(affiliates)')).fetchall()
        existing = {r[1] for r in rows}
        if 'client_discount_rate' not in existing:
            db.session.execute(text('ALTER TABLE affiliates ADD COLUMN client_discount_rate NUMERIC(5, 2) DEFAULT 0'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_payment_verification_columns():
    try:
        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'payment_reference_last5' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payment_reference_last5 VARCHAR(5)'))
        if 'payment_amount' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payment_amount NUMERIC(10, 2)'))
        if 'payment_currency' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payment_currency VARCHAR(3)'))
        if 'payer_dni_type' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payer_dni_type VARCHAR(2)'))
        if 'payer_dni_number' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payer_dni_number VARCHAR(20)'))
        if 'payer_bank_origin' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payer_bank_origin VARCHAR(20)'))
        if 'payer_phone' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payer_phone VARCHAR(20)'))
        if 'payer_payment_date' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payer_payment_date DATE'))
        if 'payer_movement_type' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payer_movement_type VARCHAR(20)'))
        if 'payment_verification_id' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payment_verification_id VARCHAR(100)'))
        if 'payment_verified_at' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payment_verified_at DATETIME'))
        if 'payment_verification_attempts' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payment_verification_attempts INTEGER DEFAULT 0'))
        if 'payment_last_verification_at' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payment_last_verification_at DATETIME'))

        db.session.commit()
    except Exception:
        db.session.rollback()


def _init_default_data(app):
    from .models import AdminUser, Category
    import os

    if AdminUser.query.count() == 0:
        admin_username = (os.environ.get('ADMIN_USERNAME') or '').strip()
        admin_password = (os.environ.get('ADMIN_PASSWORD') or '').strip()
        admin_email = (os.environ.get('ADMIN_EMAIL') or '').strip()

        if not admin_username or not admin_password:
            raise RuntimeError(
                'No existe usuario admin en base de datos y faltan variables de entorno '
                'ADMIN_USERNAME/ADMIN_PASSWORD para crearlo de forma segura.'
            )

        admin = AdminUser(
            username=admin_username,
            email=admin_email or f'{admin_username}@localhost',
        )
        admin.set_password(admin_password)
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
