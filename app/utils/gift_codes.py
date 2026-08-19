"""Códigos de regalo: generación por lotes, validación y canje.

Los códigos se reparten en el canal y en TikTok y se canjean en /redimir.
La entrega no se reinventa aquí: se manda por el mismo camino que cualquier
compra (`deliver_prize_to_player`), así el canje queda en Órdenes como una
más y, si en ese momento no hay stock, se queda pendiente para resolverla a
mano en vez de perderse.

Es una página pública que entrega saldo, así que el canje está protegido
por tres cosas: códigos largos y aleatorios, un límite de intentos por IP y
un UPDATE condicional que garantiza que un código no se use dos veces
aunque lleguen dos peticiones a la vez.
"""

import re
import secrets
import string
from datetime import datetime, timedelta

from ..models import GiftCode, Order, Package, db

# Sin 0/O/1/I/L para que nadie se equivoque al copiar un código de un video.
CODE_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'
CODE_BLOCK = 4
CODE_BLOCKS = 3          # 3 bloques de 4 = 12 caracteres útiles
MAX_BATCH_SIZE = 500

# Límite de intentos fallidos por IP antes de cerrar la puerta un rato.
ATTEMPT_LIMIT = 12
ATTEMPT_WINDOW_MINUTES = 15

# Contador de intentos en memoria del proceso. No hace falta que sea
# perfecto ni compartido entre workers: sirve para frenar a quien esté
# probando códigos en serie, y el uso legítimo no llega ni cerca del tope.
_attempts = {}


def normalize_code(raw):
    """Deja el código como se guarda: mayúsculas, sin guiones ni espacios."""
    cleaned = re.sub(r'[^A-Za-z0-9]', '', str(raw or ''))
    return cleaned.upper()


def format_code(code):
    """Presenta el código en bloques de 4: ABCD-EFGH-JKMN."""
    code = normalize_code(code)
    return '-'.join(code[i:i + CODE_BLOCK] for i in range(0, len(code), CODE_BLOCK))


def generate_code():
    return ''.join(
        secrets.choice(CODE_ALPHABET) for _ in range(CODE_BLOCK * CODE_BLOCKS)
    )


# ─── Límite de intentos ──────────────────────────────────────────────────────

def _prune_attempts(now):
    corte = now - timedelta(minutes=ATTEMPT_WINDOW_MINUTES)
    for key in [k for k, v in _attempts.items() if v['first'] < corte]:
        _attempts.pop(key, None)


def register_failed_attempt(ip):
    """Suma un intento fallido y devuelve cuántos van en la ventana."""
    if not ip:
        return 0
    now = datetime.utcnow()
    _prune_attempts(now)
    entry = _attempts.get(ip)
    if not entry or entry['first'] < now - timedelta(minutes=ATTEMPT_WINDOW_MINUTES):
        entry = {'count': 0, 'first': now}
    entry['count'] += 1
    _attempts[ip] = entry
    return entry['count']


def clear_attempts(ip):
    if ip:
        _attempts.pop(ip, None)


def is_rate_limited(ip):
    if not ip:
        return False
    now = datetime.utcnow()
    _prune_attempts(now)
    entry = _attempts.get(ip)
    return bool(entry and entry['count'] >= ATTEMPT_LIMIT)


# ─── Generación por lotes ────────────────────────────────────────────────────

def create_batch(package_id, quantity, batch='', source='', expires_at=None):
    """Genera `quantity` códigos para un paquete. Devuelve la lista creada."""
    package = Package.query.filter_by(id=int(package_id)).first()
    if not package:
        raise ValueError('El paquete no existe.')

    quantity = int(quantity or 0)
    if quantity < 1:
        raise ValueError('La cantidad debe ser al menos 1.')
    if quantity > MAX_BATCH_SIZE:
        raise ValueError(f'Máximo {MAX_BATCH_SIZE} códigos por lote.')

    creados = []
    for _ in range(quantity):
        # Reintenta si el azar repite un código ya existente.
        for _ in range(8):
            code = generate_code()
            if not GiftCode.query.filter_by(code=code).first():
                break
        else:
            raise RuntimeError('No se pudieron generar códigos únicos, intenta de nuevo.')

        gift = GiftCode(
            code=code,
            package_id=package.id,
            batch=(batch or '').strip()[:60] or None,
            source=(source or '').strip()[:30] or None,
            expires_at=expires_at,
        )
        db.session.add(gift)
        creados.append(gift)

    db.session.commit()
    return creados


# ─── Consulta y canje ────────────────────────────────────────────────────────

