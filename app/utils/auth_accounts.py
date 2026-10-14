import hashlib
import re
import uuid

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from datetime import datetime

from ..models import db, AccountNotice, AdminUser, Game, User, Order, UserLinkedId
from .locks import acquire_lock, release_lock
from .order_access import reference_tail_matches


EMAIL_ACCOUNT_KINDS = {'wallet_email', 'delivery_email', 'binance_account', 'email', 'google_email'}

# Dos tipos de cliente:
#   * "por ID": cliente creado solo con comprar o escribir su ID (account_scope = game:<id>). Ve su historial pero NO
#     puede editar nada.
#   * CUENTA registrada: con usuario + correo + contraseña ('registered') o con Google ('google_email'). Puede editar
#     su perfil; su historial de compras es lo que compra con la sesión abierta (no depende de ningún ID).
ACCOUNT_KINDS = {'registered', 'google_email'}
ACCOUNT_SCOPE = 'account'
GOOGLE_SCOPE = 'google'


def is_account_user(user):
    """True si es una cuenta registrada (correo/contraseña o Google); False para el cliente "por ID" y para el admin."""
    return bool(
        user is not None
        and getattr(user, 'is_authenticated', False)
        and user.__class__.__name__ == 'User'
        and (getattr(user, 'account_kind', '') or '') in ACCOUNT_KINDS
    )


def _get_game_player_input_type(game):
    value = (getattr(game, 'player_id_input_type', '') or '').strip().lower()
    return value if value in {'numeric', 'text', 'email'} else 'numeric'


def normalize_customer_identifier(raw_identifier, account_kind='generic'):
    value = ' '.join(str(raw_identifier or '').strip().split())
    if not value:
        return ''

    if account_kind in EMAIL_ACCOUNT_KINDS or '@' in value:
        return value.lower()

    if account_kind == 'player_id':
        return value.replace(' ', '')

    return value.lower()


def get_game_account_meta(game):
    category_slug = ((game.category.slug if game and game.category else '') or '').lower()
    game_name = (game.name or '').strip() if game else 'Servicio'
    account_kind = 'player_id'
    identifier_label = (getattr(game, 'player_id_label', '') or 'ID del jugador').strip()

    if category_slug == 'tarjetas':
        account_kind = 'delivery_email'
        identifier_label = 'Correo de entrega'
    elif category_slug == 'wallet':
        if 'binance' in game_name.lower():
            account_kind = 'binance_account'
            identifier_label = 'Pay ID o correo de destino'
        else:
            account_kind = 'wallet_email'
            identifier_label = 'Correo o cuenta del servicio'
    elif _get_game_player_input_type(game) == 'email':
        account_kind = 'email'

    return {
        'scope_key': f'game:{game.id}' if game and game.id is not None else 'game:unknown',
        'scope_label': game_name,
        'account_kind': account_kind,
        'identifier_label': identifier_label,
    }


def extract_customer_identifier_for_game(game, player_id='', email=''):
    meta = get_game_account_meta(game)
    account_kind = meta['account_kind']

    if account_kind == 'delivery_email':
        identifier = (email or '').strip()
    elif account_kind in {'wallet_email', 'binance_account'}:
        identifier = (player_id or email or '').strip()
    else:
        identifier = (player_id or '').strip()

    meta['identifier'] = identifier
    meta['identifier_normalized'] = normalize_customer_identifier(identifier, account_kind)
    return meta


def _build_customer_credentials(scope_key, normalized_identifier):
    digest = hashlib.sha1(f'{scope_key}|{normalized_identifier}'.encode('utf-8')).hexdigest()
    return {
        'username': f'u_{digest[:24]}',
        'email': f'{digest[:32]}@session.local',
        'password': uuid.uuid4().hex,
    }


def find_scoped_customer(scope_key, raw_identifier, account_kind='generic'):
    normalized_identifier = normalize_customer_identifier(raw_identifier, account_kind)
    if not normalized_identifier:
        return None
    return User.query.filter_by(
        account_scope=scope_key,
        account_identifier_normalized=normalized_identifier,
    ).first()


