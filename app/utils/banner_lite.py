"""Copia LIVIANA del banner de la portada para el modo liviano.

Pedido del dueño: si el banner se mueve (video MP4, GIF o WebP animado), en modo liviano tiene que seguir viéndose moviéndose, pero comprimido para
que pese menos. Al subir el banner (y de vez en cuando en el proceso de fondo, por si ya estaba subido) se hace, junto al original, una copia más
chica: video MP4 a 480 px de ancho como máximo, 15 cuadros por segundo, sin sonido y con más compresión (necesita `ffmpeg` en el servidor; si no
está, el modo liviano usa el original); GIF / WebP animado a WebP animado de 480 px, con menos cuadros y más compresión.

La copia se llama `<nombre>.lite.mp4` / `<nombre>.lite.webp`. La portada la usa solo en modo liviano (ver templates/index.html y static/js/lite-detect.js).
Si la copia no queda más chica que el original, no se guarda y se usa el original.
"""
import os
import shutil
import subprocess
import threading

from flask import current_app

LITE_MAX_WIDTH = 480
LITE_FPS = 15
LITE_CRF = 34
FFMPEG_TIMEOUT_SECONDS = 150
MAX_ANIMATED_FRAMES = 600

_busy = set()      # copias que se están haciendo ahora mismo (no se lanzan dos a la vez)
_gave_up = set()   # (ruta, fecha, tamaño) de banners que no se pudieron comprimir o no valía la pena: no se reintentan hasta que cambie el archivo
_lock = threading.Lock()


def _root():
    return os.path.realpath(current_app.config.get('UPLOAD_FOLDER') or '')


def lite_name(rel):
    """'branding/x.mp4' -> 'branding/x.lite.mp4'; GIF/WebP -> 'branding/x.lite.webp'. '' si no es de los que se mueven (o si ya es una copia liviana)."""
    rel = (rel or '').strip()
    base, ext = os.path.splitext(rel)
    if not base or base.lower().endswith('.lite'):
        return ''
    ext = ext.lower()
    if ext == '.mp4':
        return base + '.lite.mp4'
    if ext in ('.gif', '.webp'):
        return base + '.lite.webp'
    return ''


def _inside(rel):
    """Ruta absoluta del archivo `rel` SI existe y está dentro de la carpeta de subidas (nada de salirse con ..); si no, ''."""
    root = _root()
    if not root or not rel or os.path.isabs(rel) or rel[:1] in ('/', chr(92)) or ':' in rel[:3]:
        return ''                                             # solo rutas relativas a la carpeta de subidas (como las guarda el ajuste del banner)
    path = os.path.realpath(os.path.join(root, rel))
    return path if path.startswith(root + os.sep) and os.path.isfile(path) else ''


def existing_lite(rel):
    """Ruta relativa de la copia liviana si ya existe y es posterior al original (barato: se usa al pintar la portada); si no, ''."""
    name = lite_name(rel)
    if not name:
        return ''
    src, dst = _inside(rel), _inside(name)
    if src and dst and os.path.getmtime(dst) >= os.path.getmtime(src):
        return name.replace(os.sep, '/')
    return ''


def _remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _compress_video(src, dst):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return False
    tmp = dst + '.tmp.mp4'
    cmd = [
        ffmpeg, '-y', '-loglevel', 'error', '-i', src, '-an',
        '-vf', "scale='min(%d,iw)':-2,fps=%d" % (LITE_MAX_WIDTH, LITE_FPS),
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', str(LITE_CRF), '-pix_fmt', 'yuv420p', '-movflags', '+faststart', tmp,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        _remove(tmp)
        return False
    if result.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
        _remove(tmp)
        return False
    os.replace(tmp, dst)
    return True


def _compress_animated_image(src, dst):
    try:
        from PIL import Image
    except ImportError:
        return False
    tmp = dst + '.tmp.webp'
    try:
        with Image.open(src) as image:
            frames = int(getattr(image, 'n_frames', 1) or 1)
            if frames <= 1 or frames > MAX_ANIMATED_FRAMES:
                return False                                   # una imagen quieta ya pesa poco: no hay nada que mover ni comprimir
            width, height = image.size
            scale = min(1.0, LITE_MAX_WIDTH / float(width or 1))
            size = (max(2, int(width * scale)), max(2, int(height * scale)))
            keep_every = 2 if frames > 30 else 1               # con muchos cuadros se deja uno sí y uno no (la mitad de cuadros por segundo)
            out, durations, carry = [], [], 0
            for index in range(frames):
                image.seek(index)
                carry += int(image.info.get('duration', 100) or 100)
                if index % keep_every:
                    continue
                out.append(image.convert('RGBA').resize(size, Image.LANCZOS))
                durations.append(carry)
                carry = 0
        out[0].save(tmp, format='WEBP', save_all=True, append_images=out[1:], duration=durations, loop=0, quality=45, method=4, minimize_size=True)
    except Exception:
        _remove(tmp)
        return False
    os.replace(tmp, dst)
    return True


def ensure_lite_banner(rel):
    """Hace la copia liviana del banner `rel` si hace falta y se puede. Devuelve su ruta relativa o ''. BLOQUEA mientras comprime: llamarla desde un
    hilo de fondo (ver ensure_async) o desde el proceso de fondo."""
    name = lite_name(rel)
    src = _inside(rel)
    if not name or not src:
        return ''
    if existing_lite(rel):
        return name.replace(os.sep, '/')
    root = _root()
    dst = os.path.realpath(os.path.join(root, name))
    if not dst.startswith(root + os.sep):
        return ''
    stamp = (rel, os.path.getmtime(src), os.path.getsize(src))
    with _lock:
        if stamp in _gave_up or name in _busy:
            return ''
        _busy.add(name)
    try:
        made = _compress_video(src, dst) if name.endswith('.mp4') else _compress_animated_image(src, dst)
        if made and os.path.getsize(dst) >= os.path.getsize(src):   # no quedó más liviana: se usa el original
            _remove(dst)
            made = False
        if not made:
            with _lock:
                _gave_up.add(stamp)
            return ''
        return name.replace(os.sep, '/')
    finally:
        with _lock:
            _busy.discard(name)


def ensure_async(app, rel):
    """Lo mismo, pero en un hilo aparte (no hace esperar a quien acaba de subir el banner)."""
    if not lite_name(rel):
        return

    def work():
        with app.app_context():
            try:
                ensure_lite_banner(rel)
            except Exception:
                pass

    threading.Thread(target=work, name='banner-lite', daemon=True).start()


def delete_lite(rel):
    """Borra la copia liviana de `rel` (cuando se cambia o se quita el banner)."""
    name = lite_name(rel)
    _remove(_inside(name) if name else '')


def ensure_for_current_banner():
    """Para el proceso de fondo: si el banner de la portada se mueve y todavía no tiene copia liviana, la hace."""
    from ..models import Setting

    row = Setting.query.filter_by(key='promo_banner_image').first()
    rel = ((row.value if row else '') or '').strip()
    return ensure_lite_banner(rel) if rel else ''
