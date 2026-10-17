"""Lo premium de la tienda de puntos que una cuenta tiene puesto en su perfil: marcos, foto, portada y fondo.

Las cuentas CONSIGUEN estos artículos en /tiendapuntos (queda guardado en UserShopItem) y los PONEN desde "Editar mi perfil". Lo que tienen puesto se
anota en 5 columnas del perfil (ids de artículo). Al dibujar el perfil siempre se comprueba que la cuenta todavía lo tenga: si el administrador le
devuelve el canje, el perfil vuelve solo a lo propio, sin tocar nada más. Las fotos y los fondos de la tienda NO cuentan en el tope de 5 imágenes propias.
"""
from ..models import PointsShopItem, UserShopItem, db
from .shop_assets import asset_media_path, asset_url, is_video
from .profiles import safe_media_path

# Tipo de artículo -> columna del perfil donde se guarda el que tiene puesto.
EQUIP_COLUMN = {
    'frame_avatar': 'avatar_frame_item_id', 'frame_banner': 'banner_frame_item_id', 'avatar_photo': 'avatar_photo_item_id',
    'banner_photo': 'banner_photo_item_id', 'background': 'background_item_id',
}
CHOICE_PREFIX = 's'     # en los formularios, un artículo de la tienda de puntos se escribe "s<id>" (los números a secas son los marcos gratis de siempre)


def choice_value(item_id):
    return f'{CHOICE_PREFIX}{int(item_id)}'


def parse_choice(raw):
    """'s12' -> 12; cualquier otra cosa -> None."""
    value = (raw or '').strip().lower()
    digits = value[1:]
    # solo dígitos normales y de largo razonable: un número enorme o un dígito raro (como "²") no debe tumbar la petición
    if value[:1] == CHOICE_PREFIX and digits.isascii() and digits.isdigit() and len(digits) <= 9:
        return int(digits)
    return None


def owned_item(user_id, item_id, kind):
    """El artículo de ese tipo si la cuenta lo tiene en su colección (y tiene archivo); si no, None. Un artículo oculto de la tienda sigue sirviendo
    a quien ya lo consiguió."""
    if not user_id or not item_id:
        return None
    item = db.session.get(PointsShopItem, item_id)
    if item is None or item.kind != kind or not asset_media_path(item):
        return None
    if UserShopItem.query.filter_by(user_id=user_id, item_id=item.id).first() is None:
        return None
    return item


def equipped_item(profile, kind):
    """El artículo que el perfil tiene puesto de ese tipo, o None (no hay, ya no lo tiene o dejó de servir)."""
    if profile is None:
        return None
    return owned_item(profile.user_id, getattr(profile, EQUIP_COLUMN[kind], None), kind)


def equipped_path(profile, kind):
    """Ruta `profiles/0/...` del archivo del artículo puesto, o ''."""
    item = equipped_item(profile, kind)
    return asset_media_path(item) if item is not None else ''


def effective_avatar_path(profile):
    """La foto que se ve: la de la tienda si la tiene puesta; si no, la propia."""
    if profile is None:
        return ''
    return equipped_path(profile, 'avatar_photo') or (profile.avatar_path or '')


def effective_banner_path(profile):
    if profile is None:
        return ''
    return equipped_path(profile, 'banner_photo') or (profile.banner_path or '')


def effective_background_path(profile):
    if profile is None:
        return ''
    return equipped_path(profile, 'background') or (profile.background_path or '')


def frame_path(profile, kind):
    """Ruta del marco premium puesto ('avatar' o 'banner'), o ''."""
    return equipped_path(profile, 'frame_avatar' if kind == 'avatar' else 'frame_banner')


def owned_for_editor(user_id, kind):
    """Lo que la cuenta tiene de ese tipo, listo para el editor: [{'id', 'choice', 'name', 'url', 'video'}] (lo más reciente primero)."""
    rows = (
        db.session.query(PointsShopItem)
        .join(UserShopItem, UserShopItem.item_id == PointsShopItem.id)
        .filter(UserShopItem.user_id == user_id, PointsShopItem.kind == kind)
        .order_by(UserShopItem.acquired_at.desc(), UserShopItem.id.desc()).all()
    )
    result = []
    for item in rows:
        url = asset_url(item)
        if url:
            result.append({'id': item.id, 'choice': choice_value(item.id), 'name': item.name, 'url': url, 'video': is_video(item)})
    return result


def current_choice(profile, kind):
    """Qué tiene puesto el editor de ese tipo: 's<id>' si es de la tienda de puntos; si no, '' (lo propio / lo gratis)."""
    item = equipped_item(profile, kind)
    return choice_value(item.id) if item is not None else ''


def apply_choice(profile, user, kind, raw):
    """Guarda lo que eligió en el editor para ese tipo. `raw` = 's<id>' (uno de la tienda de puntos que la cuenta tiene) o cualquier otra cosa para
    dejar el artículo de la tienda y volver a lo propio. Un artículo que no es suyo no cambia nada. Devuelve True si quedó puesto uno de la tienda."""
    column = EQUIP_COLUMN[kind]
    item_id = parse_choice(raw)
    if item_id is None:
        setattr(profile, column, None)
        return False
    item = owned_item(user.id, item_id, kind)
    if item is None:
        return False
    setattr(profile, column, item.id)
    return True


def clear_equipped(user_id, item_id, kind):
    """Quita de un perfil el artículo que ya no es suyo (por ejemplo, si el administrador devolvió el canje)."""
    from ..models import UserProfile
    column = EQUIP_COLUMN.get(kind)
    if not column:
        return
    UserProfile.query.filter(UserProfile.user_id == user_id, getattr(UserProfile, column) == item_id).update({column: None}, synchronize_session=False)


def clear_all(profile):
    """Deja el perfil sin nada de la tienda de puntos puesto (al restablecer el perfil). La colección de la cuenta no se toca."""
    for column in EQUIP_COLUMN.values():
        setattr(profile, column, None)


__all__ = [
    'EQUIP_COLUMN', 'apply_choice', 'choice_value', 'clear_all', 'clear_equipped', 'current_choice', 'effective_avatar_path', 'effective_background_path',
    'effective_banner_path', 'equipped_item', 'equipped_path', 'frame_path', 'is_video', 'owned_for_editor', 'owned_item', 'parse_choice', 'safe_media_path',
]
