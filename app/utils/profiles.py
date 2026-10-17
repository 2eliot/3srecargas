"""Perfil público personalizable (estilo Discord): reglas de lo que el dueño puede cambiar y cómo se guarda.

Todo lo que viene del formulario se VALIDA aquí con listas permitidas y rangos (colores #rrggbb, números acotados, estilos
de una lista): nada del usuario llega al CSS ni al HTML sin pasar por estas funciones. Las imágenes se vuelven a
guardar con Pillow (se descartan metadatos y archivos que no son imágenes de verdad) con nombre aleatorio.
"""
import io
import json
import os
import re
import secrets
import time

from flask import current_app

from ..models import db, UserProfile

HEX_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
PROFILE_PATH_RE = re.compile(r'^profiles/\d+/[a-z_]+_[0-9a-f]{12}\.(webp|gif|mp4|webm)$')
CONTROL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]')

DISPLAY_NAME_MAX = 32
BIO_MAX = 300
STATUS_MAX = 60
BIO_MAX_LINES = 8

AVATAR_SHAPES = ('circle', 'squircle')
RING_STYLES = ('none', 'solid', 'gradient', 'conic', 'rainbow', 'double', 'dashed')
RING_ANIMS = ('none', 'spin', 'pulse')
BANNER_FRAME_STYLES = ('none', 'solid', 'double', 'neon', 'gradient', 'corners')
BG_MODES = ('gradient', 'color', 'image', 'banner')
NAME_FONTS = {
    'outfit': "'Outfit', sans-serif",
    'space': "'Archivo Black', 'Arial Black', sans-serif",   # la clave 'space' se conserva (lo ya guardado); la letra ahora es Archivo Black
    'cinzel': "'Cinzel', serif",
    'orbitron': "'Orbitron', sans-serif",
    'pacifico': "'Pacifico', cursive",
}

DEFAULT_THEME = {
    'accent': '#e11d2e',
    'name_color': '#ffffff',
    'font': 'outfit',
    'avatar': {
        'shape': 'circle', 'ring_style': 'gradient', 'ring_c1': '#ff1a2e', 'ring_c2': '#7a0010', 'ring_width': 4,
        'ring_glow': 10, 'ring_anim': 'none', 'ov_scale': 140, 'ov_x': 0, 'ov_y': 0,
        # true = el aro toma los colores del nivel del mes (Bronce, Plata, Oro, Diamante, Leyenda) en lugar de los de arriba
        'ring_auto': False,
    },
    'banner': {
        'c1': '#3a0a12', 'c2': '#0b0b10', 'angle': 135, 'frame_style': 'none', 'frame_c1': '#e5e7eb',
        'frame_c2': '#6b7280', 'frame_width': 3, 'frame_radius': 16, 'frame_glow': 8,
        # % del alto de la portada que el marco (imagen) de la portada SOBRESALE por arriba y por abajo (0 = justo encima, como siempre)
        'ov_extra': 0,
    },
    'background': {'mode': 'gradient', 'c1': '#1a0508', 'c2': '#000000', 'angle': 160, 'dim': 35, 'blur': 0},
    # El perfil NO muestra IDs, compras, juegos, redes sociales ni comentarios (regla de la tienda: la competencia podría
    # sacar información de los clientes que más compran). Lo único opcional es la fecha de registro.
    'show': {'since': True},
}

# (sección, clave, tipo, permitido)  — tipo: 'hex' | 'choice' | 'int' ; 'choice' usa la tupla, 'int' usa (mín, máx)
THEME_SPEC = [
    (None, 'accent', 'hex', None),
    (None, 'name_color', 'hex', None),
    (None, 'font', 'choice', tuple(NAME_FONTS)),
    ('avatar', 'shape', 'choice', AVATAR_SHAPES),
    ('avatar', 'ring_style', 'choice', RING_STYLES),
    ('avatar', 'ring_c1', 'hex', None),
    ('avatar', 'ring_c2', 'hex', None),
    ('avatar', 'ring_width', 'int', (0, 12)),
    ('avatar', 'ring_glow', 'int', (0, 30)),
    ('avatar', 'ring_anim', 'choice', RING_ANIMS),
    ('avatar', 'ov_scale', 'int', (100, 220)),
    ('avatar', 'ov_x', 'int', (-40, 40)),
    ('avatar', 'ov_y', 'int', (-40, 40)),
    ('banner', 'c1', 'hex', None),
    ('banner', 'c2', 'hex', None),
    ('banner', 'angle', 'int', (0, 360)),
    ('banner', 'frame_style', 'choice', BANNER_FRAME_STYLES),
    ('banner', 'frame_c1', 'hex', None),
    ('banner', 'frame_c2', 'hex', None),
    ('banner', 'frame_width', 'int', (0, 10)),
    ('banner', 'frame_radius', 'int', (0, 32)),
    ('banner', 'frame_glow', 'int', (0, 30)),
    ('banner', 'ov_extra', 'int', (0, 50)),
    ('background', 'mode', 'choice', BG_MODES),
    ('background', 'c1', 'hex', None),
    ('background', 'c2', 'hex', None),
    ('background', 'angle', 'int', (0, 360)),
    ('background', 'dim', 'int', (0, 90)),
    ('background', 'blur', 'int', (0, 20)),
]
SHOW_KEYS = ('since',)

