import hashlib
import hmac
import os
import re
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime

from ..models import db, AccountNotice, User, Order, Game
from ..utils import account_points, google_login
from ..utils.auth_accounts import (
    GOOGLE_SCOPE, create_registered_account, find_registered_account,
    find_scoped_customer, get_game_account_meta, get_or_create_scoped_customer, hydrate_scoped_customer_from_orders,
    id_customer_orders, is_account_user, linked_orders, sync_env_admin_user, validate_email_address,
    validate_new_password, validate_username,
)
from ..utils.locks import admin_login_locked, check_rate_limit, client_ip, register_admin_login_failure, remember_admin_device
from ..utils.profiles import get_profile
from .verify import verifiable_game_ids

auth_bp = Blueprint('auth_bp', __name__)

LOGIN_MAX_ATTEMPTS = 20
LOGIN_WINDOW_SECONDS = 10 * 60
GOOGLE_MAX_ATTEMPTS = 30
GOOGLE_ACCOUNT_SCOPE = GOOGLE_SCOPE
GOOGLE_ACCOUNT_KIND = 'google_email'
# Cuenta con contraseña: se limita por IP y también por cuenta, para que nadie pruebe contraseñas sin parar.
ACCOUNT_LOGIN_IP_MAX = 20
ACCOUNT_LOGIN_USER_MAX = 12
REGISTER_MAX_ATTEMPTS = 10
REGISTER_WINDOW_SECONDS = 60 * 60
PROFILE_EDIT_MAX = 30
PASSWORD_CHANGE_MAX = 8
LOGIN_TABS = ('id', 'cuenta', 'registro')
DEFAULT_LOGIN_TAB = 'cuenta'   # la que se abre al entrar a /login sin elegir ("Mi cuenta"); "Con mi ID" es /login?tab=id

_dummy_password_hash = None
_NEXT_UNSAFE_CHARS = re.compile(r'[\x00-\x20\x7f-\x9f]')


def _safe_next_url(value):
    """Solo rutas internas: un `next` externo (o `//otro.com`) convertía el
    login en un redirector hacia páginas falsas. También se rechaza cualquier espacio, tabulador o salto de línea dentro:
    el navegador los borra antes de seguir el enlace, así que `/<TAB>/otro.com` terminaba siendo `//otro.com`."""
    value = (value or '').strip()
    if not value.startswith('/') or value.startswith('//') or '\\' in value or _NEXT_UNSAFE_CHARS.search(value):
        return None
    return value


def _burn_password_check(password):
    """Gasta el mismo tiempo que una comprobación real cuando la cuenta no existe, para que no se pueda adivinar
    qué usuarios existen midiendo lo que tarda la respuesta."""
    global _dummy_password_hash
    if _dummy_password_hash is None:
        _dummy_password_hash = generate_password_hash('no-existe')
    check_password_hash(_dummy_password_hash, password or '')


def _signed_in_redirect():
    """Quien ya tiene sesión de administrador o de cuenta no necesita la página de acceso. El cliente que entró solo
    con su ID sí puede verla: es justo desde donde crea o abre su cuenta."""
    if not current_user.is_authenticated:
        return None
    if current_user.__class__.__name__ == 'AdminUser':
        return redirect(url_for('admin_bp.dashboard'))
    if is_account_user(current_user):
        return redirect(url_for('auth_bp.profile'))
    if current_user.__class__.__name__ != 'User':
        return redirect(url_for('main_bp.index'))
    return None