def get_or_create_scoped_customer(scope_key, scope_label, raw_identifier, account_kind='generic', contact_email='', phone=''):
    normalized_identifier = normalize_customer_identifier(raw_identifier, account_kind)
    if not normalized_identifier:
        return None

    user = find_scoped_customer(scope_key, raw_identifier, account_kind)
    if not user:
        credentials = _build_customer_credentials(scope_key, normalized_identifier)
        user = User(
            username=credentials['username'],
            email=credentials['email'],
            account_scope=scope_key,
            account_scope_label=scope_label,
            account_identifier=(raw_identifier or '').strip(),
            account_identifier_normalized=normalized_identifier,
            account_kind=account_kind,
            contact_email=(contact_email or '').strip() or None,
            phone=(phone or '').strip() or None,
        )
        user.set_password(credentials['password'])
        db.session.add(user)
        db.session.commit()
        return user

    should_commit = False
    raw_identifier = (raw_identifier or '').strip()
    contact_email = (contact_email or '').strip()
    phone = (phone or '').strip()

    if raw_identifier and user.account_identifier != raw_identifier:
        user.account_identifier = raw_identifier
        should_commit = True
    if scope_label and user.account_scope_label != scope_label:
        user.account_scope_label = scope_label
        should_commit = True
    if contact_email and user.contact_email != contact_email:
        user.contact_email = contact_email
        should_commit = True
    if phone and user.phone != phone:
        user.phone = phone
        should_commit = True

    if should_commit:
        db.session.commit()

    return user


def attach_matching_orders_to_customer(user, game_id, raw_identifier, account_kind='generic'):
    if not user or not game_id:
        return 0

    raw_identifier = (raw_identifier or '').strip()
    normalized_identifier = normalize_customer_identifier(raw_identifier, account_kind)
    if not normalized_identifier:
        return 0

    query = Order.query.filter(Order.game_id == game_id)
    if account_kind == 'delivery_email':
        query = query.filter(func.lower(Order.email) == normalized_identifier)
    elif account_kind == 'email':
        query = query.filter(func.lower(Order.player_id) == normalized_identifier)
    elif account_kind in {'wallet_email', 'binance_account'}:
        query = query.filter(
            or_(
                func.lower(Order.email) == normalized_identifier,
                func.lower(Order.player_id) == normalized_identifier,
            )
        )
    else:
        query = query.filter(Order.player_id == raw_identifier)

    changed = 0
    for order in query.all():
        if order.user_id != user.id:
            order.user_id = user.id
            changed += 1

    if changed:
        db.session.commit()

    return changed


def hydrate_scoped_customer_from_orders(game, raw_identifier):
    meta = get_game_account_meta(game)
    raw_identifier = (raw_identifier or '').strip()
    normalized_identifier = normalize_customer_identifier(raw_identifier, meta['account_kind'])
    if not normalized_identifier:
        return None

    query = Order.query.filter(Order.game_id == game.id)
    if meta['account_kind'] == 'delivery_email':
        query = query.filter(func.lower(Order.email) == normalized_identifier)
    elif meta['account_kind'] == 'email':
        query = query.filter(func.lower(Order.player_id) == normalized_identifier)
    elif meta['account_kind'] in {'wallet_email', 'binance_account'}:
        query = query.filter(
            or_(
                func.lower(Order.email) == normalized_identifier,
                func.lower(Order.player_id) == normalized_identifier,
            )
        )
    else:
        query = query.filter(Order.player_id == raw_identifier)

    seed_order = query.order_by(Order.created_at.asc()).first()
    if not seed_order:
        return None

    contact_email = (seed_order.email or '').strip()
    phone = (seed_order.phone or '').strip()
    user = get_or_create_scoped_customer(
        scope_key=meta['scope_key'],
        scope_label=meta['scope_label'],
        raw_identifier=raw_identifier,
        account_kind=meta['account_kind'],
        contact_email=contact_email,
        phone=phone,
    )
    attach_matching_orders_to_customer(user, game.id, raw_identifier, meta['account_kind'])
    return user


