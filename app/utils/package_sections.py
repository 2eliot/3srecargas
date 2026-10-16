"""Apartados de los paquetes de un juego (las pestañitas Moneda / Pases / Tarjetas de la tienda).

Hay 3 de siempre (`PACKAGE_SECTION_LABELS`: main, pass, card) y los que el dueño crea desde el formulario del paquete con "Crear nuevo +"
(tabla `package_sections`, clave 'c<id>'). Todo lo que necesite saber qué apartados existen —el admin, la tienda, `Package.to_dict`— lo
pregunta aquí, así la regla es una sola."""
import re
import unicodedata

from ..models import PACKAGE_SECTION_LABELS, PackageSection, db

# Cómo se llama cada apartado de siempre en la pestañita de la tienda (más corto) y en el admin (nombre completo).
BUILTIN_SHORT = {'main': 'Moneda', 'pass': 'Pases', 'card': 'Tarjetas'}
NAME_MAX = 24            # la pestañita de la tienda es chica
MAX_CUSTOM = 12
_BAD_CHARS = re.compile(r'[<>"`\\]|[\x00-\x1f\x7f]')


def _fold(text):
    """Para comparar nombres sin importar mayúsculas, tildes ni espacios de más."""
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'\s+', ' ', text.lower()).strip()


def _custom_rows():
    """Apartados propios en orden. Se guardan en la petición actual para no consultar una vez por paquete."""
    try:
        from flask import g, has_request_context
        if has_request_context():
            rows = getattr(g, '_package_sections', None)
            if rows is None:
                rows = PackageSection.query.order_by(PackageSection.sort_order, PackageSection.id).all()
                g._package_sections = rows
            return rows
        return PackageSection.query.order_by(PackageSection.sort_order, PackageSection.id).all()
    except Exception:
        db.session.rollback()
        return []          # base sin la tabla todavía: solo los de siempre


def forget_cache():
    try:
        from flask import g, has_request_context
        if has_request_context():
            g._package_sections = None
    except Exception:
        pass


def section_labels():
    """{clave: nombre completo} en el orden en que se muestran: los 3 de siempre y después los propios."""
    labels = dict(PACKAGE_SECTION_LABELS)
    for row in _custom_rows():
        labels[row.key] = row.name
    return labels


def valid_section_key(raw):
    """La clave si es un apartado que existe; si no, None."""
    value = str(raw or '').strip()
    return value if value in section_labels() else None


def section_meta():
    """Lo que la tienda necesita para dibujar las pestañitas: [{key, label, title}] en orden."""
    meta = [{'key': key, 'label': BUILTIN_SHORT[key], 'title': PACKAGE_SECTION_LABELS[key]} for key in PACKAGE_SECTION_LABELS]
    meta += [{'key': row.key, 'label': row.name, 'title': row.name} for row in _custom_rows()]
    return meta


def create_section(raw_name):
    """Crea un apartado propio. Devuelve (apartado, None) o (None, mensaje para el dueño). No hace commit."""
    name = re.sub(r'\s+', ' ', str(raw_name or '')).strip()
    if not name:
        return None, 'Escribe el nombre del apartado.'
    if len(name) > NAME_MAX:
        return None, f'El nombre es muy largo (máximo {NAME_MAX} letras): así se ve en la pestañita de la tienda.'
    if _BAD_CHARS.search(name):
        return None, 'El nombre tiene símbolos que no se pueden usar.'
    wanted = _fold(name)
    taken = {_fold(v) for v in PACKAGE_SECTION_LABELS.values()} | {_fold(v) for v in BUILTIN_SHORT.values()}
    rows = _custom_rows()
    taken |= {_fold(row.name) for row in rows}
    if wanted in taken:
        return None, 'Ya existe un apartado con ese nombre.'
    if len(rows) >= MAX_CUSTOM:
        return None, f'Ya tienes {MAX_CUSTOM} apartados propios, que es el máximo.'
    section = PackageSection(name=name, sort_order=(max([row.sort_order or 0 for row in rows]) + 1) if rows else 1)
    db.session.add(section)
    db.session.flush()
    forget_cache()
    return section, None
