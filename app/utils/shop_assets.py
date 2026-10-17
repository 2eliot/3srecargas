"""Archivos de los artículos premium de la tienda de puntos (marcos, fondos y fotos para el perfil).

El administrador sube UNA imagen por artículo desde "Puntos por cuenta". Se guarda con el mismo cuidado que las imágenes del perfil (utils/profiles.py:
se comprueba con Pillow que sea una imagen de verdad, se achica a las medidas de su tipo, los GIF / WEBP animados conservan su movimiento y el fondo
admite un video corto) y con un nombre aleatorio. Se guarda en `uploads/profiles/0/` (la carpeta "0" no pertenece a ninguna cuenta), así la misma ruta
sirve para dibujarla en cualquier perfil sin permisos nuevos.
"""
from .profiles import delete_media, safe_media_path, save_media

# Tipo del artículo -> tipo de imagen del perfil con el que se valida y se achica.
MEDIA_KIND = {
    'frame_avatar': 'avatar_frame',     # marco de la foto de perfil (PNG/WEBP/GIF con el centro transparente, 3 MB)
    'frame_banner': 'banner_frame',     # marco de la portada (5 MB)
    'avatar_photo': 'avatar',           # foto de perfil lista (se recorta cuadrada, 3 MB)
    'banner_photo': 'banner',           # foto de portada lista (horizontal, 4 MB)
    'background': 'background',         # fondo de la página (imagen de 4 MB o un video de hasta 1 MB)
}

# Lo que se pide al admin según el tipo (para el formulario y los avisos).
ASSET_LABELS = {
    'frame_avatar': 'el marco de la foto', 'frame_banner': 'el marco de la portada', 'avatar_photo': 'la foto de perfil',
    'banner_photo': 'la foto de la portada', 'background': 'el fondo',
}


class AssetError(ValueError):
    """Un archivo que no se pudo guardar, con el mensaje listo para mostrar al administrador."""


def asset_media_path(item):
    """Ruta (`profiles/0/...`) del archivo del artículo, o '' si no tiene o no tiene la forma que genera este módulo."""
    return safe_media_path(getattr(item, 'asset_path', '') or '')


def asset_url(item):
    """`uploads/...` para `url_for('static', filename=...)`, o ''."""
    path = asset_media_path(item)
    return f'uploads/{path}' if path else ''


def is_video(item):
    return asset_media_path(item).rsplit('.', 1)[-1] in ('mp4', 'webm')


def save_item_asset(item, upload, remove=False):
    """Guarda el archivo subido en `item.asset_path` (y borra el anterior). Sin archivo nuevo se queda el que tenía; `remove` lo quita (el artículo
    no puede quedar sin archivo: se avisa). No hace commit. Lanza AssetError con el motivo si algo no sirve."""
    media_kind = MEDIA_KIND.get(item.kind)
    if media_kind is None:
        raise AssetError('Este tipo de artículo no lleva archivo.')
    has_upload = upload is not None and bool((getattr(upload, 'filename', '') or '').strip())
    if has_upload:
        saved, error = save_media(upload, media_kind, 0)
        if not saved:
            raise AssetError(error or 'No se pudo guardar el archivo.')
        old = item.asset_path
        item.asset_path = saved
        if old and old != saved:
            delete_media(old)
    elif remove and item.asset_path:
        delete_media(item.asset_path)
        item.asset_path = None
    if not item.asset_path:
        raise AssetError(f'Sube {ASSET_LABELS.get(item.kind, "el archivo")}: es lo que se desbloquea al canjear este artículo.')