def sync_env_admin_user(admin_username, admin_email, admin_password):
    admin_username = (admin_username or '').strip()
    admin_email = (admin_email or '').strip()
    admin_password = (admin_password or '').strip()

    if not admin_username or not admin_password:
        raise ValueError('Faltan ADMIN_USERNAME o ADMIN_PASSWORD.')

    admin = AdminUser.query.filter_by(username=admin_username).first()
    if not admin and admin_email:
        admin = AdminUser.query.filter_by(email=admin_email).first()

    if not admin and AdminUser.query.count() == 1:
        admin = AdminUser.query.first()

    if not admin:
        admin = AdminUser(
            username=admin_username,
            email=admin_email or f'{admin_username}@localhost',
        )
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()
        return admin

    should_commit = False
    if admin.username != admin_username:
        username_taken = (
            AdminUser.query
            .filter(AdminUser.username == admin_username, AdminUser.id != admin.id)
            .first()
        )
        if username_taken:
            raise ValueError('ADMIN_USERNAME ya existe en otro registro admin.')
        admin.username = admin_username
        should_commit = True

    if admin_email and admin.email != admin_email:
        email_taken = (
            AdminUser.query
            .filter(AdminUser.email == admin_email, AdminUser.id != admin.id)
            .first()
        )
        if email_taken:
            raise ValueError('ADMIN_EMAIL ya existe en otro registro admin.')
        admin.email = admin_email
        should_commit = True

    if not admin.check_password(admin_password):
        admin.set_password(admin_password)
        should_commit = True

    if should_commit:
        db.session.commit()

    return admin


def _identifier_condition(game, normalized_identifier):
    """Condición SQL de "las órdenes de este juego hechas con este ID/correo" (las mismas reglas del acceso por ID)."""
    kind = get_game_account_meta(game)['account_kind']
    if kind == 'delivery_email':
        match = func.lower(Order.email) == normalized_identifier
    elif kind == 'email':
        match = func.lower(Order.player_id) == normalized_identifier
    elif kind in {'wallet_email', 'binance_account'}:
        match = or_(func.lower(Order.email) == normalized_identifier, func.lower(Order.player_id) == normalized_identifier)
    else:
        match = Order.player_id == normalized_identifier
    return db.and_(Order.game_id == game.id, match)


identifier_condition = _identifier_condition  # público: lo usan las estadísticas del perfil


def account_orders_query(user):
    """Órdenes que ve la CUENTA en Mi cuenta: las que ELLA compró con la sesión abierta, a cualquier ID (`Order.account_user_id`). Las cuentas son
    independientes de los IDs (decisión del dueño, 2026-10-14): no hay IDs enlazados ni verificados, y el historial de cada ID sigue siendo del
    ID (lo ve quien entra con ese ID), sin importar qué cuenta le recargó."""
    return Order.query.filter(Order.account_user_id == user.id)


def linked_orders(user, limit=20):
    """Las órdenes de la cuenta (ver `account_orders_query`), las más nuevas primero."""
    return account_orders_query(user).order_by(Order.created_at.desc(), Order.id.desc()).limit(limit).all()


def count_account_orders(user):
    return account_orders_query(user).count()


def id_customer_orders(user, limit=20):
    """Órdenes del perfil de solo lectura (entró con su ID): las que ya están a su nombre y las hechas con ese mismo ID,
    aunque por algún motivo todavía no se hayan unido a su cliente."""
    conditions = [Order.user_id == user.id]
    scope = (user.account_scope or '')
    if scope.startswith('game:') and scope[5:].isdigit() and user.account_identifier_normalized:
        game = db.session.get(Game, int(scope[5:]))
        if game:
            conditions.append(_identifier_condition(game, user.account_identifier_normalized))
    return Order.query.filter(or_(*conditions)).order_by(Order.created_at.desc(), Order.id.desc()).limit(limit).all()


