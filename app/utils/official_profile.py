"""Perfil OFICIAL de la tienda (el del administrador): una cuenta normal con usuario y nombre "3srecargas" que ya viene creada,
con el signo de verificado y la etiqueta "ADMIN" junto al nombre. El dueño la edita como cualquier perfil (entrando a la cuenta).

- Se crea sola al arrancar la web (`ensure_official_profile`, idempotente) con una contraseña al azar que nadie conoce: para
  entrar, el administrador usa Admin → Cuentas → "3srecargas" → Restablecer contraseña (la genera y se la muestra una vez).
- Se reconoce por el ajuste `official_profile_user_id` (el id de la cuenta), NO por el nombre: si el dueño le cambia el usuario,
  sigue siendo la oficial.
- No entra a los tops (`in_rankings` apagado); el dueño lo puede cambiar en "Editar perfil".
"""
import secrets
import time

from flask import g, has_app_context, session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from ..models import AdminUser, Setting, User, UserProfile, db
from .auth_accounts import ACCOUNT_KINDS, ACCOUNT_SCOPE

OFFICIAL_USERNAME = '3srecargas'
OFFICIAL_DISPLAY_NAME = '3srecargas'
OFFICIAL_SETTING_KEY = 'official_profile_user_id'
# Correo de relleno (la cuenta exige uno único). `.invalid` nunca existe, así que nada se envía a ese buzón.
OFFICIAL_EMAIL = '3srecargas@cuenta-oficial.invalid'
_CACHE_ATTR = '_official_user_id'


def _read_setting():
    row = Setting.query.filter_by(key=OFFICIAL_SETTING_KEY).first()
    try:
        return int(row.value) if row is not None and (row.value or '').strip() else None
    except ValueError:
        return None


def official_user_id():
    """Id de la cuenta oficial (o None si todavía no existe). Se lee una sola vez por petición."""
    if has_app_context() and hasattr(g, _CACHE_ATTR):
        return getattr(g, _CACHE_ATTR)
    uid = _read_setting()
    if has_app_context():
        setattr(g, _CACHE_ATTR, uid)
    return uid


def is_official_user(user):
    """True si `user` es la cuenta oficial. Acepta cualquier cosa (usuario de la tienda, admin, anónimo, None)."""
    try:
        if user is None or user.__class__.__name__ != 'User':
            return False
        uid = getattr(user, 'id', None)
        return uid is not None and uid == official_user_id()
    except Exception:
        return False


def visible_official_user():
    """La cuenta oficial si cualquiera puede abrir su perfil (activa, perfil público y no oculto por el admin); si no, None.
    La usa el botón "Ver perfil del admin" del Top: no debe llevar a una página que da 404."""
    uid = official_user_id()
    if not uid:
        return None
    user = db.session.get(User, uid)
    if user is None or not user.is_active or not user.has_chosen_username:
        return None
    profile = UserProfile.query.filter_by(user_id=uid).first()
    if profile is not None and (profile.moderation_hidden or profile.is_public is False):
        return None
    return user


def ensure_official_profile():
    """Crea la cuenta y el perfil oficiales si no existen (o los reconoce si ya existen). Devuelve el usuario, o None si otra
    copia de la web que arrancaba a la vez la está creando (la siguiente petición ya la encuentra)."""
    user = None
    uid = _read_setting()
    if uid:
        user = db.session.get(User, uid)
    if user is None:
        user = User.query.filter(func.lower(User.username) == OFFICIAL_USERNAME, User.account_kind.in_(ACCOUNT_KINDS)).first()

    if user is None:
        email = OFFICIAL_EMAIL
        if User.query.filter(func.lower(User.email) == email).first() is not None:
            email = f'{secrets.token_hex(4)}.{OFFICIAL_EMAIL}'
        user = User(
            username=OFFICIAL_USERNAME, email=email,
            # Contraseña al azar que nadie conoce: el dueño la restablece desde Admin → Cuentas cuando quiera entrar.
            password_hash=generate_password_hash(secrets.token_urlsafe(48)),
            account_scope=ACCOUNT_SCOPE, account_scope_label='Cuenta', account_identifier=email,
            account_identifier_normalized=email, account_kind='registered', is_active=True,
        )
        db.session.add(user)
        try:
            db.session.flush()
        except IntegrityError:
            # Otra copia de la web (varios procesos arrancan juntos) la creó un instante antes.
            db.session.rollback()
            return User.query.filter(func.lower(User.username) == OFFICIAL_USERNAME).first()

    profile = UserProfile.query.filter_by(user_id=user.id).first()
    if profile is None:
        db.session.add(UserProfile(
            user_id=user.id, display_name=OFFICIAL_DISPLAY_NAME, is_public=True, moderation_hidden=False, in_rankings=False,
        ))

    row = Setting.query.filter_by(key=OFFICIAL_SETTING_KEY).first()
    if row is None:
        db.session.add(Setting(key=OFFICIAL_SETTING_KEY, value=str(user.id), description='Cuenta del perfil oficial (el del administrador)'))
    elif (row.value or '').strip() != str(user.id):
        row.value = str(user.id)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return User.query.filter(func.lower(User.username) == OFFICIAL_USERNAME).first()
    if has_app_context() and hasattr(g, _CACHE_ATTR):
        delattr(g, _CACHE_ATTR)
    return user


# ─── Entrar al perfil oficial desde el panel y volver ────────────────────────
# El administrador toca "Perfil" en su menú y queda dentro de la cuenta oficial (sin contraseña, porque ya es el admin). La sesión del
# panel se cambia por la de la cuenta; se anota a quién volver (firmado en la cookie de sesión, nadie lo puede inventar) y con
# "Volver al panel de admin" se regresa sin escribir la clave. Si cierra sesión o pasan 12 horas, hay que entrar al panel de nuevo.
ADMIN_RETURN_KEY = 'admin_return'
ADMIN_RETURN_MAX_AGE = 12 * 3600


def remember_admin_return(admin_user):
    session[ADMIN_RETURN_KEY] = {'id': admin_user.get_id(), 'at': int(time.time())}


def clear_admin_return():
    session.pop(ADMIN_RETURN_KEY, None)


def admin_return_user(current):
    """El administrador al que se puede volver, o None. Solo vale si quien está dentro AHORA es la cuenta oficial y la marca no venció."""
    data = session.get(ADMIN_RETURN_KEY)
    if not isinstance(data, dict) or not is_official_user(current):
        return None
    try:
        at = int(data.get('at'))
    except (TypeError, ValueError):
        return None
    now = time.time()
    if now - at > ADMIN_RETURN_MAX_AGE or at > now + 60:
        return None
    kind, _, pk = str(data.get('id') or '').partition(':')
    if kind != 'admin' or not pk.isdigit():
        return None
    return db.session.get(AdminUser, int(pk))
