"""Marcos del perfil: la tienda de marcos (los sube el administrador, son GRATIS para todas las cuentas) y los propios.

Un marco es una imagen (PNG, WEBP o GIF, también con movimiento) con el centro transparente que se pone encima de la foto de perfil o del
banner. Hay dos formas de tener uno: (1) elegir uno de la tienda de marcos y (2) subir uno propio (eso vive en `utils/profiles.py` y cuenta
dentro del tope de 5 imágenes del perfil).

Las cuentas son independientes de los IDs (decisión del dueño, 2026-10-14): los puntos de la tienda son de cada ID, así que los marcos ya
no se compran con puntos. La columna `price_points` y la tabla `user_frames` se quedan en la base de datos sin usarse.

Reglas:
  * La imagen de un marco se valida con Pillow y se guarda con nombre aleatorio; el nombre real del archivo nunca se usa.
"""
import io
import os
import re
import secrets

from flask import current_app

from ..models import ProfileFrame, db

FRAME_KINDS = ('avatar', 'banner')
FRAME_PATH_RE = re.compile(r'^profile_frames/frame_[0-9a-f]{12}\.(png|webp|gif)$')
ALLOWED_EXT = {'png', 'webp', 'gif'}
FORMAT_EXT = {'PNG': 'png', 'WEBP': 'webp', 'GIF': 'gif'}
# Peso máximo por tipo de marco (igual que los marcos propios del editor: foto 3 MB, portada 5 MB)
MAX_FRAME_BYTES_BY_KIND = {'avatar': 3 * 1024 * 1024, 'banner': 5 * 1024 * 1024}
MAX_FRAME_BYTES = max(MAX_FRAME_BYTES_BY_KIND.values())
MAX_FRAME_SIDE = 1600
MAX_PIXELS = 16_000_000
NAME_MAX = 60


def frame_url(path):
    """`uploads/profile_frames/...` para `url_for('static', filename=...)`, o '' si la ruta no tiene la forma que genera este módulo."""
    path = (path or '').strip()
    return f'uploads/{path}' if FRAME_PATH_RE.match(path) else ''


def _upload_root():
    return current_app.config['UPLOAD_FOLDER']


def save_frame_asset(file_storage, kind='avatar'):
    """Valida y guarda la imagen de un marco de la tienda. Devuelve (ruta_relativa, mensaje_de_error). Se guarda tal cual (para no perder
    el movimiento de un GIF o WEBP animado) pero solo después de comprobar con Pillow que es una imagen de verdad. El peso máximo depende
    del tipo: marco de foto 3 MB, marco de portada 5 MB."""
    max_bytes = MAX_FRAME_BYTES_BY_KIND.get(kind, MAX_FRAME_BYTES_BY_KIND['avatar'])
    filename = (getattr(file_storage, 'filename', '') or '').strip()
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    if ext not in ALLOWED_EXT:
        return None, 'El marco debe ser una imagen PNG, WEBP o GIF (con el centro transparente).'
    data = file_storage.stream.read(max_bytes + 1)
    if not data:
        return None, 'El archivo está vacío.'
    if len(data) > max_bytes:
        return None, f'El marco pesa demasiado: el máximo es {max_bytes // (1024 * 1024)} MB.'
    try:
        from PIL import Image, UnidentifiedImageError
        Image.open(io.BytesIO(data)).verify()
        image = Image.open(io.BytesIO(data))
        real_ext = FORMAT_EXT.get((image.format or '').upper())
        if not real_ext:
            return None, 'Ese archivo no es una imagen PNG, WEBP o GIF válida.'
        width, height = image.size
        if width < 16 or height < 16 or width * height > MAX_PIXELS or max(width, height) > MAX_FRAME_SIDE:
            return None, f'El marco debe medir entre 16 y {MAX_FRAME_SIDE} píxeles por lado.'
        folder = os.path.join(_upload_root(), 'profile_frames')
        os.makedirs(folder, exist_ok=True)
        rel = f'profile_frames/frame_{secrets.token_hex(6)}.{real_ext}'
        with open(os.path.join(_upload_root(), *rel.split('/')), 'wb') as handle:
            handle.write(data)
        return rel, ''
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None, 'Ese archivo no es una imagen válida.'
    except Exception:
        return None, 'No se pudo procesar la imagen.'


def delete_frame_asset(path):
    path = (path or '').strip()
    if not FRAME_PATH_RE.match(path):
        return
    try:
        os.remove(os.path.join(_upload_root(), *path.split('/')))
    except OSError:
        pass


def clean_frame_name(value):
    return ' '.join(str(value or '').split())[:NAME_MAX]


# ───────────────────────── Lo que puede usar una cuenta ─────────────────────────
def usable_frame(user, frame_id, kind):
    """El marco de la tienda con ese id si se puede usar (activo y del tipo pedido); si no, None. Todos son gratis."""
    try:
        frame_id = int(frame_id)
    except (TypeError, ValueError):
        return None
    if not 0 < frame_id < 2_147_483_647:    # un número gigante a mano no debe llegar a la base (daba un error en vez de "no existe")
        return None
    frame = db.session.get(ProfileFrame, frame_id)
    if frame is None or not frame.is_active or frame.kind != kind or not frame_url(frame.image_path):
        return None
    return frame


def selected_frame_url(profile, user, kind):
    """`uploads/...` del marco de la tienda que la cuenta eligió (si todavía se puede usar) o ''."""
    if profile is None or user is None:
        return ''
    frame_id = profile.avatar_frame_id if kind == 'avatar' else profile.banner_frame_id
    if not frame_id:
        return ''
    frame = usable_frame(user, frame_id, kind)
    return frame_url(frame.image_path) if frame else ''


def shop_frames(user, kind):
    """Marcos de la tienda de ese tipo, listos para el editor (todos gratis)."""
    rows = (
        ProfileFrame.query.filter_by(kind=kind, is_active=True)
        .order_by(ProfileFrame.sort_order.asc(), ProfileFrame.id.asc()).all()
    )
    result = []
    for frame in rows:
        url = frame_url(frame.image_path)
        if not url:
            continue
        result.append({'id': frame.id, 'name': frame.name, 'url': url})
    return result
