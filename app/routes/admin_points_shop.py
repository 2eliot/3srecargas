"""Tienda de puntos de las cuentas, en el admin ("Puntos por cuenta"): crear y editar los artículos que las cuentas canjean en `/tiendapuntos`, y ver los canjes.

Va en su propio módulo (como `admin_support.py` y `admin_tournaments.py`): `admin.py` ya es enorme y el `before_request` de `admin_bp` solo protege a su
blueprint. Cuelga de `/admin/puntos-cuenta`. La lógica del canje vive en `utils/points_shop.py`.
"""
from datetime import datetime, timezone

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from ..models import Game, Package, PointsShopItem, PointsShopOrder, SHOP_KINDS, User, UserShopItem, db
from ..utils import account_points, points_shop
from ..utils.timezone import VENEZUELA_TIMEZONE, format_ve
from .admin import delete_uploaded_file, save_image

admin_points_shop_bp = Blueprint('admin_points_shop_bp', __name__, url_prefix='/admin/puntos-cuenta')

NAME_MAX = 80
DESCRIPTION_MAX = 300
RECENT_ORDERS = 60


@admin_points_shop_bp.before_request
def guard():
    if not current_user.is_authenticated:
        return None  # lo resuelve @login_required + el unauthorized_handler
    if current_user.__class__.__name__ != 'AdminUser':
        flash('Esta sección es solo para administradores.', 'warning')
        return redirect(url_for('main_bp.index'))
    return None


class FormError(ValueError):
    pass


def _int(raw, label, minimum=None, maximum=None, required=False):
    raw = str(raw or '').strip()
    if not raw:
        if required:
            raise FormError(f'Escribe {label}.')
        return None
    try:
        value = int(float(raw))
    except ValueError:
        raise FormError(f'{label[0].upper() + label[1:]} no es un número válido.')
    if minimum is not None and value < minimum:
        raise FormError(f'{label[0].upper() + label[1:]} debe ser {minimum} o más.')
    if maximum is not None and value > maximum:
        raise FormError(f'{label[0].upper() + label[1:]} es demasiado grande.')
    return value


def _parse_local(raw, label):
    """'2026-10-14T20:00' (hora de VENEZUELA, como la manda un campo de fecha y hora) -> datetime en UTC sin zona; vacío -> None."""
    raw = str(raw or '').strip()
    if not raw:
        return None
    local = None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S'):
        try:
            local = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue
    if local is None or not (2020 <= local.year <= 2100):
        raise FormError(f'{label} no es válida. Elígela en el calendario.')
    return local.replace(tzinfo=VENEZUELA_TIMEZONE).astimezone(timezone.utc).replace(tzinfo=None)


def _local_value(dt):
    return format_ve(dt, '%Y-%m-%dT%H:%M') if dt else ''