def _render_login_page(tab='id', status=200, values=None, admin_prompt=False):
    active_games = Game.query.filter_by(is_active=True).order_by(Game.position.asc(), Game.name.asc()).all()
    return render_template(
        'auth/login.html',
        active_games=active_games,
        # El correo del admin YA NO viaja en la página pública: solo el servidor sabe si lo escrito es el del admin
        # y, en ese caso, pide abrir la ventana de la clave (admin_prompt).
        admin_prompt=bool(admin_prompt),
        verifiable_ids=list(verifiable_game_ids()),
        google_login_enabled=google_login.get_config()['enabled'],
        next_url=_safe_next_url(request.args.get('next')) or '',
        # Llegó desde la tienda de puntos: se le explica para qué sirve la cuenta (los puntos de sus recargas se guardan en ella).
        from_points_shop=(_safe_next_url(request.args.get('next')) or '').split('?')[0].rstrip('/') == '/tiendapuntos',
        active_tab=tab if tab in LOGIN_TABS else DEFAULT_LOGIN_TAB,
        form_values=values or {},
        id_customer_session=current_user.is_authenticated and current_user.__class__.__name__ == 'User',
    ), status


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """Crear cuenta con nombre de usuario, correo y contraseña (la misma cuenta se puede crear con Google)."""
    blocked = _signed_in_redirect()
    if blocked:
        return blocked
    if request.method == 'GET':
        return _render_login_page('registro')

    # Campo escondido que ningún cliente real llena: si trae algo, es un programa. Se ignora sin dar pistas.
    if (request.form.get('website') or '').strip():
        return redirect(url_for('auth_bp.login'))

    username_raw = request.form.get('username', '')
    email_raw = request.form.get('email', '')
    password = request.form.get('password', '')
    repeat = request.form.get('password_repeat', '')
    values = {'reg_username': username_raw.strip()[:40], 'reg_email': email_raw.strip()[:120]}

    if not check_rate_limit(f'register:{client_ip()}', REGISTER_MAX_ATTEMPTS, REGISTER_WINDOW_SECONDS):
        flash('Demasiados intentos seguidos. Espera un rato e intenta de nuevo.', 'danger')
        return _render_login_page('registro', 429, values)

    username, username_error = validate_username(username_raw)
    email, email_error = validate_email_address(email_raw)
    password_error = validate_new_password(password, repeat, username=username_raw, email=email_raw)
    errors = [message for message in (username_error, email_error, password_error) if message]
    if errors:
        for message in errors:
            flash(message, 'danger')
        return _render_login_page('registro', 400, values)

    try:
        user = create_registered_account(username, email, password)
    except IntegrityError:
        # Dos registros iguales al mismo tiempo: el segundo choca con el índice único.
        db.session.rollback()
        flash('Ese nombre de usuario o correo ya está en uso. Prueba con otro.', 'danger')
        return _render_login_page('registro', 409, values)

    login_user(user, remember=True)
    flash('¡Tu cuenta está lista! Las compras que hagas con ella se guardan en tu historial y los puntos de tus recargas se guardan en ella.', 'success')
    return redirect(_safe_next_url(request.args.get('next')) or url_for('auth_bp.profile'))