# ───────────────────────── Registro y datos de una cuenta ─────────────────────────
USERNAME_RE = re.compile(r'^[A-Za-z0-9._-]{3,20}$')
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
INTERNAL_USERNAME_RE = re.compile(r'^u_[0-9a-f]{24}$', re.IGNORECASE)
RESERVED_USERNAMES = {
    'admin', 'administrador', 'administrator', 'root', 'soporte', 'support', 'moderador', 'staff', 'sistema', 'system',
    '3s', '3srecargas', '3s_recargas', 'recargas', 'tienda', 'ayuda', 'oficial', 'null', 'none', 'undefined',
}
WEAK_PASSWORDS = {
    '12345678', '123456789', '1234567890', '11111111', '00000000', 'password', 'password1', 'contrasena', 'contraseña',
    'qwertyui', 'qwerty123', 'abcd1234', 'iloveyou', '87654321', '3srecargas',
}
PASSWORD_MIN = 8
PASSWORD_MAX = 128


def validate_username(raw, exclude_user_id=None):
    """Devuelve (usuario_limpio, error). El usuario es único sin importar mayúsculas."""
    value = (raw or '').strip()
    if not value:
        return '', 'Escribe un nombre de usuario.'
    if not USERNAME_RE.match(value):
        return '', 'El nombre de usuario debe tener de 3 a 20 caracteres: letras, números, punto, guion o guion bajo (sin espacios).'
    if INTERNAL_USERNAME_RE.match(value) or value.lower() in RESERVED_USERNAMES:
        return '', 'Ese nombre de usuario no está disponible. Elige otro.'
    from .contact_filter import username_looks_like_contact
    if username_looks_like_contact(value):
        return '', 'El nombre de usuario no puede ser un número largo (como un ID o un teléfono) ni llevar el nombre de una red social.'
    query = User.query.filter(func.lower(User.username) == value.lower())
    if exclude_user_id:
        query = query.filter(User.id != exclude_user_id)
    if query.first():
        return '', 'Ese nombre de usuario ya está en uso. Elige otro.'
    return value, ''


def validate_email_address(raw, exclude_user_id=None):
    """Devuelve (correo_en_minúsculas, error). Un correo = una cuenta con contraseña."""
    value = (raw or '').strip().lower()
    if not value:
        return '', 'Escribe tu correo.'
    if len(value) > 120 or not EMAIL_RE.match(value) or value.endswith('@session.local'):
        return '', 'Escribe un correo válido (por ejemplo, nombre@gmail.com).'
    query = User.query.filter(func.lower(User.email) == value)
    if exclude_user_id:
        query = query.filter(User.id != exclude_user_id)
    if query.first():
        return '', 'Ese correo ya tiene una cuenta. Inicia sesión o recupera tu acceso con soporte.'
    return value, ''


def validate_new_password(password, repeat=None, username='', email=''):
    """Devuelve el mensaje de error o '' si la contraseña sirve. `repeat=None` no compara."""
    password = password or ''
    if len(password) < PASSWORD_MIN:
        return f'La contraseña debe tener al menos {PASSWORD_MIN} caracteres.'
    if len(password) > PASSWORD_MAX:
        return f'La contraseña es demasiado larga (máximo {PASSWORD_MAX} caracteres).'
    if password.strip() != password:
        return 'La contraseña no puede empezar ni terminar con espacios.'
    lowered = password.lower()
    if lowered in WEAK_PASSWORDS or len(set(password)) < 3:
        return 'Esa contraseña es muy fácil de adivinar. Elige otra.'
    if username and lowered == username.strip().lower():
        return 'La contraseña no puede ser igual a tu nombre de usuario.'
    if email and lowered == email.strip().lower():
        return 'La contraseña no puede ser igual a tu correo.'
    if repeat is not None and password != repeat:
        return 'Las contraseñas no coinciden.'
    return ''


def create_registered_account(username, email, password):
    """Crea la cuenta (ya validada) y la devuelve. El que llama maneja IntegrityError por si dos registros chocan."""
    user = User(
        username=username,
        email=email,
        account_scope=ACCOUNT_SCOPE,
        account_scope_label='Cuenta',
        account_identifier=email,
        account_identifier_normalized=email,
        account_kind='registered',
        contact_email=email,
        is_active=True,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def find_registered_account(login):
    """Busca la cuenta con contraseña por nombre de usuario o por correo (sin importar mayúsculas)."""
    value = (login or '').strip().lower()
    if not value or len(value) > 255:
        return None
    return User.query.filter(
        User.account_kind == 'registered',
        or_(func.lower(User.username) == value, func.lower(User.email) == value),
    ).first()
