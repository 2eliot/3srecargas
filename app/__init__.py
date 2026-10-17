import json
import os
import re
import secrets
import sqlite3
from flask import Flask, Request, url_for
from markupsafe import Markup, escape
from flask_login import current_user
from flask_login import LoginManager
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from .models import db, AdminUser, User, Category, Discount, Setting, Affiliate
from .utils.timezone import VENEZUELA_TIMEZONE, format_ve, today_ve_str
from .utils.catalog_version import register_catalog_version_hook, site_version
from .utils.promos import (
    get_accumulated_enabled_games, get_raffle_enabled_games, get_guess_enabled_games,
    get_horde_enabled_games,
)
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


def _secret_key_is_weak(key):
    key = (key or '').strip()
    lowered = key.lower()
    return (
        len(key) < 32
        or lowered.startswith('dev-')
        or 'change' in lowered
        or 'secret-key' in lowered
        # Las llaves de ejemplo en español (.env.example y README) miden justo 32 y pasaban la revisión de largo.
        or any(marker in lowered for marker in ('cambia', 'clave-secreta', 'clave-segura', 'tu-clave', 'ejemplo'))
    )


def _ensure_secure_secret_key(app):
    """Con una llave de ejemplo o corta (la de ejemplo es pública en el
    repo) cualquiera puede firmar una cookie de sesión de admin. En ese
    caso se usa una llave aleatoria guardada en DATA_DIR, creada una sola
    vez y compartida por todos los workers."""
    if not _secret_key_is_weak(app.config.get('SECRET_KEY')):
        return

    data_dir = app.config.get('DATA_DIR') or os.path.join(app.root_path, '..', 'data')
    os.makedirs(data_dir, exist_ok=True)
    key_path = os.path.join(data_dir, 'secret_key')
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    with open(key_path) as handle:
        app.config['SECRET_KEY'] = handle.read().strip()


class _JsonObjectRequest(Request):
    """Toda la app espera que un cuerpo JSON sea un OBJETO ({...}). Si llega un texto suelto, un número o una lista,
    get_json(silent=True) devuelve None (y el código ya hace `... or {}`): antes eso rompía unas 40 rutas con error 500."""

    def get_json(self, force=False, silent=False, cache=True):
        data = super().get_json(force=force, silent=silent, cache=cache)
        if silent and data is not None and not isinstance(data, dict):
            return None
        return data