# Nombre del campo del formulario para cada valor: "avatar_ring_style", "banner_frame_c1", "accent"...
def field_name(section, key):
    return key if section is None else f'{section}_{key}'


def _clean_value(kind, allowed, raw, default):
    # Lo guardado en la base o mandado por un formulario manipulado puede traer cualquier tipo (número, lista...): se pasa a texto.
    if kind == 'hex':
        value = ('' if raw is None else str(raw)).strip()
        return value.lower() if HEX_RE.match(value) else default
    if kind == 'choice':
        value = ('' if raw is None else str(raw)).strip().lower()
        return value if value in allowed else default
    if kind == 'int':
        try:
            number = int(float(str(raw).strip()))
        except (TypeError, ValueError):
            return default
        return max(allowed[0], min(allowed[1], number))
    return default


def _deep_default():
    return json.loads(json.dumps(DEFAULT_THEME))


def sanitize_stored_theme(data):
    """Une lo guardado con los valores por defecto y descarta cualquier cosa fuera de lo permitido."""
    theme = _deep_default()
    if not isinstance(data, dict):
        return theme
    for section, key, kind, allowed in THEME_SPEC:
        source = data if section is None else (data.get(section) if isinstance(data.get(section), dict) else {})
        target = theme if section is None else theme[section]
        if key in source:
            target[key] = _clean_value(kind, allowed, source[key], target[key])
    avatar_in = data.get('avatar') if isinstance(data.get('avatar'), dict) else {}
    theme['avatar']['ring_auto'] = bool(avatar_in.get('ring_auto')) is True
    show = data.get('show') if isinstance(data.get('show'), dict) else {}
    for key in SHOW_KEYS:
        if key in show:
            theme['show'][key] = bool(show[key])
    return theme


def load_theme(profile):
    if profile is None or not (profile.config_json or '').strip():
        return _deep_default()
    try:
        return sanitize_stored_theme(json.loads(profile.config_json))
    except (ValueError, TypeError):
        return _deep_default()


def theme_from_form(form, base):
    """Lee el formulario del editor: cada campo inválido o ausente conserva el valor que ya tenía."""
    theme = json.loads(json.dumps(base))
    for section, key, kind, allowed in THEME_SPEC:
        name = field_name(section, key)
        if name not in form:
            continue
        target = theme if section is None else theme[section]
        target[key] = _clean_value(kind, allowed, form.get(name), target[key])
    if form.get('__show_fields') is not None or any(f'show_{k}' in form for k in SHOW_KEYS):
        for key in SHOW_KEYS:
            theme['show'][key] = form.get(f'show_{key}') in ('1', 'on', 'true')
        theme['avatar']['ring_auto'] = form.get('avatar_ring_auto') in ('1', 'on', 'true')
    return theme


def clean_text(value, max_len, multiline=False):
    value = CONTROL_RE.sub('', str(value or '').replace('\r\n', '\n').replace('\r', '\n'))
    if multiline:
        lines = [' '.join(line.split()) for line in value.split('\n')]
        # sin líneas vacías seguidas ni arriba/abajo
        compact = []
        for line in lines:
            if line or (compact and compact[-1]):
                compact.append(line)
        value = '\n'.join(compact).strip('\n')
        value = '\n'.join(value.split('\n')[:BIO_MAX_LINES])
    else:
        value = ' '.join(value.split())
    return value[:max_len].strip()


def get_profile(user):
    return UserProfile.query.filter_by(user_id=user.id).first()