@auth_bp.route('/login/cuenta', methods=['GET', 'POST'])
def account_login():
    """Entrar con la cuenta (usuario o correo + contraseña)."""
    blocked = _signed_in_redirect()
    if blocked:
        return blocked
    if request.method == 'GET':
        return redirect(url_for('auth_bp.login', tab='cuenta', next=_safe_next_url(request.args.get('next')) or None))

    login_value = request.form.get('login', '').strip()
    password = request.form.get('password', '')
    values = {'acc_login': login_value[:120]}

    if not check_rate_limit(f'account_login:{client_ip()}', ACCOUNT_LOGIN_IP_MAX, LOGIN_WINDOW_SECONDS):
        flash('Demasiados intentos seguidos. Espera unos minutos e intenta de nuevo.', 'danger')
        return _render_login_page('cuenta', 429, values)
    if not login_value or not password:
        flash('Escribe tu usuario o correo y tu contraseña.', 'danger')
        return _render_login_page('cuenta', 400, values)
    # El tope por cuenta lleva también la IP de quien intenta: antes la clave era solo el usuario escrito y cualquiera, con 13 intentos,
    # dejaba a la persona dueña de esa cuenta sin poder entrar. Así cada IP solo se frena a sí misma en esa cuenta.
    login_digest = hashlib.sha1(login_value.lower().encode('utf-8')).hexdigest()[:20]
    if not check_rate_limit(f'acct_user:{login_digest}:{client_ip()}', ACCOUNT_LOGIN_USER_MAX, LOGIN_WINDOW_SECONDS):
        flash('Demasiados intentos con esta cuenta. Espera unos minutos e intenta de nuevo.', 'danger')
        return _render_login_page('cuenta', 429, values)

    user = find_registered_account(login_value)
    if user is None:
        _burn_password_check(password)
        password_ok = False
    else:
        password_ok = user.check_password(password)
    if not password_ok:
        flash('Usuario o contraseña incorrectos.', 'danger')
        return _render_login_page('cuenta', 401, values)
    if not user.is_active:
        flash('Tu cuenta está desactivada. Escríbenos por soporte para ayudarte.', 'warning')
        return _render_login_page('cuenta', 403, values)

    login_user(user, remember=True)
    flash('Sesión iniciada.', 'success')
    return redirect(_safe_next_url(request.args.get('next')) or url_for('auth_bp.profile'))


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    blocked = _signed_in_redirect()
    if blocked:
        return blocked

    env_admin_username = (os.environ.get('ADMIN_USERNAME') or '').strip()
    env_admin_password = (os.environ.get('ADMIN_PASSWORD') or '').strip()
    env_admin_email = (os.environ.get('ADMIN_EMAIL') or '').strip()

    def render_login(status=200, admin_prompt=False):
        values = {'identifier': request.form.get('identifier', '').strip()[:150]} if admin_prompt else None
        return _render_login_page('id', status, values=values, admin_prompt=admin_prompt)

    if request.method == 'POST':
        if not check_rate_limit(f'customer_login:{client_ip()}', LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS):
            flash('Demasiados intentos seguidos. Espera unos minutos e intenta de nuevo.', 'danger')
            return render_login(429)

        identifier = request.form.get('identifier', '').strip()
        service_id_raw = request.form.get('service_id', '').strip()
        admin_password = request.form.get('admin_password', '').strip()

        if not identifier:
            flash('Ingresa tu ID, correo o cuenta del servicio.', 'danger')
            return render_login()

        if env_admin_email and identifier.lower() == env_admin_email.lower():
            if not admin_password:
                # Sin clave todavía: la página abre la ventana de la clave de administrador.
                return render_login(admin_prompt=True)
            if admin_login_locked():
                flash('Demasiados intentos fallidos con la clave del administrador. Espera 15 minutos.', 'danger')
                return render_login(429)
            # (admin_login_locked() deja pasar al navegador que el dueño ya usó antes; ver utils/locks.py)
            if not env_admin_password or not hmac.compare_digest(admin_password.encode('utf-8'), env_admin_password.encode('utf-8')):
                register_admin_login_failure()
                flash('Clave de administrador incorrecta.', 'danger')
                return render_login(admin_prompt=True)

            try:
                admin = sync_env_admin_user(env_admin_username, env_admin_email, env_admin_password)
            except Exception as exc:
                db.session.rollback()
                flash(f'No se pudo sincronizar la cuenta admin. {exc}', 'danger')
                return render_login()

            login_user(admin)
            flash('Sesión de administrador iniciada.', 'success')
            return remember_admin_device(redirect(url_for('admin_bp.dashboard')))

        if not service_id_raw.isdigit():
            flash('Selecciona el juego o servicio para ubicar tu historial.', 'danger')
            return render_login()

        game = Game.query.filter_by(id=int(service_id_raw), is_active=True).first()
        if not game:
            flash('El servicio seleccionado no está disponible.', 'danger')
            return render_login()

        account_meta = get_game_account_meta(game)
        user = find_scoped_customer(account_meta['scope_key'], identifier, account_meta['account_kind'])
        if not user:
            user = hydrate_scoped_customer_from_orders(game, identifier)
        if not user:
            flash('No encontramos compras previas para ese identificador en ese servicio. Haz tu primera recarga y tu acceso se creará automáticamente.', 'warning')
            return render_login()

        login_user(user, remember=True)
        flash('Entraste con tu ID. Para editar tu perfil crea una cuenta.', 'success')
        return redirect(_safe_next_url(request.args.get('next')) or url_for('auth_bp.profile'))

    # Sin pestaña elegida se abre "Mi cuenta" (usuario/correo + contraseña); "Con mi ID" se abre con /login?tab=id.
    tab = (request.args.get('tab') or DEFAULT_LOGIN_TAB).strip().lower()
    return _render_login_page(tab if tab in LOGIN_TABS else DEFAULT_LOGIN_TAB)


