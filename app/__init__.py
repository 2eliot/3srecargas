import os
import sqlite3
from flask import Flask, url_for
from flask_login import current_user
from flask_login import LoginManager
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from .models import db, AdminUser, User, Category, Discount, Setting
from .utils.timezone import VENEZUELA_TIMEZONE, format_ve
from config import Config

login_manager = LoginManager()


def _ensure_postgres_columns(table_name, column_defs):
    if db.engine.dialect.name != 'postgresql':
        return False

    for column_def in column_defs:
        db.session.execute(text(f'ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {column_def}'))
    db.session.commit()
    return True


def _configure_sqlite_concurrency(app):
    """Prepara SQLite para varios workers escribiendo a la vez.

    Con `gunicorn -w 3` más el scheduler en segundo plano, las escrituras
    coinciden y SQLite en modo rollback-journal bloquea toda la base durante
    cada una. En producción eso se veía como `OperationalError: database is
    locked` justo al aprobar órdenes — el peor momento posible, porque un
    commit perdido después de una recarga real deja la orden pendiente
    aunque el proveedor ya haya entregado.

    WAL permite que los lectores no bloqueen al escritor, y `busy_timeout`
    hace que un escritor espere su turno en vez de abortar al instante.
    """
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if not uri.startswith('sqlite'):
        return

    options = dict(app.config.get('SQLALCHEMY_ENGINE_OPTIONS') or {})
    connect_args = dict(options.get('connect_args') or {})
    connect_args.setdefault('timeout', 30)
    options['connect_args'] = connect_args
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = options

    @event.listens_for(Engine, 'connect')
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute('PRAGMA journal_mode=WAL')
            cursor.execute('PRAGMA busy_timeout=30000')
            cursor.execute('PRAGMA synchronous=NORMAL')
        finally:
            cursor.close()


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    _configure_sqlite_concurrency(app)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth_bp.login'
    login_manager.login_message = 'Inicia sesión para continuar.'
    login_manager.login_message_category = 'warning'

    @login_manager.user_loader
    def load_user(user_id):
        raw_id = str(user_id or '').strip()
        if not raw_id:
            return None

        if ':' in raw_id:
            user_type, _, user_pk = raw_id.partition(':')
            try:
                user_pk = int(user_pk)
            except (TypeError, ValueError):
                return None
            if user_type == 'user':
                return User.query.get(user_pk)
            if user_type == 'admin':
                return AdminUser.query.get(user_pk)
            return None

        try:
            user_id_int = int(raw_id)
        except (TypeError, ValueError):
            return None
        user = User.query.get(user_id_int)
        if user:
            return user
        return AdminUser.query.get(user_id_int)

    @login_manager.unauthorized_handler
    def handle_unauthorized():
        from flask import redirect, request, url_for

        if request.path.startswith('/admin'):
            return redirect(url_for('admin_bp.login', next=request.url))
        return redirect(url_for('auth_bp.login', next=request.url))

    from .routes.main import main_bp, archive_previous_month_rankings_if_needed, has_visible_public_rankings
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

    # Versión de cachébuster por archivo: el mtime es igual en los 3 workers
    # de gunicorn y solo cambia al desplegar, así el navegador sí puede
    # cachear el CSS/JS entre visitas (antes era un número aleatorio por
    # request y se re-descargaba todo siempre).
    static_version_cache = {}

    @app.template_global('static_v')
    def static_v(filename):
        version = static_version_cache.get(filename)
        if version is None:
            try:
                version = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
            except OSError:
                version = 0
            static_version_cache[filename] = version
        return f"{url_for('static', filename=filename)}?v={version}"

    @app.context_processor
    def inject_settings():
        # Corre en cada página: una sola consulta para todos los settings
        # en vez de una por clave.
        social_keys = ['social_facebook', 'social_instagram', 'social_tiktok', 'social_whatsapp']
        ranking_keys = [
            'ranking_free_fire_enabled',
            'ranking_blood_strike_enabled',
            'ranking_free_fire_game_id',
            'ranking_blood_strike_game_id',
        ]
        wanted_keys = (
            ['site_logo', 'site_background_image', 'support_email', 'support_whatsapp', 'support_schedule', 'support_location']
            + social_keys
            + ranking_keys
        )
        values = {
            row.key: row.value
            for row in Setting.query.filter(Setting.key.in_(wanted_keys)).all()
        }

        site_logo = values.get('site_logo') or None
        site_background_image = values.get('site_background_image') or None
        social_links = {key.upper(): values.get(key) or '' for key in social_keys}
        support_email = values.get('support_email') or '3srecargas@gmail.com'
        support_whatsapp_url = values.get('support_whatsapp') or 'https://wa.me/19543789224'
        support_schedule = values.get('support_schedule') or 'Lunes a Domingo 10:00 AM a 8:00 PM'
        support_location = values.get('support_location') or 'Puerto Ordaz, Bolívar, Venezuela'
        ranking_settings = {key: values.get(key) or '' for key in ranking_keys}

        has_active_ranking = has_visible_public_rankings()

        return {
            'SITE_LOGO': site_logo,
            'SITE_BACKGROUND_IMAGE': site_background_image,
            'SOCIAL_LINKS': social_links,
            'SUPPORT_EMAIL': support_email,
            'SUPPORT_WHATSAPP_URL': support_whatsapp_url,
            'SUPPORT_SCHEDULE': support_schedule,
            'SUPPORT_LOCATION': support_location,
            'RANKING_SETTINGS': ranking_settings,
            'HAS_ACTIVE_RANKING': has_active_ranking,
            'APP_TIMEZONE': 'GMT-4',
            'APP_TIMEZONE_NAME': 'Venezuela',
            'APP_TIMEZONE_OFFSET': VENEZUELA_TIMEZONE.utcoffset(None),
        }

    @app.cli.command('archive-rankings-month')
    def archive_rankings_month_command():
        archive_previous_month_rankings_if_needed()
        print('Ranking mensual archivado/verificado correctamente.')

    with app.app_context():
        os.makedirs(app.config.get('DATA_DIR', ''), exist_ok=True)
        db.create_all()
        _ensure_payment_method_columns()
        _ensure_user_columns()
        _ensure_discount_columns()
        _ensure_order_nickname_column()
        _ensure_order_delivery_proof_column()
        _ensure_game_columns()
        _ensure_package_pricing_columns()
        _ensure_package_announcement_column()
        _ensure_minigame_opportunity_columns()
        _ensure_points_columns()
        _ensure_affiliate_columns()
        _ensure_one_per_player_columns()
        _ensure_payment_verification_columns()
        _ensure_ai_reference_columns()
        _ensure_order_idempotency_column()
        _ensure_ranking_archive_columns()
        _ensure_revendedores_mapping_columns()
        _ensure_binance_columns()
        _init_default_data(app)

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    from .utils.order_scheduler import start_order_recovery_scheduler
    start_order_recovery_scheduler(app)

    return app