def get_or_create_profile(user):
    profile = get_profile(user)
    if profile is None:
        profile = UserProfile(user_id=user.id, is_public=True, moderation_hidden=False)
        db.session.add(profile)
        db.session.flush()
    return profile


def effective_avatar(theme, level_key=None):
    """Los ajustes del aro/foto. Los aros de colores y degradados ya no se ofrecen (pedido del dueño: los marcos son imágenes, de la tienda o propios):
    lo que quedó guardado se IGNORA y solo se dibuja el aro automático del nivel del mes, si el dueño lo activó y tiene nivel."""
    avatar = dict(theme['avatar'])
    avatar.update({'ring_style': 'none', 'ring_anim': 'none', 'ring_width': 0, 'ring_glow': 0})
    if avatar.get('ring_auto') and level_key:
        from .profile_social import level_ring
        ring = level_ring(level_key)
        if ring:
            avatar.update(ring)
    return avatar


def theme_vars(theme, media=None, level_key=None):
    """Variables CSS (texto listo para `style=""`) que dibujan el perfil. Solo valores ya validados; `media` lleva las
    direcciones (ya armadas por el servidor) del fondo y del banner, si el dueño subió imágenes."""
    av, bn, bg = effective_avatar(theme, level_key), theme['banner'], theme['background']
    no_ring = av['ring_style'] == 'none'
    values = {
        '--pf-accent': theme['accent'],
        '--pf-name': theme['name_color'],
        '--pf-font': NAME_FONTS.get(theme['font'], NAME_FONTS['outfit']),
        '--av-c1': av['ring_c1'], '--av-c2': av['ring_c2'],
        '--av-w': '0px' if no_ring else f"{av['ring_width']}px", '--av-glow': '0px' if no_ring else f"{av['ring_glow']}px",
        '--av-ov-scale': str(av['ov_scale'] / 100), '--av-ov-x': f"{av['ov_x']}%", '--av-ov-y': f"{av['ov_y']}%",
        '--bn-c1': bn['c1'], '--bn-c2': bn['c2'], '--bn-angle': f"{bn['angle']}deg",
        '--bn-fc1': bn['frame_c1'], '--bn-fc2': bn['frame_c2'],
        '--bn-fw': '0px',      # los marcos de línea/color del banner ya no se ofrecen: el marco es una imagen
        '--bn-fr': f"{bn['frame_radius']}px", '--bn-fglow': '0px',
        # cuánto sobresale el marco de la portada (fracción del alto de la portada, 0 a .5): solo cuenta si hay un marco puesto
        '--bn-ov-extra': str(round(bn.get('ov_extra', 0) / 100, 2)) if (media or {}).get('banner_frame') else '0',
        '--bg-c1': bg['c1'], '--bg-c2': bg['c2'], '--bg-angle': f"{bg['angle']}deg", '--bg-dim': str(bg['dim'] / 100),
        '--bg-blur': f"{bg['blur']}px",
    }
    media = media or {}
    for var, key in (('--bg-image', 'background'), ('--bn-image', 'banner')):
        path = safe_media_path(media.get(key) or '')
        if path and not is_video_path(path):
            values[var] = f"url('/static/uploads/{path}')"
    return '; '.join(f'{name}: {value}' for name, value in values.items())


def theme_attrs(theme, level_key=None, banner_frame=False):
    av = effective_avatar(theme, level_key)
    return {
        'data-shape': av['shape'], 'data-ring': av['ring_style'], 'data-ring-anim': av['ring_anim'], 'data-level': level_key or '',
        'data-frame': 'none', 'data-bg': theme['background']['mode'],
        # "on" = el marco de la portada sobresale (hay marco puesto y la barra "Cuánto sobresale" > 0)
        'data-bn-pop': 'on' if (banner_frame and theme['banner'].get('ov_extra', 0) > 0) else 'off',
    }


def safe_media_path(path):
    """Ruta de imagen del perfil lista para `url_for('static', filename='uploads/' + ruta)`, o '' si no tiene la forma
    que genera este módulo (nunca se arma una URL con algo que no sea nuestro)."""
    path = (path or '').strip()
    return path if PROFILE_PATH_RE.match(path) else ''


def is_video_path(path):
    """True si lo guardado en el perfil es un VIDEO (el fondo del perfil puede ser un MP4 o WEBM liviano)."""
    safe = safe_media_path(path)
    return bool(safe) and safe.rsplit('.', 1)[-1] in VIDEO_EXT


