"""Etiqueta corta para las órdenes de premio/canje.

En el admin, arriba de la referencia `CANJE-...`, se muestra qué es: código de
regalo (con su lote), puntos, ruleta, torneo, ranking, Hordas, etc. Así no hay
que abrir las notas para saber de dónde salió el premio.
"""
import re

from ..models import GiftCode

PRIZE_PREFIXES = (
    'CANJE', 'PREMIO', 'PUNTOS', 'TIENDA', 'MINIJUEGO', 'RECARGA-ACUM',
    'HORDAS', 'RANKING', 'TORNEO', 'SORTEO', 'ADIVINA',
)
SOURCE_LABELS = {'canal': 'Canal', 'tiktok': 'TikTok', 'otro': 'Otro'}


def is_prize_order(order):
    if not order:
        return False
    if (order.payment_method or '').strip().lower() == 'prize':
        return True
    ref = (order.payment_reference or '').strip().upper()
    return any(ref.startswith(f'{prefix}-') for prefix in PRIZE_PREFIXES)


def _points_text(notes):
    match = re.search(r'(\d[\d.,]*)\s*pts?\b', notes or '', re.IGNORECASE)
    if not match:
        return ''
    return f'{match.group(1).replace(",", ".")} pts'


def _label_for(order, gift=None):
    notes = (order.notes or '').lower()
    ref = (order.payment_reference or '').strip().upper()
    points = _points_text(notes)

    if gift or 'código de regalo' in notes or 'codigo de regalo' in notes:
        parts = ['Código de regalo']
        if gift:
            batch = (gift.batch or '').strip()
            source = SOURCE_LABELS.get((gift.source or '').strip().lower(), '')
            if batch:
                parts.append(f'lote "{batch}"')
            if source:
                parts.append(source)
        label = ' · '.join(parts)
    elif 'tienda de puntos' in notes or ref.startswith('TIENDA-'):
        label = 'Tienda de puntos' + (f' · {points}' if points else '')
    elif 'premio canjeado con puntos' in notes or ref.startswith('PUNTOS-'):
        if 'giro' in notes or ref.startswith('PUNTOS-'):
            label = 'Ruleta de puntos' + (f' · {points}' if points else '')
        else:
            label = 'Canje de puntos' + (f' · {points}' if points else '')
    elif 'hordas' in notes or ref.startswith('HORDAS-'):
        label = 'Hordas de Diamantes'
    elif 'ranking' in notes or ref.startswith('RANKING-'):
        label = 'Ranking mensual'
    elif 'torneo' in notes or ref.startswith('TORNEO-'):
        label = 'Torneo'
    elif 'sorteo' in notes or ref.startswith('SORTEO-'):
        label = 'Sorteo Diario'
    elif 'adivina' in notes or ref.startswith('ADIVINA-'):
        label = 'Adivina el Número'
    elif 'recarga acumulada' in notes or ref.startswith('RECARGA-ACUM-'):
        label = 'Recarga Acumulada'
    elif 'minijuego' in notes or ref.startswith('MINIJUEGO-'):
        label = 'Minijuego'
    elif ref.startswith('CANJE-'):
        label = 'Canje de puntos' + (f' · {points}' if points else '')
    else:
        label = 'Premio'

    game = order.game.name if getattr(order, 'game', None) else ''
    return f'{game} · {label}' if game else label


def order_prize_label(order):
    if not is_prize_order(order):
        return ''
    try:
        gift = GiftCode.query.filter_by(order_id=order.id).first()
    except Exception:
        gift = None
    return _label_for(order, gift)


def order_prize_labels(orders):
    """{order_id: etiqueta} para una lista, con una sola consulta de códigos."""
    orders = [order for order in (orders or []) if is_prize_order(order)]
    result = {}
    if not orders:
        return result
    gifts = {}
    try:
        rows = GiftCode.query.filter(GiftCode.order_id.in_([o.id for o in orders])).all()
        gifts = {gift.order_id: gift for gift in rows if gift.order_id}
    except Exception:
        gifts = {}
    for order in orders:
        result[order.id] = _label_for(order, gifts.get(order.id))
    return result