def _read_item_form(form, kind):
    """Lee y valida el formulario de un artículo. Devuelve un diccionario con los campos ya limpios o lanza FormError con un mensaje en español."""
    name = ' '.join(str(form.get('name') or '').split())
    if not name:
        raise FormError('Ponle un nombre al artículo.')
    if len(name) > NAME_MAX:
        raise FormError(f'El nombre es muy largo (máximo {NAME_MAX} letras).')
    description = ' '.join(str(form.get('description') or '').split())[:DESCRIPTION_MAX]
    points_cost = _int(form.get('points_cost'), 'el precio en puntos', 0, points_shop.MAX_COST, required=True)

    data = {
        'name': name, 'description': description, 'points_cost': points_cost,
        'stock': _int(form.get('stock'), 'las existencias', 0, 10_000_000),
        'per_user_limit': _int(form.get('per_user_limit'), 'el límite por persona', 1, 10_000),
        'limit_period': form.get('limit_period') if form.get('limit_period') in points_shop.LIMIT_PERIODS else 'always',
        'starts_at': _parse_local(form.get('starts_at'), 'La fecha de inicio'),
        'ends_at': _parse_local(form.get('ends_at'), 'La fecha de fin'),
        'sort_order': _int(form.get('sort_order'), 'el orden', -10_000, 10_000) or 0,
        'is_active': form.get('is_active') == '1',
        'package_id': None, 'amount': None,
    }
    if data['starts_at'] and data['ends_at'] and data['ends_at'] <= data['starts_at']:
        raise FormError('La fecha de fin tiene que ser después de la de inicio.')
    if kind in points_shop.COSMETIC_KINDS:
        data['per_user_limit'] = None       # un marco, fondo o foto se tiene una sola vez: el sistema ya lo impide
        data['limit_period'] = 'always'

    if kind in points_shop.DELIVERY_KINDS:
        package_id = _int(form.get('package_id'), 'el paquete', 1, required=True)
        package = db.session.get(Package, package_id)
        if package is None or package.game is None:
            raise FormError('Elige un paquete de la lista.')
        is_card = points_shop._is_card_game(package.game)
        if kind == 'card' and not is_card:
            raise FormError('Una tarjeta de regalo tiene que ser un paquete de la categoría Tarjetas.')
        if kind == 'package' and is_card:
            raise FormError('Las tarjetas de regalo se crean como "Tarjeta de regalo", no como paquete de un juego.')
        data['package_id'] = package.id
    if kind in points_shop.BONUS_KINDS:
        data['amount'] = _int(form.get('amount'), 'cuántos puntos suma', 1, 1_000_000, required=True)
    return data


def _apply(item, data):
    for key, value in data.items():
        setattr(item, key, value)


def _save_image_field(item, field, folder_name, files, remove_flag):
    """Guarda la imagen subida (y borra la anterior); `remove_flag` pide quitar la actual sin subir otra."""
    upload = files.get(folder_name)
    if upload and upload.filename:
        saved = save_image(upload, 'shop')
        if not saved:
            raise FormError('La imagen no es válida. Usa PNG, JPG, WEBP o GIF.')
        old = getattr(item, field)
        setattr(item, field, saved)
        if old and old != saved:
            delete_uploaded_file(old)
    elif remove_flag and getattr(item, field):
        delete_uploaded_file(getattr(item, field))
        setattr(item, field, None)


# ─── Lista ──────────────────────────────────────────────────────────────────

@admin_points_shop_bp.route('/')
@login_required
def index():
    items = PointsShopItem.query.order_by(PointsShopItem.kind.asc(), PointsShopItem.sort_order.asc(), PointsShopItem.id.asc()).all()
    packages = (Package.query.filter_by(is_active=True).join(Game, Game.id == Package.game_id).filter(Game.is_active.is_(True))
                .options(joinedload(Package.game)).order_by(Game.name.asc(), Package.sort_order.asc(), Package.id.asc()).all())
    package_options = [{'id': p.id, 'label': f'{p.game.name} — {p.name}', 'card': points_shop._is_card_game(p.game)} for p in packages]
    stats = {
        'items': len(items), 'active': sum(1 for i in items if i.is_active),
        'orders': PointsShopOrder.query.filter_by(status='done').count(),
        'points_spent': int(db.session.query(func.coalesce(func.sum(PointsShopOrder.points_spent), 0)).filter_by(status='done').scalar() or 0),
    }
    sold = dict(db.session.query(PointsShopOrder.item_id, func.count(PointsShopOrder.id)).filter_by(status='done').group_by(PointsShopOrder.item_id).all())
    recent = (PointsShopOrder.query.options(joinedload(PointsShopOrder.order))
              .order_by(PointsShopOrder.id.desc()).limit(RECENT_ORDERS).all())
    users = {u.id: u for u in User.query.filter(User.id.in_({r.user_id for r in recent} or {0})).all()}
    return render_template(
        'admin/points_shop.html', items=items, kinds=SHOP_KINDS, enabled_kinds=set(points_shop._handlers().keys()),
        package_options=package_options, stats=stats, sold=sold, recent=recent, users=users,
        limit_periods=points_shop.LIMIT_PERIOD_LABELS, local_value=_local_value,
    )


# ─── Crear / editar ─────────────────────────────────────────────────────────