# ───────────────────────── Imágenes ─────────────────────────
ALLOWED_IMAGE_EXT = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
# El FONDO del perfil también acepta un video corto (MP4 o WEBM) de hasta 1 MB (pedido del dueño). No hay ffmpeg en el servidor para recodificarlo: se comprueba
# que el archivo sea de verdad un MP4 / WEBM bien formado y se guarda tal cual, con nombre aleatorio.
VIDEO_EXT = ('mp4', 'webm')
BACKGROUND_VIDEO_MAX_BYTES = 1 * 1024 * 1024
MAX_PIXELS = 16_000_000
MEDIA_KINDS = {
    # tipo: (columna del perfil, tope en bytes, lado máximo en px, recorte cuadrado, conserva transparencia)
    'avatar': ('avatar_path', 3 * 1024 * 1024, 512, True, True),
    'banner': ('banner_path', 4 * 1024 * 1024, 1400, False, False),
    'background': ('background_path', 4 * 1024 * 1024, 1920, False, False),
    'avatar_frame': ('avatar_frame_path', 3 * 1024 * 1024, 1024, False, True),   # marco de la foto: hasta 3 MB (pedido del dueño)
    'banner_frame': ('banner_frame_path', 5 * 1024 * 1024, 1600, False, True),   # marco del banner: hasta 5 MB (pedido del dueño)
}
AWARD_KIND = ('image_path', 1 * 1024 * 1024, 512, False, True)   # imagen de un premio de la vitrina (lo carga el admin)
MEDIA_LABELS = {
    'avatar': 'La foto de perfil', 'banner': 'El banner', 'background': 'El fondo',
    'avatar_frame': 'El marco de la foto', 'banner_frame': 'El marco del banner', 'award': 'La imagen del premio',
}
# Un perfil puede tener como máximo 5 imágenes propias subidas en total (foto, marco de la foto, banner, marco del banner y fondo: un espacio
# por cada tipo de MEDIA_KINDS, el dueño pidió que los cinco se puedan llenar); los marcos de la tienda no cuentan.
MAX_PROFILE_IMAGES = 5


def profile_image_count(profile):
    """Cuántas imágenes propias tiene ya subidas el perfil (de los 5 tipos de MEDIA_KINDS)."""
    if profile is None:
        return 0
    return sum(1 for column, *_rest in MEDIA_KINDS.values() if (getattr(profile, column, '') or '').strip())


def _upload_root():
    return current_app.config['UPLOAD_FOLDER']


def delete_media(relative_path):
    path = safe_media_path(relative_path)
    if not path:
        return
    full = os.path.join(_upload_root(), *path.split('/'))
    try:
        os.remove(full)
    except OSError:
        pass
    # si la carpeta del usuario quedó vacía, se quita
    try:
        os.rmdir(os.path.dirname(full))
    except OSError:
        pass


