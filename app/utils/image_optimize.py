"""Reconvierte, una sola vez, las imágenes de fondo ya subidas que pesan de más.

El fondo de la portada llegó a ser un PNG de 1920x1080 de 1,2 MB: es más de la mitad de los bytes de la primera visita y en un teléfono con
datos lentos tarda casi un minuto en bajar (mientras tanto se ve negro). En WebP el mismo fondo pesa ~30 KB. Las subidas NUEVAS ya se
guardan así (routes/admin.py: _compress_and_save_image); esto arregla las que ya estaban.

Solo toca las claves de fondos de la tabla `settings`. El PNG original se CONSERVA en disco (por si hay que volver atrás) y solo se cambia
el nombre guardado en el ajuste. Si la imagen tiene transparencia real, no se toca.
"""
import os

from flask import current_app

from ..models import Setting, db

SETTING_KEYS = (
    'site_background_image', 'drawer_bg_recarga', 'drawer_bg_sorteo', 'drawer_bg_adivina', 'drawer_bg_hordas', 'drawer_bg_torneos',
    'drawer_bg_canjear', 'drawer_bg_mini', 'drawer_bg_login',
)
MIN_BYTES = 250 * 1024
MAX_SIDE = 1280
_done = set()   # (clave, valor) ya revisados en este proceso: no se vuelve a abrir la imagen cada 10 minutos


def _has_real_transparency(image):
    try:
        if image.mode in ('RGBA', 'LA'):
            return image.getchannel('A').getextrema()[0] < 250
        if image.mode in ('P', 'PA'):
            return 'transparency' in image.info or image.mode == 'PA'
    except Exception:
        return True
    return False


def optimize_branding_images():
    """Devuelve cuántas imágenes se reconvirtieron."""
    # De paso: si el banner de la portada se mueve y todavía no tiene su copia liviana (modo liviano), se hace ahora.
    try:
        from .banner_lite import ensure_for_current_banner
        ensure_for_current_banner()
    except Exception:
        pass
    root = os.path.realpath(current_app.config.get('UPLOAD_FOLDER') or '')
    if not root or not os.path.isdir(root):
        return 0
    converted = 0
    for key in SETTING_KEYS:
        row = Setting.query.filter_by(key=key).first()
        value = ((row.value if row else '') or '').strip()
        if not value.lower().endswith('.png') or (key, value) in _done:
            continue
        _done.add((key, value))
        source = os.path.realpath(os.path.join(root, value))
        if not source.startswith(root + os.sep) or not os.path.isfile(source) or os.path.getsize(source) < MIN_BYTES:
            continue
        target = source[:-4] + '.webp'
        try:
            from PIL import Image, ImageOps

            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image)
                if _has_real_transparency(image):
                    continue
                if max(image.size) > MAX_SIDE:
                    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
                image.convert('RGB').save(target, format='WEBP', quality=82, method=6)
            if os.path.getsize(target) >= os.path.getsize(source):
                os.remove(target)
                continue
            row.value = value[:-4] + '.webp'
            db.session.commit()
            converted += 1
        except Exception:
            db.session.rollback()
            try:
                if os.path.exists(target):
                    os.remove(target)
            except OSError:
                pass
    return converted
