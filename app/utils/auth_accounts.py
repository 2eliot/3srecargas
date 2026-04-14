import hashlib
import uuid

from sqlalchemy import func, or_

from ..models import db, AdminUser, User, Order


EMAIL_ACCOUNT_KINDS = {'wallet_email', 'delivery_email', 'binance_account', 'email'}


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
    identifier_label = 'ID del jugador'

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