def save_media(file_storage, kind, user_id):
    """Valida y guarda una imagen del perfil. Devuelve (ruta_relativa, mensaje_de_error)."""
    spec = AWARD_KIND if kind == 'award' else MEDIA_KINDS.get(kind)
    if spec is None:
        return None, 'Tipo de imagen no válido.'
    _column, max_bytes, max_side, square, keep_alpha = spec
    label = MEDIA_LABELS[kind]
    filename = (getattr(file_storage, 'filename', '') or '').strip()
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    if kind == 'background' and ext in VIDEO_EXT:
        return _save_background_video(file_storage, ext, user_id, label)
    if ext not in ALLOWED_IMAGE_EXT:
        if kind == 'background':
            return None, f'{label}: usa una imagen PNG, JPG, WEBP o GIF, o un video MP4 o WEBM de hasta 1 MB.'
        return None, f'{label}: usa una imagen PNG, JPG, WEBP o GIF.'
    data = file_storage.stream.read(max_bytes + 1)
    if not data:
        return None, f'{label}: el archivo está vacío.'
    if len(data) > max_bytes:
        return None, f'{label} pesa demasiado: el máximo es {max_bytes // (1024 * 1024)} MB.'
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
        probe = Image.open(io.BytesIO(data))
        probe.verify()
        image = Image.open(io.BytesIO(data))
        fmt = (image.format or '').upper()
        if fmt not in ('PNG', 'JPEG', 'WEBP', 'GIF'):
            return None, f'{label}: ese archivo no es una imagen válida.'
        width, height = image.size
        if width < 1 or height < 1 or width * height > MAX_PIXELS:
            return None, f'{label}: la imagen es demasiado grande.'
        n_frames = getattr(image, 'n_frames', 1)
        animated = n_frames > 1   # GIF, WEBP animado o PNG animado (APNG)
        folder_rel = f'profiles/{int(user_id)}'
        os.makedirs(os.path.join(_upload_root(), *folder_rel.split('/')), exist_ok=True)
        token = secrets.token_hex(6)
        if animated and keep_alpha_or_small(kind):
            if n_frames > MAX_ANIM_FRAMES:
                return None, f'{label}: la animación tiene demasiados cuadros ({n_frames}; el máximo es {MAX_ANIM_FRAMES}). Usa una más corta.'
            if fmt in ('GIF', 'WEBP') and max(width, height) <= max_side:
                # Un GIF o WEBP animado que ya cabe en las medidas se guarda TAL CUAL (conserva todo: cuadros, velocidad y calidad). Antes de
                # guardarlo se comprueba que de verdad se puede leer hasta el último cuadro.
                if width * height * n_frames > MAX_ANIM_KEEP_PIXELS:
                    return None, f'{label}: la animación es demasiado pesada ({n_frames} cuadros de {width} × {height}). Usa una más chica o con menos cuadros.'
                # Un GIF puede traer cuadros más grandes que el primero (Pillow agranda el lienzo al leerlos): antes de decodificar nada se
                # comprueba, cuadro por cuadro, que todos caben en las medidas ya aceptadas. Si no, se rechaza.
                if not _frames_stay_inside(image, n_frames, width, height):
                    return None, f'{label}: la animación tiene cuadros de distinto tamaño. Expórtala de nuevo con todos los cuadros iguales.'
                image.seek(n_frames - 1)
                image.load()
                rel = f'{folder_rel}/{kind}_{token}.{"gif" if fmt == "GIF" else "webp"}'
                with open(os.path.join(_upload_root(), *rel.split('/')), 'wb') as handle:
                    handle.write(data)
                return rel, ''
            # APNG o una animación más grande que el máximo: se achica cuadro por cuadro y se guarda como WEBP animado
            # (antes se guardaba SOLO el primer cuadro y el movimiento se perdía al guardar).
            return _save_animated_webp(image, n_frames, kind, spec, f'{folder_rel}/{kind}_{token}.webp')
        if animated:
            image.seek(0)
        image = ImageOps.exif_transpose(image)
        has_alpha = image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info)
        if keep_alpha and has_alpha:
            image = image.convert('RGBA')
        else:
            image = image.convert('RGB')
        if square:
            side = min(image.size)
            left, top = (image.size[0] - side) // 2, (image.size[1] - side) // 2
            image = image.crop((left, top, left + side, top + side))
        if max(image.size) > max_side:
            image.thumbnail((max_side, max_side), Image.LANCZOS)
        rel = f'{folder_rel}/{kind}_{token}.webp'
        image.save(os.path.join(_upload_root(), *rel.split('/')), format='WEBP', quality=88 if keep_alpha else 82, method=6)
        return rel, ''
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None, f'{label}: ese archivo no es una imagen válida.'
    except Exception:  # cualquier otra falla de Pillow: no se guarda nada
        return None, f'{label}: no se pudo procesar la imagen.'


def _valid_mp4(data):
    """True si `data` es un MP4 bien formado: cajas una tras otra que cubren el archivo entero, empieza con `ftyp` y trae `moov` (los datos del video) y `mdat`
    (la imagen). No prueba el códec, pero descarta texto, páginas web, ejecutables y archivos cortados disfrazados de video."""
    pos, total, seen = 0, len(data), []
    while pos + 8 <= total:
        size = int.from_bytes(data[pos:pos + 4], 'big')
        box = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > total:
                return False
            size = int.from_bytes(data[pos + 8:pos + 16], 'big')
            header = 16
        elif size == 0:
            size = total - pos
        if size < header or pos + size > total or not all(32 <= b < 127 or b == 0xA9 for b in box):
            return False
        seen.append(box)
        pos += size
    return pos == total and bool(seen) and seen[0] == b'ftyp' and b'moov' in seen and b'mdat' in seen