@auth_bp.route('/login/google')
def google_login_start():
    """Manda al cliente a Google para que elija su cuenta. Solo existe si el administrador puso las llaves."""
    blocked = _signed_in_redirect()
    if blocked:
        return blocked
    if not google_login.get_config()['enabled']:
        flash('Entrar con Google no está disponible por ahora. Usa tu ID.', 'warning')
        return redirect(url_for('auth_bp.login'))
    if not check_rate_limit(f'google_login:{client_ip()}', GOOGLE_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS):
        flash('Demasiados intentos seguidos. Espera unos minutos e intenta de nuevo.', 'danger')
        return redirect(url_for('auth_bp.login'))
    return redirect(google_login.begin(_safe_next_url(request.args.get('next'))))


@auth_bp.route('/login/google/callback')
def google_login_callback():
    """Google devuelve aquí al cliente: se comprueba todo y se abre su cuenta de cliente (nunca la de administrador)."""
    blocked = _signed_in_redirect()
    if blocked:
        return blocked
    if not check_rate_limit(f'google_login:{client_ip()}', GOOGLE_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS):
        flash('Demasiados intentos seguidos. Espera unos minutos e intenta de nuevo.', 'danger')
        return redirect(url_for('auth_bp.login'))
    try:
        info = google_login.complete(request.args)
    except google_login.GoogleLoginError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('auth_bp.login'))

    user = get_or_create_scoped_customer(
        scope_key=GOOGLE_ACCOUNT_SCOPE,
        scope_label='Google',
        raw_identifier=info['email'],
        account_kind=GOOGLE_ACCOUNT_KIND,
        contact_email=info['email'],
    )
    if not user or not user.is_active:
        flash('No se pudo abrir tu cuenta. Entra con tu ID o escríbenos por soporte.', 'danger')
        return redirect(url_for('auth_bp.login'))

    login_user(user, remember=True)
    flash('Sesión iniciada con Google.', 'success')
    return redirect(_safe_next_url(info.get('next')) or url_for('auth_bp.profile'))

@auth_bp.route('/logout', methods=['GET', 'POST'])
@login_required
def logout():
    from ..utils.official_profile import clear_admin_return
    clear_admin_return()
    logout_user()
    flash('Has cerrado sesión exitosamente.', 'info')
    return redirect(url_for('main_bp.index'))


# ───────────────────────────── Perfil ─────────────────────────────

def _customer_only():
    """None si es un cliente (por ID o cuenta); si no, a dónde mandarlo (el admin va a su panel)."""
    if current_user.__class__.__name__ == 'AdminUser':
        return redirect(url_for('admin_bp.dashboard'))
    if current_user.__class__.__name__ != 'User':
        return redirect(url_for('main_bp.index'))
    return None


def _account_only():
    """Editar es solo para cuentas: quien entró con su ID únicamente puede mirar."""
    blocked = _customer_only()
    if blocked:
        return blocked
    if not is_account_user(current_user):
        flash('Con tu ID solo puedes ver tu perfil. Crea una cuenta para editarlo.', 'warning')
        return redirect(url_for('auth_bp.login', tab='registro'))
    return None


def _profile_rate_limited(kind, limit=PROFILE_EDIT_MAX):
    if check_rate_limit(f'profile_{kind}:{current_user.id}', limit, LOGIN_WINDOW_SECONDS):
        return False
    flash('Demasiados intentos seguidos. Espera unos minutos e intenta de nuevo.', 'danger')
    return True


@auth_bp.route('/profile')
@login_required
def profile():
    """Quien tiene CUENTA va a su perfil público (/u/<usuario>); quien entró solo con su ID ve su perfil de solo lectura."""
    blocked = _customer_only()
    if blocked:
        return blocked

    if is_account_user(current_user):
        if not current_user.has_chosen_username:
            flash('Elige tu nombre de usuario para tener perfil público: sale en su dirección.', 'info')
            return redirect(url_for('auth_bp.account'))
        return redirect(url_for('profiles_bp.public_profile', username=current_user.username))

    # Solo con su ID: vista de solo lectura con las órdenes de ese ID.
    orders = id_customer_orders(current_user, limit=20)
    return render_template(
        'auth/profile.html', orders=orders, is_account=False, profile_url=None, profile_status=None,
    )


