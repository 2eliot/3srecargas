"""Fondos de pantalla de la web (Admin → Configuración → Imágenes y fondos → Fondos de pantalla) con VIDEO.

Cada fondo (la web y cada página especial) puede ser una imagen o, desde 2026-10-14 (pedido del dueño), un video corto MP4 o WEBM de hasta 1 MB.
Aquí viven las reglas: cómo se reconoce un video, cómo se valida lo que sube el admin (por su CONTENIDO, no solo por la extensión) y cómo se reparte lo
guardado entre "imagen" y "video" para las plantillas. No hay ffmpeg en el servidor: el video se guarda tal cual, con nombre propio.
"""
import os
import secrets
from datetime import datetime

from flask import current_app

from .profiles import BACKGROUND_VIDEO_MAX_BYTES, VIDEO_EXT, _valid_mp4, _valid_webm

MAX_MB = BACKGROUND_VIDEO_MAX_BYTES // (1024 * 1024)


def is_video(path):
    """True si lo guardado en un ajuste de fondo es un video (por la extensión que puso el propio servidor al guardarlo)."""
    return bool(path) and str(path).rsplit('.', 1)[-1].lower() in VIDEO_EXT


def split_bg(path):
    """(imagen, video) de un fondo guardado: solo uno de los dos viene con valor, el otro es ''."""
    path = (path or '').strip()
    return ('', path) if is_video(path) else (path, '')


def wants_video(file_storage):
    """True si el archivo que se está subiendo parece un video por su nombre (lo decide el nombre; lo que de verdad es se comprueba al guardar)."""
    name = (getattr(file_storage, 'filename', '') or '').strip().lower()
    return name.rsplit('.', 1)[-1] in VIDEO_EXT if '.' in name else False


def save_background_video(file_storage, subfolder='branding'):
    """Valida y guarda el video de un fondo. Devuelve (ruta_relativa, mensaje_de_error): con error, la ruta es None."""
    name = (getattr(file_storage, 'filename', '') or '').strip().lower()
    ext = name.rsplit('.', 1)[-1] if '.' in name else ''
    if ext not in VIDEO_EXT:
        return None, 'El fondo en video debe ser un archivo MP4 o WEBM.'
    data = file_storage.stream.read(BACKGROUND_VIDEO_MAX_BYTES + 1)
    if not data:
        return None, 'El video está vacío.'
    if len(data) > BACKGROUND_VIDEO_MAX_BYTES:
        return None, f'El video pesa más de {MAX_MB} MB: el máximo es {MAX_MB} MB. Usa uno más corto o más liviano.'
    if len(data) >= 12 and data[4:8] == b'ftyp':
        real, ok = 'mp4', _valid_mp4(data)
    elif data[:4] == b'\x1a\x45\xdf\xa3':
        real, ok = 'webm', _valid_webm(data)
    else:
        return None, 'Ese archivo no es un video válido.'
    if not ok or real != ext:
        return None, f'Ese archivo no es un video {ext.upper()} válido.'
    folder = os.path.join(current_app.config['UPLOAD_FOLDER'], subfolder)
    os.makedirs(folder, exist_ok=True)
    filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}_{secrets.token_hex(4)}.{real}"
    with open(os.path.join(folder, filename), 'wb') as handle:
        handle.write(data)
    return f'{subfolder}/{filename}', ''
