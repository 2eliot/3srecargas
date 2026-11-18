"""Bloqueo de IP para CREAR órdenes tras varias órdenes rechazadas.

Regla del dueño: 5 órdenes rechazadas desde la misma IP en 24 h → esa IP no
puede crear más órdenes durante 24 h. El bloqueo NO afecta navegar, ver el
estado de una orden, el chat de soporte ni el resto de la web.
"""
from datetime import datetime, timedelta

from flask import current_app

from ..models import IpOrderBlock, Order, db

ORDER_STRIKE_LIMIT = 5
ORDER_STRIKE_WINDOW_HOURS = 24
ORDER_BLOCK_HOURS = 24


def _now():
    return datetime.utcnow()


def is_enabled():
    """El bloqueo por rechazos se puede apagar desde Admin → Seguridad.

    Por defecto está encendido. Se apaga con el setting
    `order_ip_block_enabled = 0` (por ejemplo, si el proveedor se queda sin
    saldo y hay que rechazar muchas órdenes legítimas de golpe).
    """
    try:
        from flask import g, has_app_context
        if has_app_context():
            cached = getattr(g, '_order_ip_block_enabled', None)
            if cached is not None:
                return cached
        from ..models import Setting
        row = Setting.query.filter_by(key='order_ip_block_enabled').first()
        value = ((row.value if row else '') or '1').strip().lower()
        enabled = value not in {'0', 'false', 'no', 'off'}
        if has_app_context():
            g._order_ip_block_enabled = enabled
        return enabled
    except Exception:
        return True


def active_block(ip):
    if not is_enabled():
        return None
    ip = (ip or '').strip()
    if not ip:
        return None
    row = IpOrderBlock.query.filter_by(ip=ip).first()
    if row and row.blocked_until and row.blocked_until > _now():
        return row
    return None


def register_rejected_order(order):
    """Se llama DESPUÉS de guardar el rechazo. Cuenta las rechazadas de esa IP."""
    if not is_enabled():
        return None
    ip = (getattr(order, 'client_ip', '') or '').strip()
    if not ip:
        return None
    since = _now() - timedelta(hours=ORDER_STRIKE_WINDOW_HOURS)
    rejected = (
        Order.query
        .filter(
            Order.client_ip == ip,
            Order.status == 'rejected',
            Order.rejected_at.isnot(None),
            Order.rejected_at >= since,
        )
        .count()
    )
    if rejected < ORDER_STRIKE_LIMIT:
        return None

    until = _now() + timedelta(hours=ORDER_BLOCK_HOURS)
    row = IpOrderBlock.query.filter_by(ip=ip).first()
    if not row:
        row = IpOrderBlock(ip=ip)
        db.session.add(row)
    row.blocked_until = until
    row.reason = f'{rejected} órdenes rechazadas en {ORDER_STRIKE_WINDOW_HOURS} h'
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return None
    try:
        current_app.logger.warning(
            'ORDER_IP_BLOCK ip=%s rejected=%s until=%s',
            ip, rejected, until.isoformat(timespec='seconds'),
        )
    except Exception:
        pass
    return row


def active_blocks(limit=100):
    return (
        IpOrderBlock.query
        .filter(IpOrderBlock.blocked_until > _now())
        .order_by(IpOrderBlock.blocked_until.asc())
        .limit(int(limit or 100))
        .all()
    )


def unblock(ip):
    ip = (ip or '').strip()
    if not ip:
        return False
    try:
        deleted = IpOrderBlock.query.filter_by(ip=ip).delete(synchronize_session=False)
        db.session.commit()
        return bool(deleted)
    except Exception:
        db.session.rollback()
        return False
