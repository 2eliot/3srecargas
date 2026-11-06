"""Vincular Google a una cuenta con usuario y contraseña (opcional).

Quien se registró con usuario y contraseña puede, desde "Mi cuenta → Mis datos → Google", vincular su cuenta de Google. Desde ese
momento "Continuar con Google" lo mete a SU cuenta de siempre (con su perfil, sus puntos y sus órdenes), y puede seguir entrando
con su usuario y contraseña como antes: Google es una forma más de entrar, no la única. Se puede desvincular cuando quiera.

Por qué solo se vincula desde adentro de la cuenta (con sesión abierta) y no solo por tener el mismo correo: el correo de una cuenta
registrada no se verifica al crearla. Si Google entrara solo con coincidir el correo, alguien podría registrarse antes con el
correo de otra persona y esa persona terminaría dentro de la cuenta que creó el otro. Vinculando desde adentro la persona demuestra
que es dueña de las dos cosas: de la cuenta y de ese Google.
"""
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from ..models import User, UserGoogleLink, db
from .auth_accounts import GOOGLE_SCOPE, find_scoped_customer

GOOGLE_ACCOUNT_KIND = 'google_email'


def clean_email(email):
    return (email or '').strip().lower()


def mask_email(email):
    """ab***@gmail.com (para mostrarlo sin dejarlo completo en pantalla)."""
    email = clean_email(email)
    if '@' not in email:
        return email[:2] + '***' if email else ''
    name, domain = email.split('@', 1)
    return f'{name[:2]}***@{domain}'


def link_for_user(user_id):
    return UserGoogleLink.query.filter_by(user_id=user_id).first()


def link_by_email(email):
    email = clean_email(email)
    return UserGoogleLink.query.filter_by(google_email=email).first() if email else None


def registered_accounts_with_email(email):
    """Cuentas con usuario y contraseña que usan ese correo (como correo de la cuenta o de contacto)."""
    email = clean_email(email)
    if not email:
        return []
    return User.query.filter(
        User.account_kind == 'registered',
        or_(db.func.lower(User.email) == email, db.func.lower(User.contact_email) == email),
    ).all()


def google_only_account(email):
    """La cuenta que se creó entrando solo con Google (sin usuario y contraseña) con ese correo, si existe."""
    return find_scoped_customer(GOOGLE_SCOPE, clean_email(email), GOOGLE_ACCOUNT_KIND)


def link_google(user, info):
    """Vincula el Google que acaba de comprobar google_login (`info`) a `user`. Devuelve (ok, mensaje para el cliente)."""
    email = clean_email(info.get('email'))
    if not email:
        return False, 'Google no entregó un correo. Intenta de nuevo.'
    if not getattr(user, 'is_registered_account', False) or not user.is_active:
        return False, 'Solo las cuentas con usuario y contraseña pueden vincular Google.'

    mine = link_for_user(user.id)
    if mine:
        if mine.google_email == email:
            return True, 'Esa cuenta de Google ya estaba vinculada a tu cuenta.'
        return False, 'Tu cuenta ya tiene otro Google vinculado. Desvincúlalo primero si quieres cambiarlo.'

    other = link_by_email(email)
    if other and other.user_id != user.id:
        return False, 'Esa cuenta de Google ya está vinculada a otra cuenta de la tienda.'

    # El correo de ese Google no puede ser el de OTRA cuenta registrada: no se le quita a nadie. (Si es el de la propia cuenta, sí.)
    if any(account.id != user.id for account in registered_accounts_with_email(email)):
        return False, 'Ese correo de Google ya pertenece a otra cuenta de la tienda. Escríbenos por soporte y te ayudamos.'

    # Si con ese Google ya se creó una cuenta aparte (entrando solo con Google), no se mezclan solas.
    if google_only_account(email):
        return False, 'Con ese Google ya se creó otra cuenta en la tienda. Escríbenos por soporte y te ayudamos a unirlas.'

    db.session.add(UserGoogleLink(user_id=user.id, google_email=email, google_sub=(info.get('sub') or '')[:64]))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return False, 'No se pudo vincular Google en este momento. Intenta de nuevo.'
    return True, 'Listo: tu cuenta quedó vinculada con Google. Ya puedes entrar con Google o con tu usuario y contraseña.'


def unlink_google(user):
    """Quita el Google vinculado. Devuelve True si había uno. La cuenta sigue entrando con su usuario y contraseña."""
    link = link_for_user(user.id)
    if not link:
        return False
    db.session.delete(link)
    db.session.commit()
    return True