@auth_bp.route('/account')
@login_required
def account():
    """Cuenta y seguridad (correo, contraseña, órdenes). El perfil que ven los demás es aparte (/u/<usuario>)."""
    blocked = _customer_only()
    if blocked:
        return blocked
    if not is_account_user(current_user):
        return redirect(url_for('auth_bp.profile'))

    # Cuenta independiente de los IDs: las órdenes son las que ELLA compró con la sesión abierta (a cualquier ID).
    orders = linked_orders(current_user, limit=20)
    # Avisos pendientes de la cuenta: salen una sola vez.
    for notice in AccountNotice.query.filter_by(user_id=current_user.id, seen_at=None).order_by(AccountNotice.id.asc()).all():
        flash(notice.message, 'warning')
        notice.seen_at = datetime.utcnow()
    db.session.commit()
    public_url = None
    status = None
    if current_user.has_chosen_username:
        public_url = url_for('profiles_bp.public_profile', username=current_user.username)
        prof = get_profile(current_user)
        status = 'oculto' if (prof and prof.moderation_hidden) else ('privado' if (prof and not prof.is_public) else 'publico')
    return render_template(
        'auth/profile.html',
        orders=orders,
        is_account=True,
        profile_url=public_url,
        profile_status=status,
        points_balance=account_points.balance(current_user.id),
    )


@auth_bp.route('/profile/username', methods=['POST'])
@login_required
def profile_username():
    blocked = _account_only()
    if blocked:
        return blocked
    if _profile_rate_limited('edit'):
        return redirect(url_for('auth_bp.account') + '#datos')
    username, error = validate_username(request.form.get('username', ''), exclude_user_id=current_user.id)
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth_bp.account') + '#datos')
    current_user.username = username
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('Ese nombre de usuario ya está en uso. Elige otro.', 'danger')
        return redirect(url_for('auth_bp.account') + '#datos')
    flash('Nombre de usuario actualizado.', 'success')
    return redirect(url_for('auth_bp.account') + '#datos')


@auth_bp.route('/profile/email', methods=['POST'])
@login_required
def profile_email():
    blocked = _account_only()
    if blocked:
        return blocked
    if not current_user.is_registered_account:
        flash('Tu correo es el de tu cuenta de Google y no se cambia desde aquí.', 'warning')
        return redirect(url_for('auth_bp.account') + '#datos')
    if _profile_rate_limited('password', PASSWORD_CHANGE_MAX):
        return redirect(url_for('auth_bp.account') + '#datos')
    if not current_user.check_password(request.form.get('current_password', '')):
        flash('La contraseña actual no es correcta.', 'danger')
        return redirect(url_for('auth_bp.account') + '#datos')
    email, error = validate_email_address(request.form.get('email', ''), exclude_user_id=current_user.id)
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth_bp.account') + '#datos')
    current_user.email = email
    current_user.contact_email = email
    current_user.account_identifier = email
    current_user.account_identifier_normalized = email
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash('Ese correo ya tiene una cuenta.', 'danger')
        return redirect(url_for('auth_bp.account') + '#datos')
    flash('Correo actualizado.', 'success')
    return redirect(url_for('auth_bp.account') + '#datos')


@auth_bp.route('/profile/password', methods=['POST'])
@login_required
def profile_password():
    blocked = _account_only()
    if blocked:
        return blocked
    if not current_user.is_registered_account:
        flash('Tu cuenta entra con Google: no tiene contraseña que cambiar.', 'warning')
        return redirect(url_for('auth_bp.account') + '#datos')
    if _profile_rate_limited('password', PASSWORD_CHANGE_MAX):
        return redirect(url_for('auth_bp.account') + '#datos')
    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    repeat = request.form.get('new_password_repeat', '')
    if not current_user.check_password(current_password):
        flash('La contraseña actual no es correcta.', 'danger')
        return redirect(url_for('auth_bp.account') + '#datos')
    error = validate_new_password(new_password, repeat, username=current_user.username, email=current_user.email)
    if not error and new_password == current_password:
        error = 'La contraseña nueva debe ser distinta a la actual.'
    if error:
        flash(error, 'danger')
        return redirect(url_for('auth_bp.account') + '#datos')
    current_user.set_password(new_password)
    db.session.commit()
    flash('Contraseña actualizada.', 'success')
    return redirect(url_for('auth_bp.account') + '#datos')


# Las cuentas son independientes de los IDs (decisión del dueño, 2026-10-14): ya no se enlazan ni se verifican IDs. Lo que la cuenta compra
# con la sesión abierta queda en su historial (Order.account_user_id) y suma a su nivel; el historial de cada ID sigue siendo del ID.