def find_code(raw_code):
    code = normalize_code(raw_code)
    if not code:
        return None
    return GiftCode.query.filter_by(code=code).first()


def describe_code_problem(gift):
    """Motivo por el que un código no sirve, o '' si sí sirve."""
    if not gift:
        return 'Ese código no existe. Revísalo y vuelve a intentar.'
    if not gift.is_active:
        return 'Ese código fue desactivado.'
    if gift.is_used:
        return 'Ese código ya fue canjeado.'
    if gift.is_expired:
        return 'Ese código ya venció.'
    if not gift.package or not gift.package.is_active:
        return 'El premio de ese código ya no está disponible. Escríbenos por soporte.'
    return ''


def claim_code(gift_id, player_id, zone_id=None, nickname=''):
    """Marca el código como usado de forma atómica.

    El UPDATE condicional (`is_used = False`) es lo que impide que dos
    peticiones simultáneas con el mismo código entreguen dos recargas: solo
    una ve filas afectadas.

    Devuelve el GiftCode ya reservado, o None si alguien se adelantó.
    """
    ahora = datetime.utcnow()
    try:
        tomado = (
            db.session.query(GiftCode)
            .filter(GiftCode.id == gift_id, GiftCode.is_used.is_(False))
            .update(
                {
                    'is_used': True,
                    'used_at': ahora,
                    'used_player_id': str(player_id or '').strip()[:100],
                    'used_zone_id': (str(zone_id or '').strip() or None),
                    'used_nickname': (str(nickname or '').strip()[:120] or None),
                },
                synchronize_session=False,
            )
        )
        if not tomado:
            db.session.rollback()
            return None
        db.session.commit()
    except Exception:
        db.session.rollback()
        return None

    return GiftCode.query.get(gift_id)


def release_code(gift):
    """Devuelve el código al estado sin usar si la entrega no se pudo crear."""
    if not gift:
        return
    try:
        gift.is_used = False
        gift.used_at = None
        gift.used_player_id = None
        gift.used_zone_id = None
        gift.used_nickname = None
        gift.order_id = None
        db.session.commit()
    except Exception:
        db.session.rollback()


def redeem_code(gift, player_id, zone_id=None, nickname=''):
    """Canjea un código ya validado y entrega la recarga.

    Devuelve (ok, mensaje, order).
    """
    from .order_processing import deliver_prize_to_player

    package = gift.package
    game = package.game if package else None
    if not game:
        return False, 'El premio de ese código ya no está disponible.', None

    reservado = claim_code(gift.id, player_id, zone_id=zone_id, nickname=nickname)
    if not reservado:
        return False, 'Ese código acaba de ser canjeado.', None

    order, approval = deliver_prize_to_player(
        game, package, player_id,
        zone_id=zone_id,
        note=f'Canje del código de regalo {format_code(gift.code)}.',
        reference_prefix='CANJE',
    )

    if not order:
        # No llegó ni a crearse la orden: el código vuelve a estar libre para
        # que el cliente no se quede sin su premio.
        release_code(reservado)
        return False, (approval or {}).get('message') or 'No se pudo procesar el canje.', None

    reservado.order_id = order.id
    db.session.commit()

    if approval and approval.get('ok'):
        return True, 'Listo, tu recarga fue enviada.', order

    # La orden quedó registrada pero sin entregar (por ejemplo, sin stock en
    # ese momento). El código NO se libera: ya tiene su orden asociada y el
    # admin la completa a mano, igual que cualquier compra sin stock.
    return True, (
        'Tu canje quedó registrado y lo estamos procesando. '
        'Si no llega en unos minutos, escríbenos por soporte con tu número de orden.'
    ), order


def get_redemption_history(player_id, limit=20):
    """Canjes hechos con este ID de jugador, del más nuevo al más viejo."""
    player_id = str(player_id or '').strip()
    if not player_id:
        return []
    return (
        GiftCode.query
        .filter(GiftCode.is_used.is_(True), GiftCode.used_player_id == player_id)
        .order_by(GiftCode.used_at.desc())
        .limit(int(limit or 20))
        .all()
    )


def serialize_redemption(gift):
    """Lo que ve el cliente en su historial: sin datos internos."""
    package = gift.package
    game = package.game if package else None
    order = gift.order
    return {
        'code': format_code(gift.code),
        'game': game.name if game else '—',
        'package': package.name if package else '—',
        'date': gift.used_at.strftime('%d/%m/%Y %H:%M') if gift.used_at else '',
        'order_number': order.order_number if order else '',
        'status': (order.status if order else 'pending'),
        'delivered': bool(order and order.status in ('approved', 'completed')),
    }