@admin_points_shop_bp.route('/articulo', methods=['POST'])
@login_required
def item_save():
    item_id = _int(request.form.get('item_id'), 'el artículo', 1)
    item = db.session.get(PointsShopItem, item_id) if item_id else None
    if item_id and item is None:
        abort(404)
    kind = item.kind if item else (request.form.get('kind') or '')
    if kind not in SHOP_KINDS:
        flash('Elige qué tipo de artículo vas a crear.', 'danger')
        return redirect(url_for('admin_points_shop_bp.index'))
    if item is None and kind not in points_shop._handlers():
        flash('Ese tipo de artículo todavía no está disponible.', 'danger')
        return redirect(url_for('admin_points_shop_bp.index'))
    try:
        data = _read_item_form(request.form, kind)
        creating = item is None
        if creating:
            item = PointsShopItem(kind=kind)
            db.session.add(item)
        _apply(item, data)
        _save_image_field(item, 'image', 'image', request.files, request.form.get('remove_image') == '1')
        _save_asset(item, kind)
        db.session.commit()
    except FormError as exc:
        db.session.rollback()
        flash(str(exc), 'danger')
        return redirect(url_for('admin_points_shop_bp.index') + ('#a' + str(item_id) if item_id else '#nuevo'))
    flash(('Artículo creado: ' if creating else 'Artículo guardado: ') + item.name, 'success')
    return redirect(url_for('admin_points_shop_bp.index') + f'#a{item.id}')


def _save_asset(item, kind):
    """Los marcos, fondos y fotos premium necesitan su archivo (lo que se desbloquea). Lo agrega la etapa de cosméticos."""
    if kind not in points_shop.COSMETIC_KINDS:
        return
    from ..utils import shop_assets
    try:
        shop_assets.save_item_asset(item, request.files.get('asset'), request.form.get('remove_asset') == '1')
    except shop_assets.AssetError as exc:
        raise FormError(str(exc))


@admin_points_shop_bp.route('/articulo/<int:item_id>/activar', methods=['POST'])
@login_required
def item_toggle(item_id):
    item = db.session.get(PointsShopItem, item_id) or abort(404)
    item.is_active = not item.is_active
    db.session.commit()
    flash(f'«{item.name}» ' + ('ahora se ve en la tienda.' if item.is_active else 'quedó oculto de la tienda.'), 'success')
    return redirect(url_for('admin_points_shop_bp.index') + f'#a{item.id}')


@admin_points_shop_bp.route('/articulo/<int:item_id>/eliminar', methods=['POST'])
@login_required
def item_delete(item_id):
    item = db.session.get(PointsShopItem, item_id) or abort(404)
    used = PointsShopOrder.query.filter_by(item_id=item.id).count() + UserShopItem.query.filter_by(item_id=item.id).count()
    if used:
        # Ya lo canjeó alguien: no se borra (se rompería su historial o su colección). Se oculta de la tienda.
        item.is_active = False
        db.session.commit()
        flash(f'«{item.name}» ya fue canjeado, así que no se borra: quedó oculto de la tienda.', 'warning')
    else:
        for path in (item.image, item.asset_path):
            if path:
                delete_uploaded_file(path)
        db.session.delete(item)
        db.session.commit()
        flash('Artículo eliminado.', 'success')
    return redirect(url_for('admin_points_shop_bp.index'))


# ─── Canjes ─────────────────────────────────────────────────────────────────

@admin_points_shop_bp.route('/canje/<int:order_id>/devolver', methods=['POST'])
@login_required
def order_refund(order_id):
    """Devuelve los puntos de un canje a la cuenta (y la existencia al artículo). NO cancela la orden de premio de un paquete o tarjeta: eso se resuelve en Órdenes."""
    order = db.session.get(PointsShopOrder, order_id) or abort(404)
    if order.status == 'refunded':
        flash('Ese canje ya estaba devuelto.', 'warning')
    else:
        points_shop._refund(order.id, 'Devuelto por el administrador')
        flash(f'Se devolvieron {order.points_spent} puntos a la cuenta.', 'success')
    return redirect(url_for('admin_points_shop_bp.index') + '#canjes')