def _ensure_payment_method_columns():
    try:
        if _ensure_postgres_columns('payment_methods', [
            'contact_email VARCHAR(255)',
            'pay_id VARCHAR(255)',
            'contact_phone VARCHAR(50)',
            'bank_name VARCHAR(100)',
            'id_number VARCHAR(30)',
            "account_currency VARCHAR(3) DEFAULT 'bs'",
            'show_contact_email BOOLEAN DEFAULT FALSE',
            'show_pay_id BOOLEAN DEFAULT FALSE',
            'show_contact_phone BOOLEAN DEFAULT FALSE',
            'uses_rate BOOLEAN DEFAULT TRUE',
            'pabilo_user_bank_id VARCHAR(100)',
            'pabilo_requires_phone_dni BOOLEAN DEFAULT FALSE',
            'tutorial_video VARCHAR(255)',
        ]):
            return

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
        if 'pabilo_requires_phone_dni' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN pabilo_requires_phone_dni BOOLEAN DEFAULT 0'))
        if 'tutorial_video' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN tutorial_video VARCHAR(255)'))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_user_columns():
    try:
        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'user_id' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN user_id INTEGER'))

        user_rows = db.session.execute(text('PRAGMA table_info(users)')).fetchall()
        user_existing = {r[1] for r in user_rows}
        if 'account_scope' not in user_existing:
            db.session.execute(text('ALTER TABLE users ADD COLUMN account_scope VARCHAR(120)'))
        if 'account_scope_label' not in user_existing:
            db.session.execute(text('ALTER TABLE users ADD COLUMN account_scope_label VARCHAR(120)'))
        if 'account_identifier' not in user_existing:
            db.session.execute(text('ALTER TABLE users ADD COLUMN account_identifier VARCHAR(255)'))
        if 'account_identifier_normalized' not in user_existing:
            db.session.execute(text('ALTER TABLE users ADD COLUMN account_identifier_normalized VARCHAR(255)'))
        if 'account_kind' not in user_existing:
            db.session.execute(text("ALTER TABLE users ADD COLUMN account_kind VARCHAR(50) DEFAULT 'player_id'"))
        if 'contact_email' not in user_existing:
            db.session.execute(text('ALTER TABLE users ADD COLUMN contact_email VARCHAR(255)'))

        db.session.execute(
            text('CREATE UNIQUE INDEX IF NOT EXISTS uq_users_scope_identifier ON users(account_scope, account_identifier_normalized)')
        )
        
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_discount_columns():
    try:
        if _ensure_postgres_columns('orders', [
            'discount_id INTEGER',
            'original_amount NUMERIC(10, 2)',
            'discount_amount NUMERIC(10, 2) DEFAULT 0',
        ]):
            return

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
        if _ensure_postgres_columns('orders', [
            'player_nickname VARCHAR(200)',
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return
        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'player_nickname' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN player_nickname VARCHAR(200)'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_order_delivery_proof_column():
    try:
        if _ensure_postgres_columns('orders', [
            'delivery_proof VARCHAR(255)',
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return
        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'delivery_proof' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN delivery_proof VARCHAR(255)'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_package_pricing_columns():
    try:
        if _ensure_postgres_columns('packages', [
            'usd_price NUMERIC(10, 2)',
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(packages)')).fetchall()
        existing = {r[1] for r in rows}
        if 'usd_price' not in existing:
            db.session.execute(text('ALTER TABLE packages ADD COLUMN usd_price NUMERIC(10, 2)'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_package_announcement_column():
    try:
        if _ensure_postgres_columns('packages', ['announcement_type VARCHAR(30)']):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(packages)')).fetchall()
        existing = {r[1] for r in rows}
        if 'announcement_type' not in existing:
            db.session.execute(text('ALTER TABLE packages ADD COLUMN announcement_type VARCHAR(30)'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_minigame_opportunity_columns():
    try:
        if _ensure_postgres_columns('order_minigame_opportunities', ['prize_order_id INTEGER']):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(order_minigame_opportunities)')).fetchall()
        existing = {r[1] for r in rows}
        if 'prize_order_id' not in existing:
            db.session.execute(text('ALTER TABLE order_minigame_opportunities ADD COLUMN prize_order_id INTEGER'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_points_columns():
    try:
        if _ensure_postgres_columns('orders', ['points_awarded BOOLEAN DEFAULT FALSE']):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'points_awarded' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN points_awarded BOOLEAN DEFAULT 0'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_game_columns():
    try:
        if _ensure_postgres_columns('games', [
            'show_selection_popup BOOLEAN DEFAULT FALSE',
            "player_id_input_type VARCHAR(20) DEFAULT 'numeric'",
            'bs_rate_override NUMERIC(10, 4)',
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(games)')).fetchall()
        existing = {r[1] for r in rows}
        if 'show_selection_popup' not in existing:
            db.session.execute(text('ALTER TABLE games ADD COLUMN show_selection_popup BOOLEAN DEFAULT 0'))
        if 'player_id_input_type' not in existing:
            db.session.execute(text("ALTER TABLE games ADD COLUMN player_id_input_type VARCHAR(20) DEFAULT 'numeric'"))
        if 'bs_rate_override' not in existing:
            db.session.execute(text('ALTER TABLE games ADD COLUMN bs_rate_override NUMERIC(10, 4)'))
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


def _ensure_one_per_player_columns():
    """Marca de 'solo 1 uso por ID' para códigos de descuento y de afiliado,
    pensada para regalarlos a usuarios nuevos sin que se puedan reutilizar
    en la misma cuenta/ID de juego una vez usados."""
    try:
        if _ensure_postgres_columns('discounts', ['one_per_player BOOLEAN DEFAULT FALSE']):
            pass
        elif db.engine.dialect.name == 'sqlite':
            rows = db.session.execute(text('PRAGMA table_info(discounts)')).fetchall()
            existing = {r[1] for r in rows}
            if 'one_per_player' not in existing:
                db.session.execute(text('ALTER TABLE discounts ADD COLUMN one_per_player BOOLEAN DEFAULT 0'))
                db.session.commit()

        if _ensure_postgres_columns('affiliates', ['one_per_player BOOLEAN DEFAULT FALSE']):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(affiliates)')).fetchall()
        existing = {r[1] for r in rows}
        if 'one_per_player' not in existing:
            db.session.execute(text('ALTER TABLE affiliates ADD COLUMN one_per_player BOOLEAN DEFAULT 0'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_payment_verification_columns():
    try:
        if _ensure_postgres_columns('orders', [
            'payment_reference_last5 VARCHAR(6)',
            'payment_amount NUMERIC(10, 2)',
            'payment_currency VARCHAR(3)',
            'payer_dni_type VARCHAR(2)',
            'payer_dni_number VARCHAR(20)',
            'payer_bank_origin VARCHAR(20)',
            'payer_phone VARCHAR(20)',
            'payer_payment_date DATE',
            'payer_movement_type VARCHAR(20)',
            'payment_verification_id VARCHAR(100)',
            'payment_verified_at TIMESTAMP',
            'payment_verification_attempts INTEGER DEFAULT 0',
            'payment_last_verification_at TIMESTAMP',
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'payment_reference_last5' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payment_reference_last5 VARCHAR(6)'))
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


def _ensure_ai_reference_columns():
    try:
        if _ensure_postgres_columns('orders', [
            'ai_extracted_reference VARCHAR(255)',
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'ai_extracted_reference' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN ai_extracted_reference VARCHAR(255)'))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_order_idempotency_column():
    try:
        if _ensure_postgres_columns('orders', [
            'idempotency_key VARCHAR(64)',
        ]):
            db.session.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_idempotency_key ON orders(idempotency_key)'))
            db.session.commit()
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'idempotency_key' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN idempotency_key VARCHAR(64)'))

        db.session.execute(
            text('CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_idempotency_key ON orders(idempotency_key)')
        )
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_ranking_archive_columns():
    try:
        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(ranking_archives)')).fetchall()
        existing = {r[1] for r in rows}
        if 'player_id' not in existing:
            db.session.execute(text('ALTER TABLE ranking_archives ADD COLUMN player_id VARCHAR(100)'))
        if 'nickname' not in existing:
            db.session.execute(text('ALTER TABLE ranking_archives ADD COLUMN nickname VARCHAR(200)'))

        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_binance_columns():
    """Columna que ata una transacción de Binance Pay a la orden que saldó.

    El índice único es la garantía de que una misma transferencia no se
    acredite a dos órdenes distintas — imprescindible ahora que un pago sin
    memo puede emparejarse por monto.
    """
    try:
        if _ensure_postgres_columns('orders', [
            'binance_tx_id VARCHAR(120)',
        ]):
            db.session.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_binance_tx_id ON orders(binance_tx_id)'))
            db.session.commit()
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'binance_tx_id' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN binance_tx_id VARCHAR(120)'))

        db.session.execute(
            text('CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_binance_tx_id ON orders(binance_tx_id)')
        )
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_revendedores_mapping_columns():
    try:
        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(revendedores_item_mappings)')).fetchall()
        existing = {r[1] for r in rows}
        if 'catalog_item_id_2' not in existing:
            db.session.execute(text('ALTER TABLE revendedores_item_mappings ADD COLUMN catalog_item_id_2 INTEGER'))

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
