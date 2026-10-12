"""Campana de notificaciones de la tienda.

La campana del header junta tres tipos de avisos, todos con los más nuevos
primero:

* Novedades (`SiteNotification`): las escribe el admin y las ven todos.
* Mensajes a una orden (`OrderNotification`): los escribe el admin desde la
  orden y solo los ve quien hizo esa compra en ese navegador.
* Estado de las órdenes de ese navegador: no se guardan, se calculan en el
  momento a partir de la orden. Así la campana siempre dice cómo va el pedido
  (verificando pago, en proceso, pago incompleto, lista o rechazada) sin
  depender de cada lugar del código que cambia el estado.

El navegador manda los números de las órdenes que hizo ahí (la página de la
orden solo los registra si la compra se hizo en ese navegador). Una orden de
otra persona no sale aunque alguien conozca su correo o su ID.
"""
import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy.orm import joinedload

from ..models import Order, OrderNotification, SiteNotification

ORDER_NUMBER_RE = re.compile(r'^[0-9A-Za-z]{4,20}$')
MAX_ORDERS = 15
FEED_MAX = 40
BROADCAST_MAX = 20
# Pasado este tiempo una orden terminada deja de aparecer; las pendientes se
# siguen mostrando mientras no terminen.
RECENT_ORDER_DAYS = 30
BROADCAST_DAYS = 60

TITLE_MAX = 120
BODY_MAX = 400


def clean_link(value):
    """Link de un aviso: una ruta de la tienda ("/promos/...") o una dirección
    http(s). Devuelve '' si está vacío, None si no es válido (por ejemplo
    "javascript:..."), para que nadie cuele código en la campana."""
    link = str(value or '').strip()
    if not link:
        return ''
    if link.startswith('/') and not link.startswith('//'):
        return link[:300]
    if re.match(r'^https?://[^\s/]+', link, re.I):
        return link[:300]
    return None


def _iso(moment):
    return (moment or datetime.utcnow()).isoformat() + 'Z'


def _is_code_section(order):
    category = order.game.category if order.game else None
    return bool(category and (category.slug or '').lower() == 'tarjetas')


def _missing_bs(order):
    from .payment_verification import _get_expected_order_amount
    expected, error = _get_expected_order_amount(order)
    if error or expected is None:
        return None
    missing = expected - Decimal(str(order.paid_amount_bs or 0))
    return str(missing.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)) if missing > 0 else None


def order_status_item(order):
    """Aviso con el estado ACTUAL de la orden (uno por orden)."""
    number = order.order_number
    game = order.game.name if order.game else ''
    package = order.package.name if order.package else ''
    what = f'{game} · {package}' if game and package else (game or package or 'tu recarga')
    base = {'kind': 'order', 'order_number': number, 'url': f'/order/{number}'}

    def item(state, level, title, body, moment):
        return {**base, 'id': f'order-{order.id}-{state}', 'level': level, 'title': title, 'body': body,
                'moment': moment or order.updated_at or order.created_at}

    if order.status == 'completed':
        if order.pin_delivered and _is_code_section(order):
            return item('completed', 'success', '¡Tu código está listo!',
                        f'Tu orden #{number} ({what}) está lista. Ábrela para ver tu código.', order.updated_at)
        return item('completed', 'success', '¡Tu recarga está lista! 🎉',
                    f'Tu orden #{number} ({what}) se completó. Si no la ves en el juego, reinícialo.', order.updated_at)
    if order.status == 'rejected':
        return item('rejected', 'danger', 'Tu orden fue rechazada',
                    f'Tu orden #{number} ({what}) no se pudo procesar. Escríbenos por soporte si crees que es un error.',
                    order.updated_at)
    if order.awaiting_payment_completion:
        missing = _missing_bs(order)
        body = (f'A tu orden #{number} le faltan Bs {missing}. Entra a tu orden y sube el pago restante para completarla.'
                if missing else f'El pago de tu orden #{number} llegó incompleto. Entra a tu orden y sube el pago restante.')
        return item('incomplete', 'warning', 'Tu pago está incompleto', body,
                    order.payment_last_verification_at or order.updated_at)
    if order.status == 'approved' or order.payment_verified_at:
        return item('processing', 'info', 'Tu recarga está en proceso',
                    f'Ya confirmamos el pago de tu orden #{number} ({what}). Estamos haciendo la recarga; te avisamos cuando esté lista.',
                    order.payment_verified_at or order.updated_at)
    return item('verifying', 'info', 'Estamos verificando tu pago',
                f'Recibimos tu orden #{number} ({what}). Estamos verificando el pago; puedes seguir el proceso en tu orden.',
                order.created_at)


def _valid_order_numbers(raw_numbers):
    numbers, seen = [], set()
    for raw in raw_numbers if isinstance(raw_numbers, (list, tuple)) else []:
        number = str(raw or '').strip()
        if ORDER_NUMBER_RE.match(number) and number not in seen:
            seen.add(number)
            numbers.append(number)
        if len(numbers) >= MAX_ORDERS:
            break
    return numbers


def build_feed(raw_order_numbers):
    """Lista de avisos de la campana, los más nuevos primero. Cada elemento:
    id, kind (broadcast|order|message), level, title, body, url, order_number, at."""
    now = datetime.utcnow()
    entries = []

    broadcasts = (
        SiteNotification.query
        .filter(SiteNotification.is_active.is_(True), SiteNotification.created_at >= now - timedelta(days=BROADCAST_DAYS))
        .order_by(SiteNotification.created_at.desc(), SiteNotification.id.desc())
        .limit(BROADCAST_MAX)
        .all()
    )
    for row in broadcasts:
        entries.append({
            'id': f'site-{row.id}', 'kind': 'broadcast', 'level': 'info', 'title': row.title, 'body': row.body,
            'url': row.url or '', 'order_number': '', 'moment': row.created_at,
        })

    numbers = _valid_order_numbers(raw_order_numbers)
    if numbers:
        orders = (
            Order.query
            .options(joinedload(Order.game), joinedload(Order.package))
            .filter(Order.order_number.in_(numbers))
            .all()
        )
        cutoff = now - timedelta(days=RECENT_ORDER_DAYS)
        for order in orders:
            status_item = order_status_item(order)
            still_open = order.status not in ('completed', 'rejected')
            if still_open or (status_item['moment'] and status_item['moment'] >= cutoff):
                entries.append(status_item)

        by_id = {order.id: order for order in orders}
        if by_id:
            messages = (
                OrderNotification.query
                .filter(OrderNotification.order_id.in_(list(by_id)), OrderNotification.created_at >= now - timedelta(days=BROADCAST_DAYS))
                .order_by(OrderNotification.created_at.desc(), OrderNotification.id.desc())
                .limit(30)
                .all()
            )
            for msg in messages:
                number = by_id[msg.order_id].order_number
                entries.append({
                    'id': f'msg-{msg.id}', 'kind': 'message', 'level': 'info', 'title': f'Sobre tu orden #{number}',
                    'body': msg.body, 'url': f'/order/{number}', 'order_number': number, 'moment': msg.created_at,
                })

    entries.sort(key=lambda e: (e['moment'] or now, e['id']), reverse=True)
    feed = []
    for entry in entries[:FEED_MAX]:
        feed.append({
            'id': entry['id'], 'kind': entry['kind'], 'level': entry['level'], 'title': entry['title'],
            'body': entry['body'], 'url': entry['url'], 'order_number': entry['order_number'],
            'at': _iso(entry['moment']),
        })
    return feed