def create_app(config_class=Config):
    app = Flask(__name__)
    app.request_class = _JsonObjectRequest
    app.config.from_object(config_class)
    _ensure_secure_secret_key(app)
    # SameSite=Lax: el navegador no manda la sesión en formularios POST que
    # vengan de otra página, así una web ajena no puede aprobar órdenes ni
    # tocar el panel a nombre del admin (CSRF).
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
    app.config['REMEMBER_COOKIE_HTTPONLY'] = True
    # "Recordarme" de los CLIENTES (cuenta, ID, Google): la sesión no se cierra al cerrar
    # el navegador, dura un año. El admin entra SIN recordar (su sesión sigue cerrándose
    # al cerrar el navegador), por eso aquí no se activa REMEMBER_COOKIE_REFRESH_EACH_REQUEST
    # (renovaría la cookie de todos los que estén dentro, admin incluido).
    from datetime import timedelta as _timedelta
    app.config['REMEMBER_COOKIE_DURATION'] = _timedelta(days=365)
    # Secure: el navegador solo manda la cookie por HTTPS, nunca por http://,
    # así no se puede interceptar en una red abierta. Se activa solo en
    # producción (detectada por usar Postgres) y se deja apagado en local
    # (SQLite, http://localhost) para no romper el acceso al panel al probar.
    _is_production = str(app.config.get('SQLALCHEMY_DATABASE_URI', '')).startswith('postgres')
    # En un VPS con SQLite lo de arriba da False aunque el sitio se sirva por
    # HTTPS, y la cookie de sesión (también la del admin) viajaba sin la
    # marca Secure. SESSION_COOKIE_SECURE=1 en el .env lo fuerza.
    _secure_env = (os.environ.get('SESSION_COOKIE_SECURE') or '').strip().lower()
    if _secure_env in {'1', 'true', 'yes', 'on'}:
        _is_production = True
    elif _secure_env in {'0', 'false', 'no', 'off'}:
        _is_production = False
    app.config['SESSION_COOKIE_SECURE'] = _is_production
    app.config['REMEMBER_COOKIE_SECURE'] = _is_production

    _configure_sqlite_concurrency(app)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth_bp.login'
    login_manager.login_message = 'Inicia sesión para continuar.'
    login_manager.login_message_category = 'warning'

    def _only_if_enabled(user):
        # Una cuenta que el admin desactivó pierde su sesión (antes solo se le negaba entrar de nuevo,
        # y con sesiones que duran un año eso sería casi para siempre).
        if user is not None and user.is_active is False:
            return None
        return user

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
                return _only_if_enabled(User.query.get(user_pk))
            if user_type == 'admin':
                return AdminUser.query.get(user_pk)
            if user_type == 'mini':
                return Affiliate.query.get(user_pk)
            return None

        try:
            user_id_int = int(raw_id)
        except (TypeError, ValueError):
            return None
        user = User.query.get(user_id_int)
        if user:
            return _only_if_enabled(user)
        return AdminUser.query.get(user_id_int)

    @login_manager.unauthorized_handler
    def handle_unauthorized():
        from flask import redirect, request, url_for

        if request.path.startswith('/admin'):
            return redirect(url_for('admin_bp.login', next=request.url))
        if request.path.startswith('/minis'):
            return redirect(url_for('minis_bp.login', next=request.url))
        return redirect(url_for('auth_bp.login', next=request.url))

    from .routes.main import main_bp, archive_previous_month_rankings_if_needed, has_visible_public_rankings
    from .routes.checkout import checkout_bp
    from .routes.admin import admin_bp
    from .routes.affiliates import affiliates_bp
    from .routes.auth import auth_bp
    from .routes.profiles import profiles_bp
    from .routes.verify import verify_bp
    from .routes.redeem import redeem_bp
    from .routes.minis import minis_bp
    from .routes.revendedores_webhook import revendedores_webhook_bp
    from .routes.support import support_bp
    from .routes.admin_support import admin_support_bp
    from .routes.admin_tournaments import admin_tournaments_bp
    from .routes.promos import promos_bp
    from .routes.points_shop import points_shop_bp
    from .routes.admin_points_shop import admin_points_shop_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(checkout_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(affiliates_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(profiles_bp)
    app.register_blueprint(verify_bp)
    app.register_blueprint(redeem_bp)
    app.register_blueprint(minis_bp, url_prefix='/minis')
    app.register_blueprint(revendedores_webhook_bp)
    app.register_blueprint(support_bp)
    app.register_blueprint(admin_support_bp)
    app.register_blueprint(admin_tournaments_bp)
    app.register_blueprint(promos_bp)
    app.register_blueprint(points_shop_bp)
    app.register_blueprint(admin_points_shop_bp)

    @app.template_filter('datetime_ve')
    def datetime_ve_filter(value, fmt='%d/%m/%Y %H:%M'):
        return format_ve(value, fmt)

    @app.template_filter('jsattr')
    def jsattr_filter(value):
        """Texto -> literal de JavaScript ("...") seguro DENTRO de un onclick="..." / onsubmit="...".

        `'{{ nombre }}'` NO es seguro en un atributo: el navegador convierte &#39; otra vez en ' antes de ejecutar el JS, así que un
        nombre con comillas rompe el texto y ejecuta código. Esto lo escribe como JSON (comillas y símbolos con \\u) y luego lo escapa
        para HTML. Uso: onclick="abrir({{ nombre|jsattr }})" (sin poner comillas alrededor).
        """
        return Markup(escape(json.dumps('' if value is None else str(value), ensure_ascii=True)))

    @app.template_filter('safe_href')
    def safe_href_filter(value):
        """Enlace escrito por una persona -> solo http(s), mailto, tel o ruta del sitio; si no, "#" (evita javascript:/data:)."""
        raw = str(value or '').strip()
        probe = re.sub(r'[\x00-\x20\x7f]+', '', raw)
        if not probe:
            return ''
        if probe.startswith('/') and not probe.startswith('//') and not probe.startswith('/\\'):
            return raw
        if probe.startswith('#'):
            return raw
        scheme = re.match(r'^([a-zA-Z][a-zA-Z0-9+.\-]*):', probe)
        if scheme:
            return raw if scheme.group(1).lower() in ('http', 'https', 'mailto', 'tel') else '#'
        if probe.startswith('//') or probe.startswith('\\'):
            return '#'
        return 'https://' + raw

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

    app.add_template_global(site_version, 'app_version')
    app.add_template_global(today_ve_str, 'page_day_ve')
    register_catalog_version_hook()

    # Prefijos que siguen andando aunque la tienda esté en mantenimiento: el
    # panel admin (para poder apagarlo de nuevo y seguir trabajando), los
    # estáticos (los necesita hasta la propia página de mantenimiento) y el
    # webhook de Revendedores (una recarga en curso no debería romperse
    # solo porque alguien está editando la tienda al mismo tiempo).
    MAINTENANCE_ALLOWED_PREFIXES = ('/admin', '/static/', '/api/revendedores/webhook', '/favicon.ico')

    @app.before_request
    def _check_maintenance_mode():
        from flask import render_template, request

        if request.path.startswith(MAINTENANCE_ALLOWED_PREFIXES):
            return None

        setting = Setting.query.filter_by(key='maintenance_mode').first()
        if setting and setting.value == 'true':
            return render_template('maintenance.html'), 503
        return None

    # Un ID de jugador (o zona, o el dato con el que se entra) NUNCA lleva comillas, < >, barra invertida ni caracteres invisibles:
    # si llegan, es un intento de meter código. Se frena aquí, una sola vez, para todas las rutas públicas (checkout, canje, sorteo,
    # Hordas, adivina, puntos, perfiles...). El panel admin y el webhook del proveedor quedan fuera.
    HOSTILE_ID = re.compile('[<>"\'`\\\\\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]')
    GUARDED_ID_FIELDS = ('player_id', 'lookup_identifier', 'identifier', 'zone_id', 'link_identifier')

    # Formularios normales de la cuenta (no fetch): ahí el aviso sale como mensaje en la misma página, no como JSON crudo.
    FORM_PREFIXES = ('/profile/', '/login', '/register', '/account/')

    @app.before_request
    def _reject_hostile_ids():
        from urllib.parse import urlparse
        from flask import flash, jsonify, redirect, request

        if request.path.startswith(('/admin', '/static/', '/api/revendedores/webhook')):
            return None
        sources = [request.args]
        if request.method in ('POST', 'PUT', 'PATCH'):
            body = request.get_json(silent=True) if request.is_json else None
            sources.append(body if isinstance(body, dict) else request.form)
        for source in sources:
            for field in GUARDED_ID_FIELDS:
                value = source.get(field)
                if not isinstance(value, str):
                    continue
                hostile = bool(HOSTILE_ID.search(value))
                if not hostile and len(value) <= 150:
                    continue
                message = 'El ID tiene caracteres que no se permiten.' if hostile else 'Ese ID no es válido.'
                is_plain_form = (
                    request.method == 'POST' and request.path.startswith(FORM_PREFIXES) and not request.is_json
                    and not request.headers.get('X-Requested-With') and request.form.get('ajax') != '1'
                )
                if is_plain_form:
                    flash(message, 'danger')
                    back = request.referrer or ''
                    target = urlparse(back)
                    if target.netloc and target.netloc != request.host:
                        back = ''
                    return redirect(back or ('/account' if request.path.startswith('/profile/') else '/'))
                return jsonify({'ok': False, 'message': message, 'error': message}), 400
        return None

    @app.errorhandler(413)
    def _request_too_large(exc):
        """Subir varias imágenes grandes a la vez pasa del tope de 16 MB de una petición: antes salía una página en inglés
        ("413 Request Entity Too Large") y se perdía lo editado. Ahora un aviso claro, y en los formularios se vuelve a la página."""
        from urllib.parse import urlparse
        from flask import flash, jsonify, redirect, request

        message = 'Los archivos juntos pesan demasiado (máximo 16 MB en total). Sube menos fotos a la vez o usa imágenes más livianas.'
        is_plain_form = (
            request.method == 'POST' and request.path.startswith(FORM_PREFIXES + ('/admin',)) and not request.is_json
            and not request.headers.get('X-Requested-With') and 'application/json' not in (request.headers.get('Accept') or '')
        )
        if is_plain_form:
            flash(message, 'danger')
            back = request.referrer or ''
            target = urlparse(back)
            if target.netloc and target.netloc != request.host:
                back = ''
            return redirect(back or '/')
        return jsonify({'ok': False, 'message': message, 'error': message}), 413

    @app.errorhandler(OverflowError)
    def _number_too_big(exc):
        """Un número gigante (un ID de 25 cifras) no cabe en la base de datos: es una petición mala, no un fallo del servidor."""
        from flask import jsonify, request

        message = 'Ese número es demasiado grande.'
        if request.path.startswith('/admin') and not request.is_json:
            return message, 400
        return jsonify({'ok': False, 'message': message, 'error': message}), 400

    # Compresión gzip + caché larga de estáticos (se registra ANTES que _no_cachear_html: Flask corre los after_request en orden
    # inverso, así que ésta corre al final y comprime ya con todas las cabeceras puestas). Ver utils/web_perf.py.
    from .utils.web_perf import init_web_perf
    init_web_perf(app)

    @app.after_request
    def _no_cachear_html(response):
        """El HTML nunca se guarda en caché.

        El CSS/JS ya se cachea bien (van con ?v=mtime), pero si el navegador
        se queda con el HTML viejo apunta a los archivos viejos y el cliente
        sigue viendo la tienda de la semana pasada. Con `no-cache` no
        bastaba: Chrome y Safari, al restaurar una pestaña descartada,
        reutilizan la copia de disco sin revalidar aunque sea vieja (en los
        logs aparecían páginas de semanas atrás pidiendo su CSS). Solo
        `no-store` evita que la guarden. Solo toca text/html: los estáticos
        y las respuestas JSON conservan sus propias cabeceras.
        """
        if response.mimetype == 'text/html':
            response.headers.setdefault('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            # Nadie puede meter la tienda dentro de otra página (clickjacking), ni cambiar la base de los enlaces, ni cargar plugins.
            # (El chat del admin se carga dentro del propio admin: mismo sitio, sigue permitido.)
            response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
            response.headers.setdefault('Content-Security-Policy', "base-uri 'self'; object-src 'none'; frame-ancestors 'self'")
            response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        # Páginas y respuestas JSON: el navegador no debe "adivinar" que un texto es HTML o un script.
        if response.mimetype in ('text/html', 'application/json'):
            response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        return response

    @app.context_processor
    def inject_admin_return():
        """ADMIN_RETURN = el administrador está dentro del perfil oficial y puede volver a su panel (muestra la barra "Volver al panel de admin")."""
        from flask import session as _session
        if 'admin_return' not in _session:
            return {'ADMIN_RETURN': False}
        from flask_login import current_user as _current
        from .utils.official_profile import admin_return_user
        return {'ADMIN_RETURN': admin_return_user(_current) is not None}

    @app.template_global('is_official_user')
    def is_official_user_global(user):
        """True si `user` es la cuenta oficial del administrador (lleva el verificado y la etiqueta ADMIN)."""
        from .utils.official_profile import is_official_user
        return is_official_user(user)

    @app.template_global('bg_video_layer')
    def bg_video_layer(path):
        """Capa fija con el VIDEO de fondo de una página (ver templates/_bg_video.html). Solo rutas de nuestras subidas, con forma de nombre de archivo MP4/WEBM."""
        from flask import render_template as _render
        path = (path or '').strip()
        if not path or '..' in path or not re.match(r'^[\w./-]+\.(mp4|webm)$', path, re.I):
            return ''
        return Markup(_render('_bg_video.html', path=path))

    @app.context_processor
    def inject_settings():
        # Corre en cada página: una sola consulta para todos los settings
        # en vez de una por clave.
        social_keys = ['social_facebook', 'social_instagram', 'social_tiktok', 'social_whatsapp']
        drawer_bg_keys = [
            'drawer_bg_recarga', 'drawer_bg_sorteo', 'drawer_bg_adivina', 'drawer_bg_hordas', 'drawer_bg_torneos',
            'drawer_bg_canjear', 'drawer_bg_mini', 'drawer_bg_login',
        ]
        wanted_keys = (
            ['site_logo', 'support_avatar', 'site_background_image', 'support_email', 'support_whatsapp', 'support_schedule', 'support_location']
            + ['site_tutorial_video_file', 'site_tutorial_video_title']
            + social_keys
            + drawer_bg_keys
        )
        values = {
            row.key: row.value
            for row in Setting.query.filter(Setting.key.in_(wanted_keys)).all()
        }

        site_logo = values.get('site_logo') or None
        support_avatar = values.get('support_avatar') or None
        site_background_image = values.get('site_background_image') or None
        social_links = {key.upper(): values.get(key) or '' for key in social_keys}
        support_email = values.get('support_email') or '3srecargas@gmail.com'
        support_whatsapp_url = values.get('support_whatsapp') or 'https://wa.me/19543789224'
        support_schedule = values.get('support_schedule') or 'Lunes a Domingo 10:00 AM a 8:00 PM'
        support_location = values.get('support_location') or 'Puerto Ordaz, Bolívar, Venezuela'
        site_tutorial_video_file = values.get('site_tutorial_video_file') or None
        site_tutorial_video_title = values.get('site_tutorial_video_title') or 'Tutorial'
        # Cada fondo puede ser una imagen o un VIDEO corto (MP4/WEBM, 1 MB): las plantillas que ya dibujaban la imagen siguen igual (solo reciben imágenes) y
        # el video se dibuja aparte con bg_video_layer(...). PAGE_BG_FB = el fondo propio de la página y, si no tiene, el de toda la web.
        from .utils.page_bg import split_bg
        site_img, site_vid = split_bg(site_background_image)
        own_bg = {key: split_bg(values.get(key)) for key in drawer_bg_keys}
        drawer_bg_images = {key: own_bg[key][0] or None for key in drawer_bg_keys}
        drawer_bg_videos = {key: own_bg[key][1] or None for key in drawer_bg_keys}
        page_bg_fb = {
            key: ({'image': own_bg[key][0], 'video': own_bg[key][1]} if (own_bg[key][0] or own_bg[key][1]) else {'image': site_img, 'video': site_vid})
            for key in drawer_bg_keys
        }

        has_active_ranking = has_visible_public_rankings()

        # El menú lateral (base.html) solo muestra el link de cada promo si
        # todavía queda al menos un juego con esa promo activa — si el admin
        # apaga el Sorteo Diario en el último juego que lo tenía, el link
        # desaparece solo, sin dejar una página vacía enlazada.
        promo_availability = {
            'recarga_acumulada': bool(get_accumulated_enabled_games()),
            'sorteo': bool(get_raffle_enabled_games()),
            'adivina': bool(get_guess_enabled_games()),
            'hordas': bool(get_horde_enabled_games()),
            'torneos': True,   # la página existe siempre (por ahora "muy pronto"); cuando tenga reglas se podrá apagar desde el admin
        }

        return {
            'SITE_LOGO': site_logo,
            'SUPPORT_AVATAR': support_avatar,
            'SITE_BACKGROUND_IMAGE': site_img or None,
            'SITE_BACKGROUND_VIDEO': site_vid or None,
            'SOCIAL_LINKS': social_links,
            'SUPPORT_EMAIL': support_email,
            'SUPPORT_WHATSAPP_URL': support_whatsapp_url,
            'SUPPORT_SCHEDULE': support_schedule,
            'SUPPORT_LOCATION': support_location,
            'SITE_TUTORIAL_VIDEO_FILE': site_tutorial_video_file,
            'SITE_TUTORIAL_VIDEO_TITLE': site_tutorial_video_title,
            'DRAWER_BG_IMAGES': drawer_bg_images,
            'DRAWER_BG_VIDEOS': drawer_bg_videos,
            'PAGE_BG_FB': page_bg_fb,
            'HAS_ACTIVE_RANKING': has_active_ranking,
            'PROMO_AVAILABILITY': promo_availability,
            'APP_TIMEZONE': 'GMT-4',
            'APP_TIMEZONE_NAME': 'Venezuela',
            'APP_TIMEZONE_OFFSET': VENEZUELA_TIMEZONE.utcoffset(None),
        }

    @app.cli.command('purge-support-chats')
    def purge_support_chats_command():
        """Borra chats de soporte cerrados hace más de 90 días."""
        from .utils.support import purge_old_chats
        removed = purge_old_chats()
        print(f'Chats de soporte purgados: {removed}')

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
        _ensure_order_account_user_column()
        _ensure_game_columns()
        _ensure_package_pricing_columns()
        _ensure_package_announcement_column()
        _ensure_package_points_column()
        _ensure_package_support_fallback_column()
        _ensure_package_bs_price_column()
        _ensure_package_cost_column()
        _ensure_package_section_column()
        _ensure_horde_columns()
        _ensure_minigame_opportunity_columns()
        _ensure_points_columns()
        _ensure_affiliate_columns()
        _ensure_mini_influencer_columns()
        _ensure_mini_withdrawal_columns()
        _ensure_one_per_player_columns()
        _ensure_payment_verification_columns()
        _ensure_ai_reference_columns()
        _ensure_order_payment_completion_columns()
        _ensure_order_payment_verification_raw_response_column()
        _ensure_order_idempotency_column()
        _ensure_order_pause_column()
        _ensure_ranking_archive_columns()
        _ensure_ranking_board_columns()
        _ensure_revendedores_mapping_columns()
        _ensure_binance_columns()
        _ensure_support_columns()
        _ensure_promo_accumulated_columns()
        _ensure_raffle_draw_minute_column()
        _ensure_setting_value_text_column()
        _ensure_pin_delivered_text_column()
        _ensure_guess_winner_nick_column()
        _ensure_support_delete_columns()
        _ensure_profile_social_columns()
        _ensure_tournament_entry_rules()
        _ensure_performance_indexes()
        _init_default_data(app)
        _migrate_wallet_input_types()
        from .utils.ranking_boards import ensure_default_boards, migrate_accounts_units_label
        ensure_default_boards()
        migrate_accounts_units_label()
        # El perfil oficial del administrador (usuario "3srecargas") ya viene creado; si algo falla aquí, la web arranca igual.
        try:
            from .utils.official_profile import ensure_official_profile
            ensure_official_profile()
        except Exception:
            db.session.rollback()
            app.logger.exception('No se pudo crear el perfil oficial del administrador')

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
            'binance_verify BOOLEAN DEFAULT FALSE',
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
        if 'binance_verify' not in existing:
            db.session.execute(text('ALTER TABLE payment_methods ADD COLUMN binance_verify BOOLEAN DEFAULT 0'))
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


def _ensure_order_account_user_column():
    """Órdenes: `account_user_id` = la CUENTA que hizo la compra (ver models.Order). Las órdenes anteriores se quedan sin cuenta a propósito
    (las cuentas empiezan en cero); el índice lo crea `_ensure_performance_indexes`."""
    try:
        if _ensure_postgres_columns('orders', [
            'account_user_id INTEGER',
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return
        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'account_user_id' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN account_user_id INTEGER'))
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


def _ensure_package_points_column():
    try:
        if _ensure_postgres_columns('packages', ['points_reward INTEGER']):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(packages)')).fetchall()
        existing = {r[1] for r in rows}
        if 'points_reward' not in existing:
            db.session.execute(text('ALTER TABLE packages ADD COLUMN points_reward INTEGER'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_package_support_fallback_column():
    try:
        if _ensure_postgres_columns('packages', ['show_support_fallback BOOLEAN DEFAULT TRUE']):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(packages)')).fetchall()
        existing = {r[1] for r in rows}
        if 'show_support_fallback' not in existing:
            db.session.execute(text('ALTER TABLE packages ADD COLUMN show_support_fallback BOOLEAN DEFAULT 1'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_package_bs_price_column():
    try:
        if _ensure_postgres_columns('packages', ['bs_price NUMERIC(10, 2)']):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(packages)')).fetchall()
        existing = {r[1] for r in rows}
        if 'bs_price' not in existing:
            db.session.execute(text('ALTER TABLE packages ADD COLUMN bs_price NUMERIC(10, 2)'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_package_cost_column():
    try:
        if _ensure_postgres_columns('packages', ['cost_usd NUMERIC(10, 2)']):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(packages)')).fetchall()
        existing = {r[1] for r in rows}
        if 'cost_usd' not in existing:
            db.session.execute(text('ALTER TABLE packages ADD COLUMN cost_usd NUMERIC(10, 2)'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_package_section_column():
    """Apartado del paquete (Moneda principal / Pases / Tarjetas). Los paquetes que ya existían quedan sin valor = moneda principal."""
    try:
        if _ensure_postgres_columns('packages', ['section VARCHAR(10)']):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(packages)')).fetchall()
        existing = {r[1] for r in rows}
        if 'section' not in existing:
            db.session.execute(text('ALTER TABLE packages ADD COLUMN section VARCHAR(10)'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_horde_columns():
    """Hordas de Diamantes: premios del 2do y 3er lugar y partidas extra
    canjeadas con puntos."""
    tables = {
        'promo_horde_configs': ['package_id_2 INTEGER', 'package_id_3 INTEGER', 'points_per_extra_run INTEGER DEFAULT 0'],
        'promo_horde_runs': ['points_spent INTEGER NOT NULL DEFAULT 0', 'seed BIGINT', 'wave INTEGER', 'flag VARCHAR(40)',
                             'replay_check VARCHAR(20)', 'replay_wave INTEGER', 'replay_diamonds INTEGER'],
        'promo_horde_replays': ['rules INTEGER DEFAULT 1'],
        # Asistente de IA del chat de soporte. Los chats que ya existían
        # quedan con la IA apagada (quizás ya los atiende una persona); los
        # nuevos la traen encendida por el default del modelo.
        'support_chats': ['ai_active BOOLEAN DEFAULT FALSE', 'ai_failed INTEGER DEFAULT 0', 'ai_handoff_at TIMESTAMP',
                          'ai_typing_at TIMESTAMP', 'ai_verified_orders TEXT'],
        'support_messages': ['is_ai BOOLEAN DEFAULT FALSE', 'action_url VARCHAR(255)', 'action_label VARCHAR(60)',
                             'ai_note TEXT'],
        # Ganadores ya existentes = ya entregados → 'approved'.
        'promo_horde_winners': ["status VARCHAR(20) NOT NULL DEFAULT 'approved'", 'package_id INTEGER', 'reviewed_at TIMESTAMP'],
    }
    for table_name, columns in tables.items():
        try:
            if _ensure_postgres_columns(table_name, columns):
                continue

            if db.engine.dialect.name != 'sqlite':
                continue

            rows = db.session.execute(text(f'PRAGMA table_info({table_name})')).fetchall()
            existing = {r[1] for r in rows}
            for column_def in columns:
                if column_def.split()[0] not in existing:
                    db.session.execute(text(f'ALTER TABLE {table_name} ADD COLUMN {column_def}'))
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


def _migrate_wallet_input_types():
    """Una sola vez: los productos de Wallet de antes ignoraban el tipo de campo (siempre pedían el correo), así que los que traían el valor de
    fábrica ("solo números") o "texto" pasan a "correo", que es lo que sus clientes ven hoy. Desde ahí Wallet obedece el tipo que elija el dueño."""
    try:
        from .models import Category, Game, Setting
        key = 'wallet_input_types_migrated_v1'
        if Setting.query.filter_by(key=key).first():
            return
        wallet = Category.query.filter_by(slug='wallet').first()
        if wallet is not None:
            for game in Game.query.filter_by(category_id=wallet.id).all():
                if (game.player_id_input_type or 'numeric') in ('numeric', 'text'):
                    game.player_id_input_type = 'email'
        db.session.add(Setting(key=key, value='1'))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_game_columns():
    try:
        if _ensure_postgres_columns('games', [
            'show_selection_popup BOOLEAN DEFAULT FALSE',
            "player_id_input_type VARCHAR(20) DEFAULT 'numeric'",
            'bs_rate_override NUMERIC(10, 4)',
            'show_wallet_notice BOOLEAN DEFAULT TRUE',
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(games)')).fetchall()
        existing = {r[1] for r in rows}
        if 'show_selection_popup' not in existing:
            db.session.execute(text('ALTER TABLE games ADD COLUMN show_selection_popup BOOLEAN DEFAULT 0'))
        if 'show_wallet_notice' not in existing:
            db.session.execute(text('ALTER TABLE games ADD COLUMN show_wallet_notice BOOLEAN DEFAULT 1'))
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


def _ensure_mini_influencer_columns():
    """Columnas del autoservicio de mini influencers sobre `affiliates`
    (login propio, estado de solicitud, datos de la solicitud). Un afiliado
    creado antes de este cambio no tiene ninguna de estas columnas seteadas
    y sigue funcionando igual: is_mini queda NULL/False y status NULL, el
    código de la app trata NULL como 'approved'/'no es mini' donde aplica."""
    try:
        if _ensure_postgres_columns('affiliates', [
            'password_hash VARCHAR(255)',
            "status VARCHAR(20) DEFAULT 'approved'",
            'is_mini BOOLEAN DEFAULT FALSE',
            'channel_url VARCHAR(255)',
            'whatsapp_phone VARCHAR(50)',
            'application_note TEXT',
            'rejection_reason TEXT',
            'reviewed_at TIMESTAMP',
            'ranks_paid TEXT',
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(affiliates)')).fetchall()
        existing = {r[1] for r in rows}
        if 'password_hash' not in existing:
            db.session.execute(text('ALTER TABLE affiliates ADD COLUMN password_hash VARCHAR(255)'))
        if 'status' not in existing:
            db.session.execute(text("ALTER TABLE affiliates ADD COLUMN status VARCHAR(20) DEFAULT 'approved'"))
        if 'is_mini' not in existing:
            db.session.execute(text('ALTER TABLE affiliates ADD COLUMN is_mini BOOLEAN DEFAULT 0'))
        if 'channel_url' not in existing:
            db.session.execute(text('ALTER TABLE affiliates ADD COLUMN channel_url VARCHAR(255)'))
        if 'whatsapp_phone' not in existing:
            db.session.execute(text('ALTER TABLE affiliates ADD COLUMN whatsapp_phone VARCHAR(50)'))
        if 'application_note' not in existing:
            db.session.execute(text('ALTER TABLE affiliates ADD COLUMN application_note TEXT'))
        if 'rejection_reason' not in existing:
            db.session.execute(text('ALTER TABLE affiliates ADD COLUMN rejection_reason TEXT'))
        if 'reviewed_at' not in existing:
            db.session.execute(text('ALTER TABLE affiliates ADD COLUMN reviewed_at DATETIME'))
        if 'ranks_paid' not in existing:
            db.session.execute(text('ALTER TABLE affiliates ADD COLUMN ranks_paid TEXT'))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_mini_withdrawal_columns():
    """Comprobante que el admin sube al aprobar un retiro de mini, para que
    el mini lo vea en su propio panel sin tener que preguntar."""
    try:
        if _ensure_postgres_columns('affiliate_withdrawals', [
            'payment_proof VARCHAR(255)',
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return
        rows = db.session.execute(text('PRAGMA table_info(affiliate_withdrawals)')).fetchall()
        existing = {r[1] for r in rows}
        if 'payment_proof' not in existing:
            db.session.execute(text('ALTER TABLE affiliate_withdrawals ADD COLUMN payment_proof VARCHAR(255)'))
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


def _ensure_order_payment_completion_columns():
    try:
        if _ensure_postgres_columns('orders', [
            'awaiting_payment_completion BOOLEAN DEFAULT FALSE',
            'paid_amount_bs NUMERIC(10, 2)',
            'remainder_reference VARCHAR(255)',
            'remainder_capture VARCHAR(255)',
            'remainder_ai_extracted_reference VARCHAR(255)',
            'remainder_references TEXT',
            'remainder_verification_ids TEXT',
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'awaiting_payment_completion' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN awaiting_payment_completion BOOLEAN DEFAULT 0'))
        if 'paid_amount_bs' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN paid_amount_bs NUMERIC(10, 2)'))
        if 'remainder_reference' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN remainder_reference VARCHAR(255)'))
        if 'remainder_capture' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN remainder_capture VARCHAR(255)'))
        if 'remainder_ai_extracted_reference' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN remainder_ai_extracted_reference VARCHAR(255)'))
        if 'remainder_references' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN remainder_references TEXT'))
        if 'remainder_verification_ids' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN remainder_verification_ids TEXT'))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_order_payment_verification_raw_response_column():
    try:
        if _ensure_postgres_columns('orders', [
            'payment_verification_raw_response TEXT',
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'payment_verification_raw_response' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN payment_verification_raw_response TEXT'))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_order_pause_column():
    """Columna de la pausa de emergencia de una orden (orders.is_paused, ver utils/order_pause.py). En Postgres las órdenes que ya existen quedan en FALSE."""
    try:
        if _ensure_postgres_columns('orders', ['is_paused BOOLEAN DEFAULT FALSE']):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(orders)')).fetchall()
        existing = {r[1] for r in rows}
        if 'is_paused' not in existing:
            db.session.execute(text('ALTER TABLE orders ADD COLUMN is_paused BOOLEAN DEFAULT 0'))
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
        if _ensure_postgres_columns('ranking_archives', [
            'player_id VARCHAR(100)',
            'nickname VARCHAR(200)',
            'prize_order_id INTEGER',
            'user_id INTEGER',
            "prize_detail VARCHAR(200) DEFAULT ''",
        ]):
            return

        if db.engine.dialect.name != 'sqlite':
            return

        rows = db.session.execute(text('PRAGMA table_info(ranking_archives)')).fetchall()
        existing = {r[1] for r in rows}
        if 'player_id' not in existing:
            db.session.execute(text('ALTER TABLE ranking_archives ADD COLUMN player_id VARCHAR(100)'))
        if 'nickname' not in existing:
            db.session.execute(text('ALTER TABLE ranking_archives ADD COLUMN nickname VARCHAR(200)'))
        if 'prize_order_id' not in existing:
            db.session.execute(text('ALTER TABLE ranking_archives ADD COLUMN prize_order_id INTEGER'))
        if 'user_id' not in existing:
            db.session.execute(text('ALTER TABLE ranking_archives ADD COLUMN user_id INTEGER'))
        if 'prize_detail' not in existing:
            db.session.execute(text("ALTER TABLE ranking_archives ADD COLUMN prize_detail VARCHAR(200) DEFAULT ''"))

        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_ranking_board_columns():
    try:
        if _ensure_postgres_columns('ranking_boards', ['pays_from VARCHAR(7)']):
            return
        if db.engine.dialect.name != 'sqlite':
            return
        rows = db.session.execute(text('PRAGMA table_info(ranking_boards)')).fetchall()
        if rows and 'pays_from' not in {r[1] for r in rows}:
            db.session.execute(text('ALTER TABLE ranking_boards ADD COLUMN pays_from VARCHAR(7)'))
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


def _ensure_support_columns():
    """Columnas del chat de soporte.

    Las tablas nacen de `db.create_all()`, pero eso solo crea lo que no
    existe: no toca una tabla ya creada a la que luego se le añade un
    campo. Como el soporte va a seguir creciendo (vincular una orden,
    bloquear a un insistente), la lista se mantiene aquí desde el primer
    día y las altas futuras solo agregan una línea.
    """
    try:
        chat_columns = [
            'short_code VARCHAR(8)',
            'client_email VARCHAR(255)',
            'order_id INTEGER',
            'user_id INTEGER',
            'context_order_number VARCHAR(20)',
            'context_player_id VARCHAR(100)',
            'context_email VARCHAR(255)',
            'context_phone VARCHAR(50)',
            'context_game VARCHAR(120)',
            'context_package VARCHAR(160)',
            'context_page VARCHAR(255)',
            "status VARCHAR(20) DEFAULT 'open'",
            'is_blocked BOOLEAN DEFAULT FALSE',
            'unread_admin INTEGER DEFAULT 0',
            'unread_client INTEGER DEFAULT 0',
            'client_ip VARCHAR(45)',
            'user_agent VARCHAR(255)',
            'admin_note TEXT',
            'admin_note_at TIMESTAMP',
            'admin_note_admin_id INTEGER',
            'last_message_at TIMESTAMP',
            'closed_at TIMESTAMP',
        ]
        message_columns = [
            'admin_id INTEGER',
            'attachment VARCHAR(255)',
            'read_at TIMESTAMP',
        ]
        tag_columns = [
            "kind VARCHAR(10) DEFAULT 'error'",
            "color VARCHAR(9) DEFAULT '#6c5ce7'",
            'is_active BOOLEAN DEFAULT TRUE',
            'sort_order INTEGER DEFAULT 0',
        ]

        if _ensure_postgres_columns('support_chats', chat_columns):
            _ensure_postgres_columns('support_messages', message_columns)
            _ensure_postgres_columns('support_tags', tag_columns)
            return

        if db.engine.dialect.name != 'sqlite':
            return

        for table, columns in (
            ('support_chats', chat_columns),
            ('support_messages', message_columns),
            ('support_tags', tag_columns),
        ):
            rows = db.session.execute(text(f'PRAGMA table_info({table})')).fetchall()
            if not rows:
                continue
            existing = {r[1] for r in rows}
            for column_def in columns:
                name = column_def.split()[0]
                if name not in existing:
                    db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {column_def}'))

        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_promo_accumulated_columns():
    """Orden del juego en el selector de cada promo (Recarga Acumulada,
    Sorteo Diario, Adivina el Número), editable desde Admin > Mini Juegos
    sin depender de la Posición general de la tienda. Cada promo tiene su
    propio "sort_order" porque no necesariamente comparten el mismo orden
    entre sí."""
    try:
        for table in ('promo_accumulated_levels', 'promo_raffle_configs', 'promo_guess_configs'):
            if _ensure_postgres_columns(table, ['sort_order INTEGER DEFAULT 100']):
                continue
            if db.engine.dialect.name != 'sqlite':
                continue
            rows = db.session.execute(text(f'PRAGMA table_info({table})')).fetchall()
            existing = {r[1] for r in rows}
            if 'sort_order' not in existing:
                db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN sort_order INTEGER DEFAULT 100'))
                db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_setting_value_text_column():
    """`settings.value` nació como VARCHAR(255) para valores cortos, pero
    ahora también guarda cosas largas como las reglas del programa de
    minis (varios párrafos). En SQLite (dev local) el límite nunca se
    aplicó de verdad, así que esto pasaba desapercibido; en Postgres (el
    VPS de producción) sí es un límite duro y guardar un texto largo
    tiraba un error 500 al hacer clic en "Guardar". Se pasa a TEXT (sin
    límite) en ambos motores."""
    try:
        if db.engine.dialect.name == 'postgresql':
            db.session.execute(text('ALTER TABLE settings ALTER COLUMN value TYPE TEXT'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_pin_delivered_text_column():
    """`orders.pin_delivered` guardaba un solo código en VARCHAR(255). Con los
    códigos de Central One una orden puede traer varios (uno por línea), y en
    Postgres el límite de 255 es duro. Se pasa a TEXT; en SQLite no cambia nada."""
    try:
        if db.engine.dialect.name == 'postgresql':
            db.session.execute(text('ALTER TABLE orders ALTER COLUMN pin_delivered TYPE TEXT'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_profile_social_columns():
    """Perfiles sociales: ID enlazado verificado y frase/permisos del perfil (tablas que ya existían sin esas columnas)."""
    try:
        done = _ensure_postgres_columns('user_linked_ids', ['verified_at TIMESTAMP'])
        done = _ensure_postgres_columns('user_profiles', [
            "status_text VARCHAR(60) DEFAULT ''",
            'show_stats BOOLEAN DEFAULT TRUE',
            'in_rankings BOOLEAN DEFAULT TRUE',
            'avatar_frame_id INTEGER',
            'banner_frame_id INTEGER',
            'avatar_frame_item_id INTEGER',
            'banner_frame_item_id INTEGER',
            'avatar_photo_item_id INTEGER',
            'banner_photo_item_id INTEGER',
            'background_item_id INTEGER',
        ]) or done
        done = _ensure_postgres_columns('profile_stats', [
            "favorite_product VARCHAR(100) DEFAULT ''",
            'xp_total INTEGER DEFAULT 0',
            'xp_level INTEGER DEFAULT 1',
            'xp_rank INTEGER',
        ]) or done
        done = _ensure_postgres_columns('tournaments', ['opens_at TIMESTAMP']) or done
        done = _ensure_postgres_columns('tournament_rooms', ['finished_at TIMESTAMP']) or done
        done = _ensure_postgres_columns('profile_awards', [
            "kind VARCHAR(10) DEFAULT 'manual'",
            "month_key VARCHAR(7) DEFAULT ''",
        ]) or done
        if not done and db.engine.dialect.name == 'sqlite':
            wanted = {
                'user_linked_ids': [('verified_at', 'DATETIME')],
                'user_profiles': [('status_text', "VARCHAR(60) DEFAULT ''"), ('show_stats', 'BOOLEAN DEFAULT 1'), ('in_rankings', 'BOOLEAN DEFAULT 1'),
                                  ('avatar_frame_id', 'INTEGER'), ('banner_frame_id', 'INTEGER'),
                                  ('avatar_frame_item_id', 'INTEGER'), ('banner_frame_item_id', 'INTEGER'), ('avatar_photo_item_id', 'INTEGER'),
                                  ('banner_photo_item_id', 'INTEGER'), ('background_item_id', 'INTEGER')],
                'profile_awards': [('kind', "VARCHAR(10) DEFAULT 'manual'"), ('month_key', "VARCHAR(7) DEFAULT ''")],
                'profile_stats': [('favorite_product', "VARCHAR(100) DEFAULT ''"), ('xp_total', 'INTEGER DEFAULT 0'), ('xp_level', 'INTEGER DEFAULT 1'),
                                  ('xp_rank', 'INTEGER')],
                'tournaments': [('opens_at', 'DATETIME')],
                'tournament_rooms': [('finished_at', 'DATETIME')],
            }
            for table, columns in wanted.items():
                existing = {r[1] for r in db.session.execute(text(f'PRAGMA table_info({table})')).fetchall()}
                for name, definition in columns:
                    if name not in existing:
                        db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))
            db.session.commit()
    except Exception:
        db.session.rollback()


PERFORMANCE_INDEXES = (
    # Auditoría de rendimiento: `orders` casi no tenía índices; con decenas de miles de pedidos cada búsqueda por ID, por cuenta, por
    # referencia o por correo recorría la tabla entera (8-15 ms cada una, varias por página). Cuestan ~0,3 ms por pedido nuevo.
    ('ix_orders_game_player', 'orders', '(game_id, player_id)'),
    ('ix_orders_status_id', 'orders', '(status, id)'),
    ('ix_orders_user_id', 'orders', '(user_id)'),
    ('ix_orders_account_user_id', 'orders', '(account_user_id)'),
    ('ix_orders_payment_reference', 'orders', '(payment_reference)'),
    ('ix_orders_payment_reference_last5', 'orders', '(payment_reference_last5)'),
    ('ix_orders_remainder_reference', 'orders', '(remainder_reference)'),
    ('ix_orders_created_at', 'orders', '(created_at)'),
    ('ix_orders_game_lower_email', 'orders', '(game_id, lower(email))'),
    ('ix_orders_game_lower_player', 'orders', '(game_id, lower(player_id))'),
    ('ix_pins_package_used', 'pins', '(package_id, is_used)'),
    ('ix_api_rate_limit_hits_window', 'api_rate_limit_hits', '(window_start)'),
    ('ix_support_chats_unread_admin', 'support_chats', '(unread_admin)'),
    ('ix_ranking_archives_user_id', 'ranking_archives', '(user_id)'),
)


def _ensure_performance_indexes():
    """Crea los índices de PERFORMANCE_INDEXES si faltan (CREATE INDEX IF NOT EXISTS: idempotente, sirve en SQLite y Postgres). Cada uno va
    aparte y dentro de try/except: si dos workers arrancan a la vez y uno pierde la carrera, o una columna no existe en una base vieja,
    simplemente se salta ese índice sin tumbar el arranque."""
    for name, table, columns in PERFORMANCE_INDEXES:
        try:
            db.session.execute(text(f'CREATE INDEX IF NOT EXISTS {name} ON {table} {columns}'))
            db.session.commit()
        except Exception:
            db.session.rollback()


def _ensure_tournament_entry_rules():
    """Torneos: un ID solo puede estar en una sala a la vez (hasta que esa sala termine).

    La primera versión de `tournament_entries` tenía una restricción única (torneo, ID) y no tenía `game_id` ni `released_at`; con ella un ID nunca podía
    pasar de una sala a otra. Esa tabla solo existe en bases de prueba (los torneos todavía no se publican), así que en SQLite se reconstruye copiando sus
    filas. Las bases nuevas ya nacen bien de `db.create_all()`."""
    try:
        if db.engine.dialect.name != 'sqlite':
            return
        row = db.session.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='tournament_entries'")).fetchone()
        if not row or 'uq_tournament_entry_player' not in (row[0] or ''):
            return
        old_indexes = [r[0] for r in db.session.execute(text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tournament_entries' AND sql IS NOT NULL")).fetchall()]
        for name in old_indexes:
            db.session.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
        db.session.execute(text('ALTER TABLE tournament_entries RENAME TO tournament_entries_old'))
        db.session.commit()
        from .models import TournamentEntry
        TournamentEntry.__table__.create(bind=db.engine)
        db.session.execute(text(
            'INSERT INTO tournament_entries (id, tournament_id, room_id, game_id, player_id, player_nick, slot_number, ip, status, released_at, created_at) '
            'SELECT e.id, e.tournament_id, e.room_id, t.game_id, e.player_id, e.player_nick, e.slot_number, e.ip, e.status, NULL, e.created_at '
            'FROM tournament_entries_old e JOIN tournaments t ON t.id = e.tournament_id'))
        db.session.execute(text('DROP TABLE tournament_entries_old'))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_support_delete_columns():
    """'Eliminar para todos' en el chat de soporte (mensajes) y adjunto
    opcional en las respuestas rápidas (imagen o video)."""
    try:
        if not _ensure_postgres_columns('support_messages', [
            'is_deleted BOOLEAN DEFAULT FALSE',
            'deleted_at TIMESTAMP',
        ]) and db.engine.dialect.name == 'sqlite':
            rows = db.session.execute(text('PRAGMA table_info(support_messages)')).fetchall()
            existing = {r[1] for r in rows}
            if 'is_deleted' not in existing:
                db.session.execute(text('ALTER TABLE support_messages ADD COLUMN is_deleted BOOLEAN DEFAULT 0'))
            if 'deleted_at' not in existing:
                db.session.execute(text('ALTER TABLE support_messages ADD COLUMN deleted_at DATETIME'))
            db.session.commit()

        if not _ensure_postgres_columns('support_quick_replies', [
            'attachment VARCHAR(255)',
        ]) and db.engine.dialect.name == 'sqlite':
            rows = db.session.execute(text('PRAGMA table_info(support_quick_replies)')).fetchall()
            existing = {r[1] for r in rows}
            if 'attachment' not in existing:
                db.session.execute(text('ALTER TABLE support_quick_replies ADD COLUMN attachment VARCHAR(255)'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_guess_winner_nick_column():
    """Nombre real del jugador en cada ganador de Adivina el Número, igual
    que ya se guarda en el Sorteo Diario, para mostrarlo junto al ID."""
    try:
        if _ensure_postgres_columns('promo_guess_winners', ['player_nick VARCHAR(150)']):
            return
        if db.engine.dialect.name != 'sqlite':
            return
        rows = db.session.execute(text('PRAGMA table_info(promo_guess_winners)')).fetchall()
        existing = {r[1] for r in rows}
        if 'player_nick' not in existing:
            db.session.execute(text('ALTER TABLE promo_guess_winners ADD COLUMN player_nick VARCHAR(150)'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _ensure_raffle_draw_minute_column():
    """El sorteo diario arrancaba solo a hora en punto; se le agrega el
    minuto para poder programarlo a una hora exacta."""
    try:
        if _ensure_postgres_columns('promo_raffle_configs', ['draw_minute INTEGER DEFAULT 0']):
            return
        if db.engine.dialect.name != 'sqlite':
            return
        rows = db.session.execute(text('PRAGMA table_info(promo_raffle_configs)')).fetchall()
        existing = {r[1] for r in rows}
        if 'draw_minute' not in existing:
            db.session.execute(text('ALTER TABLE promo_raffle_configs ADD COLUMN draw_minute INTEGER DEFAULT 0'))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _init_default_data(app):
    from .models import AdminUser, Category, MiniRank, MiniViewTier, SupportTag
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

    # Checkpoint: confirma admin/categorías antes de los bloques de abajo,
    # que pueden hacer rollback si chocan por una carrera entre workers (ver
    # comentario abajo) — sin este commit, ese rollback también se llevaría
    # puesto lo de arriba en un primer arranque con la BD 100% vacía.
    db.session.commit()

    # Escalera de rangos y tabla de tramos de vistas del panel de mini
    # influencer: valores de arranque razonables, el admin los ajusta luego
    # desde /admin/minis sin tocar código.
    #
    # Con gunicorn -w 3 los 3 workers llaman a create_app() casi al mismo
    # tiempo en el primer arranque con la tabla vacía: más de un worker
    # puede ver count()==0 antes de que otro confirme su INSERT, y el
    # segundo choca contra el UNIQUE de mini_ranks.name (visto en
    # producción el 2026-08-24: tumbó el worker con IntegrityError). Se
    # aisla cada bloque en su propio try/flush/except para que un choque
    # así no aborte el resto de _init_default_data ni tumbe el worker — el
    # resultado final es el mismo, la tabla queda sembrada una sola vez.
    try:
        if MiniRank.query.count() == 0:
            for rank in [
                MiniRank(name='Bronce', uses_required=100, bonus_amount=5, sort_order=1),
                MiniRank(name='Plata', uses_required=250, bonus_amount=12, sort_order=2),
                MiniRank(name='Oro', uses_required=500, bonus_amount=25, sort_order=3),
            ]:
                db.session.add(rank)
            db.session.flush()
    except Exception:
        db.session.rollback()

    try:
        if MiniViewTier.query.count() == 0:
            for tier in [
                MiniViewTier(min_views=1000, max_views=9999, reward_amount=1, sort_order=1),
                MiniViewTier(min_views=10000, max_views=49999, reward_amount=4, sort_order=2),
                MiniViewTier(min_views=50000, max_views=99999, reward_amount=15, sort_order=3),
                MiniViewTier(min_views=100000, max_views=None, reward_amount=37, sort_order=4),
            ]:
                db.session.add(tier)
            db.session.flush()
    except Exception:
        db.session.rollback()

    # Catálogo de etiquetas del soporte. Son dos preguntas distintas y por
    # eso son dos tipos: 'user' responde quién es quien escribe (el chat
    # solo pide el nombre, que no verifica nada) y 'error' responde qué le
    # pasa. "Sin identificar" es la que se pone sola a todo chat que llega
    # sin contexto: es la cola de trabajo del admin.
    #
    # Mismo blindaje que los bloques de arriba: con `gunicorn -w 3` los
    # tres workers pueden ver la tabla vacía a la vez y el segundo choca
    # contra el UNIQUE de support_tags.slug.
    try:
        if SupportTag.query.count() == 0:
            default_tags = [
                ('user', 'Sin identificar', 'sin-identificar', '#9aa0a6', 1),
                ('user', 'Cliente frecuente', 'cliente-frecuente', '#00b894', 2),
                ('user', 'Primera compra', 'primera-compra', '#0984e3', 3),
                ('user', 'Revendedor', 'revendedor', '#6c5ce7', 4),
                ('user', 'Mini influencer', 'mini-influencer', '#e84393', 5),
                ('user', 'Reincidente', 'reincidente', '#d63031', 6),
                ('error', 'Pago no verificado', 'pago-no-verificado', '#e17055', 1),
                ('error', 'Recarga no llegó', 'recarga-no-llego', '#d63031', 2),
                ('error', 'ID incorrecto', 'id-incorrecto', '#fdcb6e', 3),
                ('error', 'Doble cobro', 'doble-cobro', '#c0392b', 4),
                ('error', 'Tasa o precio', 'tasa-o-precio', '#0984e3', 5),
                ('error', 'Código regalo', 'codigo-regalo', '#00b894', 6),
                ('error', 'Otro', 'otro', '#636e72', 7),
            ]
            for kind, name, slug, color, sort_order in default_tags:
                db.session.add(SupportTag(
                    kind=kind, name=name, slug=slug,
                    color=color, sort_order=sort_order,
                ))
            db.session.flush()
    except Exception:
        db.session.rollback()

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