def _valid_webm(data):
    """True si `data` empieza como un WEBM (cabecera EBML de Matroska con el tipo "webm") y trae el segmento con los datos."""
    return len(data) > 64 and data[:4] == b'\x1a\x45\xdf\xa3' and b'webm' in data[:64] and b'\x18\x53\x80\x67' in data[:4096]


def _save_background_video(file_storage, ext, user_id, label):
    """Guarda el video del fondo del perfil. Devuelve (ruta_relativa, mensaje_de_error). Tope: 1 MB."""
    data = file_storage.stream.read(BACKGROUND_VIDEO_MAX_BYTES + 1)
    if not data:
        return None, f'{label}: el archivo está vacío.'
    if len(data) > BACKGROUND_VIDEO_MAX_BYTES:
        return None, f'{label}: el video pesa demasiado, el máximo es 1 MB. Usa uno más corto o más liviano.'
    # el tipo REAL lo decide el contenido, no la extensión que trae el nombre
    if len(data) >= 12 and data[4:8] == b'ftyp':
        real, ok = 'mp4', _valid_mp4(data)
    elif data[:4] == b'\x1a\x45\xdf\xa3':
        real, ok = 'webm', _valid_webm(data)
    else:
        return None, f'{label}: ese archivo no es un video válido.'
    if not ok or real != ext:
        return None, f'{label}: ese archivo no es un video {ext.upper()} válido.'
    folder_rel = f'profiles/{int(user_id)}'
    os.makedirs(os.path.join(_upload_root(), *folder_rel.split('/')), exist_ok=True)
    rel = f'{folder_rel}/background_{secrets.token_hex(6)}.{real}'
    with open(os.path.join(_upload_root(), *rel.split('/')), 'wb') as handle:
        handle.write(data)
    return rel, ''


# Límites de una animación que se vuelve a codificar (protegen la memoria del servidor: cada cuadro decodificado pesa ancho × alto × 4 bytes).
MAX_ANIM_FRAMES = 300
MAX_ANIM_PIXELS = 64_000_000          # al volver a codificar: cuadros × ancho × alto con el tamaño final (cada cuadro en memoria = ancho × alto × 4 bytes)
MAX_ANIM_KEEP_PIXELS = 160_000_000    # al guardar el archivo tal cual no se decodifica todo junto: el tope es más alto
ANIM_QUALITIES = (85, 70, 55)         # se baja la calidad solo si la animación no cabe en el peso máximo


MAX_ANIM_SOURCE_PIXELS = 120_000_000   # cuadros × ancho × alto del ORIGINAL: lo que hay que decodificar y achicar (antes solo se medía el tamaño final)
MAX_ANIM_SECONDS = 20                  # tiempo máximo (CPU de la petición) para volver a codificar una animación


def _frames_stay_inside(image, n_frames, width, height):
    """True si todos los cuadros de la animación caben en el lienzo (width × height) del primero."""
    try:
        for index in range(n_frames):
            image.seek(index)
            if image.size != (width, height):
                return False
            for tile in (image.tile or ()):
                box = tile[1]
                if box[2] > width or box[3] > height:
                    return False
        image.seek(0)
    except (EOFError, ValueError, OSError):
        return False
    return True


