"""Sello del catálogo: cambia cada vez que el admin toca precios o tasa.

El sello de despliegue (`version.build_version`) solo cambia con un deploy,
así que una pestaña abierta hace días no se enteraba de que el admin subió
la tasa o cambió el precio de un paquete: seguía mostrando —y cobrando— lo
de cuando se abrió. Este sello se guarda en la tabla `settings` (compartida
por los 3 workers) y se sube solo, desde un hook de SQLAlchemy, cuando en
un flush hay cambios en juegos, paquetes, categorías, métodos de pago,
descuentos o en los ajustes que afectan lo que ve la tienda. No hay que
acordarse de llamarlo desde cada ruta del admin.

`site_version()` junta ambos sellos: es lo que compara version-watch.js.
"""

import time

from sqlalchemy import event

from ..models import db, Category, Game, Package, PaymentMethod, Discount, Setting
from .version import build_version

CATALOG_VERSION_KEY = 'catalog_version'

# Ajustes cuyo cambio debe verse en la tienda sin esperar a un deploy.
CATALOG_SETTING_KEYS = frozenset({
    'usd_rate_bs',
    'default_auto_package_id',
    'active_login_game_id',
    'bs_package_id',
    'binance_wallet_address',
    'promo_banner_image',
    'promo_banner_link',
    'site_logo',
    'site_background_image',
    'checkout_payment_video_method',
    'checkout_payment_video_file',
    'checkout_payment_video_title',
    'checkout_payment_video_message',
    'checkout_payment_video_cta',
    'manual_open_hour',
    'manual_close_hour',
})

_CATALOG_MODELS = (Category, Game, Package, PaymentMethod, Discount)


def _touches_catalog(obj):
    if isinstance(obj, _CATALOG_MODELS):
        return True
    if isinstance(obj, Setting):
        return (obj.key or '') in CATALOG_SETTING_KEYS
    return False


def _bump_in_session(session):
    """Sube el sello dentro del flush en curso (misma transacción)."""
    row = session.query(Setting).filter_by(key=CATALOG_VERSION_KEY).first()
    stamp = str(int(time.time()))
    if row is None:
        session.add(Setting(
            key=CATALOG_VERSION_KEY,
            value=stamp,
            description='Sello del catálogo (precios/tasa); lo sube la app sola',
        ))
    elif row.value != stamp:
        row.value = stamp


def _before_flush(session, flush_context, instances):
    pending = list(session.new) + list(session.dirty) + list(session.deleted)
    for obj in pending:
        if isinstance(obj, Setting) and (obj.key or '') == CATALOG_VERSION_KEY:
            continue
        if _touches_catalog(obj):
            _bump_in_session(session)
            return


def register_catalog_version_hook():
    if not event.contains(db.session, 'before_flush', _before_flush):
        event.listen(db.session, 'before_flush', _before_flush)


def get_catalog_version():
    row = Setting.query.filter_by(key=CATALOG_VERSION_KEY).first()
    return (row.value or '0') if row else '0'


def site_version():
    """Sello completo: despliegue + catálogo."""
    return f'{build_version()}.{get_catalog_version()}'