def _save_animated_webp(image, n_frames, kind, spec, rel):
    """Guarda una animación (WEBP animado, APNG o GIF grande) como WEBP animado: cada cuadro se achica al lado máximo (y se recorta cuadrado
    en la foto), conserva la transparencia y la velocidad de cada cuadro, y se repite sin parar. Devuelve (ruta_relativa, mensaje_de_error)."""
    from PIL import Image
    _column, max_bytes, max_side, square, keep_alpha = spec
    label = MEDIA_LABELS[kind]
    width, height = image.size
    box = (0, 0, width, height)
    if square:
        side = min(width, height)
        box = ((width - side) // 2, (height - side) // 2, (width - side) // 2 + side, (height - side) // 2 + side)
        width = height = side
    scale = min(1.0, max_side / max(width, height))
    out_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    if (n_frames > MAX_ANIM_FRAMES or out_size[0] * out_size[1] * n_frames > MAX_ANIM_PIXELS
            or image.size[0] * image.size[1] * n_frames > MAX_ANIM_SOURCE_PIXELS):
        return None, f'{label}: la animación es demasiado pesada ({n_frames} cuadros). Usa una con menos cuadros o más chica.'
    if not _frames_stay_inside(image, n_frames, image.size[0], image.size[1]):
        return None, f'{label}: la animación tiene cuadros de distinto tamaño. Expórtala de nuevo con todos los cuadros iguales.'
    started = time.monotonic()
    frames, durations = [], []
    for index in range(n_frames):
        if time.monotonic() - started > MAX_ANIM_SECONDS:
            return None, f'{label}: la animación tardó demasiado en procesarse. Usa una más corta o más chica.'
        image.seek(index)
        frame = image.convert('RGBA' if keep_alpha else 'RGB')
        if square:
            frame = frame.crop(box)
        if frame.size != out_size:
            frame = frame.resize(out_size, Image.LANCZOS)
        frames.append(frame)
        delay = int(image.info.get('duration') or 0)
        durations.append(delay if delay >= 20 else 100)   # los navegadores tratan un cuadro de menos de 20 ms como 100 ms
    path = os.path.join(_upload_root(), *rel.split('/'))
    for quality in ANIM_QUALITIES:
        if time.monotonic() - started > MAX_ANIM_SECONDS:
            return None, f'{label}: la animación tardó demasiado en procesarse. Usa una más corta o más chica.'
        buffer = io.BytesIO()
        frames[0].save(buffer, format='WEBP', save_all=True, append_images=frames[1:], duration=durations, loop=0, quality=quality, method=4)
        if buffer.tell() <= max_bytes:
            with open(path, 'wb') as handle:
                handle.write(buffer.getvalue())
            return rel, ''
    return None, f'{label}: la animación queda demasiado pesada (máximo {max_bytes // (1024 * 1024)} MB). Usa una más corta o más chica.'


def keep_alpha_or_small(kind):
    """Los GIF animados solo se conservan tal cual en foto de perfil, banner y marcos (no en el fondo)."""
    return kind != 'background'


def media_url_path(path):
    """`uploads/...` para url_for('static') o ''."""
    safe = safe_media_path(path)
    return f'uploads/{safe}' if safe else ''


def reset_profile(profile, keep_privacy=False):
    """Deja el perfil como nuevo: borra imágenes, textos y apariencia (lo usa el dueño o el administrador).
    Con keep_privacy=True (el administrador) NO toca los interruptores de privacidad del dueño: público, mostrar números y aparecer en rankings."""
    for kind, (column, *_rest) in MEDIA_KINDS.items():
        delete_media(getattr(profile, column))
        setattr(profile, column, '')
    profile.avatar_frame_id = None
    profile.banner_frame_id = None
    # lo premium de la tienda de puntos que tenía puesto (la colección de la cuenta no se toca: lo puede volver a poner)
    for column in ('avatar_frame_item_id', 'banner_frame_item_id', 'avatar_photo_item_id', 'banner_photo_item_id', 'background_item_id'):
        setattr(profile, column, None)
    profile.display_name = ''
    profile.bio = ''
    profile.status_text = ''
    profile.config_json = ''
    if not keep_privacy:
        profile.is_public = True
        profile.show_stats = True
        profile.in_rankings = True


# ───────────────────────── Estilos listos para elegir (el editor los copia a los campos) ─────────────────────────
AVATAR_RING_PRESETS = [
    {'name': 'Ninguno', 'ring_style': 'none', 'ring_c1': '#ffffff', 'ring_c2': '#9ca3af', 'ring_width': 0, 'ring_glow': 0, 'ring_anim': 'none'},
    {'name': 'Clásico', 'ring_style': 'solid', 'ring_c1': '#ffffff', 'ring_c2': '#9ca3af', 'ring_width': 3, 'ring_glow': 0, 'ring_anim': 'none'},
    {'name': 'Rojo 3S', 'ring_style': 'gradient', 'ring_c1': '#ff1a2e', 'ring_c2': '#7a0010', 'ring_width': 4, 'ring_glow': 12, 'ring_anim': 'none'},
    {'name': 'Neón', 'ring_style': 'conic', 'ring_c1': '#00e5ff', 'ring_c2': '#ff00e5', 'ring_width': 4, 'ring_glow': 14, 'ring_anim': 'spin'},
    {'name': 'Oro', 'ring_style': 'gradient', 'ring_c1': '#ffe08a', 'ring_c2': '#b8860b', 'ring_width': 4, 'ring_glow': 8, 'ring_anim': 'none'},
    {'name': 'Arcoíris', 'ring_style': 'rainbow', 'ring_c1': '#ffffff', 'ring_c2': '#ffffff', 'ring_width': 4, 'ring_glow': 10, 'ring_anim': 'spin'},
    {'name': 'Fuego', 'ring_style': 'gradient', 'ring_c1': '#ffb000', 'ring_c2': '#ff2d00', 'ring_width': 5, 'ring_glow': 18, 'ring_anim': 'pulse'},
    {'name': 'Hielo', 'ring_style': 'gradient', 'ring_c1': '#bdf3ff', 'ring_c2': '#2f8cff', 'ring_width': 4, 'ring_glow': 12, 'ring_anim': 'none'},
    {'name': 'Plata doble', 'ring_style': 'double', 'ring_c1': '#e5e7eb', 'ring_c2': '#6b7280', 'ring_width': 3, 'ring_glow': 6, 'ring_anim': 'none'},
    {'name': 'Espinas', 'ring_style': 'dashed', 'ring_c1': '#d1d5db', 'ring_c2': '#111827', 'ring_width': 4, 'ring_glow': 8, 'ring_anim': 'spin'},
]
BANNER_FRAME_PRESETS = [
    {'name': 'Ninguno', 'frame_style': 'none', 'frame_c1': '#e5e7eb', 'frame_c2': '#6b7280', 'frame_width': 0, 'frame_radius': 16, 'frame_glow': 0},
    {'name': 'Plata', 'frame_style': 'double', 'frame_c1': '#e5e7eb', 'frame_c2': '#6b7280', 'frame_width': 3, 'frame_radius': 14, 'frame_glow': 8},
    {'name': 'Rojo neón', 'frame_style': 'neon', 'frame_c1': '#ff1a2e', 'frame_c2': '#7a0010', 'frame_width': 2, 'frame_radius': 14, 'frame_glow': 16},
    {'name': 'Oro', 'frame_style': 'gradient', 'frame_c1': '#ffe08a', 'frame_c2': '#b8860b', 'frame_width': 4, 'frame_radius': 12, 'frame_glow': 8},
    {'name': 'Cian neón', 'frame_style': 'neon', 'frame_c1': '#00e5ff', 'frame_c2': '#0891b2', 'frame_width': 2, 'frame_radius': 16, 'frame_glow': 18},
    {'name': 'Esquinas', 'frame_style': 'corners', 'frame_c1': '#ffffff', 'frame_c2': '#9ca3af', 'frame_width': 3, 'frame_radius': 6, 'frame_glow': 0},
    {'name': 'Blanco', 'frame_style': 'solid', 'frame_c1': '#ffffff', 'frame_c2': '#9ca3af', 'frame_width': 3, 'frame_radius': 18, 'frame_glow': 0},
]
BANNER_COLOR_PRESETS = [
    {'name': 'Sangre', 'c1': '#3a0a12', 'c2': '#0b0b10', 'angle': 135},
    {'name': 'Noche', 'c1': '#0f172a', 'c2': '#312e81', 'angle': 120},
    {'name': 'Neón', 'c1': '#06b6d4', 'c2': '#d946ef', 'angle': 135},
    {'name': 'Fuego', 'c1': '#f59e0b', 'c2': '#dc2626', 'angle': 100},
    {'name': 'Bosque', 'c1': '#052e16', 'c2': '#15803d', 'angle': 150},
    {'name': 'Plata', 'c1': '#111827', 'c2': '#6b7280', 'angle': 135},
]
BACKGROUND_PRESETS = [
    {'name': 'Rojo oscuro', 'c1': '#1a0508', 'c2': '#000000', 'angle': 160},
    {'name': 'Medianoche', 'c1': '#0b1020', 'c2': '#1e1b4b', 'angle': 150},
    {'name': 'Púrpura', 'c1': '#2e1065', 'c2': '#09090b', 'angle': 135},
    {'name': 'Océano', 'c1': '#042f2e', 'c2': '#0c4a6e', 'angle': 145},
    {'name': 'Ceniza', 'c1': '#1f2937', 'c2': '#000000', 'angle': 180},
    {'name': 'Atardecer', 'c1': '#7c2d12', 'c2': '#1e1b4b', 'angle': 150},
]
ACCENT_SWATCHES = ['#e11d2e', '#f59e0b', '#22c55e', '#06b6d4', '#3b82f6', '#8b5cf6', '#ec4899', '#ffffff']
