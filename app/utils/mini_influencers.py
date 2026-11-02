"""Lógica del autoservicio de mini influencers (/minis).

Un mini influencer es un `Affiliate` con is_mini=True: mismo código, mismo
descuento al cliente y misma comisión por venta que cualquier afiliado
(process_affiliate_commission en order_processing.py no cambia). Lo que
agrega este módulo es la capa de encima: progreso de rango y retiro de
balance — cada una con su propio guard de idempotencia porque, a diferencia
de las órdenes, no hay un lock de aprobación compartido que ya las proteja.
El mini solo cobra por los usos de su código (escalera de rangos): los videos
como prueba de alcance se quitaron el 2026-10-21 (las tablas mini_videos y
mini_view_tiers quedan con su historial, sin uso).
"""

import re
import threading
import time
from datetime import datetime, timedelta
from uuid import uuid4

from ..models import Affiliate, AffiliateWithdrawal, MiniNotification, MiniRank, Order, Setting, db
from .locks import acquire_lock, release_lock

# TTL del lock de aprobación de retiro: alcanza de sobra para una operación
# de BD simple; si el proceso muere a mitad de camino, se libera solo.
WITHDRAWAL_APPROVE_LOCK_TTL_SECONDS = 30

# Límite de intentos fallidos de login/registro por IP en /minis, mismo
# criterio que gift_codes.py pero con su propio contador: son features
# distintas y no deben compartir ventana.
ATTEMPT_LIMIT = 12
ATTEMPT_WINDOW_MINUTES = 15
_attempts = {}


# ─── Límite de intentos ──────────────────────────────────────────────────────

def _prune_attempts(now):
    corte = now - timedelta(minutes=ATTEMPT_WINDOW_MINUTES)
    for key in [k for k, v in _attempts.items() if v['first'] < corte]:
        _attempts.pop(key, None)


def register_failed_attempt(ip):
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


# ─── Links y código temporal ────────────────────────────────────────────────

def normalize_video_url(raw):
    trimmed = (raw or '').strip()
    if not trimmed:
        return ''
    return trimmed if re.match(r'^https?://', trimmed, re.IGNORECASE) else f'https://{trimmed}'


def generate_temp_code():
    """Código provisorio para satisfacer la constraint NOT NULL/UNIQUE de
    Affiliate.code mientras la solicitud sigue pending. El admin lo ve
    precargado (editable) en el modal de aprobación, igual que hoy lo
    teclea a mano en /admin/affiliates."""
    for _ in range(10):
        candidate = f'MINI{uuid4().hex[:8].upper()}'
        if not Affiliate.query.filter_by(code=candidate).first():
            return candidate
    # Colisión 10 veces seguidas es prácticamente imposible; si pasara, un
    # sufijo más largo la resuelve sin más reintentos.
    return f'MINI{uuid4().hex[:16].upper()}'


# ─── Rango ───────────────────────────────────────────────────────────────────

def count_qualifying_uses(affiliate_id):
    """Órdenes con el código del mini que ya cobraron: mismo criterio
    (status approved/completed) que ya dispara process_affiliate_commission
    en order_processing.py, así rango y comisión quedan sincronizados sin
    un contador aparte. Las compras del propio mini con su código (mismo correo) no cuentan: tiene el descuento pero no sube de rango con ellas."""
    query = Order.query.filter(
        Order.affiliate_id == affiliate_id,
        Order.status.in_(['approved', 'completed']),
    )
    affiliate = db.session.get(Affiliate, affiliate_id)
    owner_email = ((affiliate.email if affiliate else '') or '').strip().lower()
    if owner_email:
        query = query.filter(db.func.lower(db.func.trim(db.func.coalesce(Order.email, ''))) != owner_email)
    return query.count()


def _ranks_paid_list(raw):
    return [name.strip() for name in (raw or '').split(',') if name.strip()]


# ─── Notificaciones ────────────────────────────────────────────────────────

def notify_mini(affiliate, message):
    """Deja un aviso en el panel del mini. No hace commit: siempre se llama
    justo antes del commit que ya cierra la operación que dispara el aviso,
    para que ambos queden atómicos (si algo falla después, no queda un
    aviso huérfano de un cambio que no se guardó)."""
    db.session.add(MiniNotification(affiliate_id=affiliate.id, message=message[:300]))


def ordered_ladder(ranks):
    """La escalera en el orden en que se sube: el "Orden" que puso el admin (si dos tienen el mismo, va primero el que se creó antes).
    Los usos de cada rango ya no sirven para ordenar: se cuentan desde el rango anterior."""
    return sorted(ranks, key=lambda r: (r.sort_order if r.sort_order is not None else 100, r.id or 0))


def _paid_keys(raw):
    """Nombres de rango ya pagados, sin distinguir mayúsculas (ranks_paid guarda los NOMBRES separados por coma)."""
    return {name.casefold() for name in _ranks_paid_list(raw)}


def rank_name_is_safe(name):
    """Un nombre con coma se leería como DOS en ranks_paid y el rango nunca quedaría pagado (con el pago automático se
    acreditaría una y otra vez): no se permite."""
    return ',' not in (name or '')


def rename_rank_in_paid(old_name, new_name):
    """Al renombrar un rango, cambia su nombre también en lo ya pagado a cada creador. Sin esto el rango renombrado
    parecería sin pagar y con el pago automático se acreditaría otra vez. Devuelve a cuántos creadores se les cambió.
    No hace commit: lo cierra la ruta que renombra, en el mismo commit."""
    old_key = (old_name or '').strip().casefold()
    new_name = (new_name or '').strip()
    if not old_key or (old_name or '').strip() == new_name:
        return 0
    changed = 0
    for aff in Affiliate.query.filter(Affiliate.ranks_paid.isnot(None), Affiliate.ranks_paid != '').all():
        tokens = _ranks_paid_list(aff.ranks_paid)
        new_tokens = [new_name if token.casefold() == old_key else token for token in tokens]
        if new_tokens != tokens:
            aff.ranks_paid = ','.join(new_tokens)
            changed += 1
    return changed


def ladder_with_totals(ranks):
    """[(rango, usos en total para alcanzarlo), ...] en orden de subida.

    Los "usos requeridos" de cada rango se cuentan DESDE QUE SE ALCANZÓ EL ANTERIOR (la barra de cada rango arranca en 0):
    Bronce 65 → Plata 120 más → Oro 360 más. Un creador con 93 usos ya alcanzó Bronce (65) y le quedan 28 contando hacia Plata:
    el sobrante pasa al siguiente rango. Por eso el total para llegar a cada rango es la suma de los usos de los anteriores."""
    out = []
    total = 0
    for rank in ordered_ladder(ranks):
        total += max(int(rank.uses_required or 0), 1)
        out.append((rank, total))
    return out


def get_rank_progress(affiliate, ranks=None):
    """Rango y avance de un creador. `ranks` se puede pasar ya cargado para no volver a pedirlos por cada creador de una lista.

    'uses' son todos sus usos; 'tier_uses' lo que lleva en el rango actual (ya restado lo de los rangos alcanzados) y
    'tier_required' lo que pide el siguiente: la barra va de 0 a tier_required."""
    uses = count_qualifying_uses(affiliate.id)
    if ranks is None:
        ranks = MiniRank.query.all()
    ladder = ladder_with_totals(ranks)
    paid = set(_ranks_paid_list(affiliate.ranks_paid))
    paid_keys = _paid_keys(affiliate.ranks_paid)

    reached = [(r, total) for r, total in ladder if uses >= total]
    upcoming = [(r, total) for r, total in ladder if uses < total]
    current = reached[-1][0] if reached else None
    base = reached[-1][1] if reached else 0
    nxt, nxt_total = upcoming[0] if upcoming else (None, None)

    tier_uses = uses - base
    tier_required = (nxt_total - base) if nxt else None
    progress_percent = 100.0
    if nxt:
        progress_percent = round(min(max(tier_uses / max(tier_required, 1) * 100, 0), 100), 1)

    return {
        'uses': uses,
        'tier_uses': tier_uses,
        'tier_required': tier_required,
        'current': current,
        'next': nxt,
        'uses_to_next': max(nxt_total - uses, 0) if nxt else 0,
        'progress_percent': progress_percent,
        'paid': sorted(paid),
        # Un rango con coma en el nombre nunca se da por pagado (rank_name_is_safe): mejor no ofrecerlo que pagarlo cada vez.
        'unpaid': [r for r, _ in reached if r.name.casefold() not in paid_keys and rank_name_is_safe(r.name)],
    }


def unpaid_ranks_reached(affiliate):
    return get_rank_progress(affiliate)['unpaid']


def award_rank_bonus(affiliate, rank_name, bonus_amount=None):
    """Paga el bono de un rango ya alcanzado y no cobrado. Idempotente: si
    el rango no está en unpaid_ranks_reached (ya pagado, o no alcanzado
    todavía), no hace nada."""
    # Mismo candado por creador que el pago automático (RANK_LOCK_TTL_SECONDS): el botón y el proceso de fondo no pueden acreditar
    # el mismo rango a la vez.
    lock_key = f'mini_rank:{affiliate.id}'
    holder = uuid4().hex
    if not acquire_lock(lock_key, RANK_LOCK_TTL_SECONDS, holder):
        return False, 'Ese creador se está procesando en este momento, intenta de nuevo en unos segundos.'
    try:
        db.session.refresh(affiliate)
        match = next((r for r in unpaid_ranks_reached(affiliate) if r.name == rank_name), None)
        if not match:
            return False, 'Ese rango no está pendiente de pago.'

        amount = float(bonus_amount) if bonus_amount is not None else float(match.bonus_amount)
        if amount < 0:
            return False, 'Monto inválido.'

        paid = _ranks_paid_list(affiliate.ranks_paid)
        paid.append(match.name)
        affiliate.ranks_paid = ','.join(paid)
        # Suma hecha por la base (balance = balance + monto), no con el valor leído antes: así no pisa un retiro o una comisión
        # que se guarden al mismo tiempo.
        affiliate.balance = Affiliate.balance + round(amount, 2)
        affiliate.total_earned = Affiliate.total_earned + round(amount, 2)
        notify_mini(affiliate, f'🏆 ¡Alcanzaste el rango {match.name}! Se te acreditó un premio de ${amount:.2f}.')
        db.session.commit()
        return True, None
    finally:
        release_lock(lock_key, holder)


# ─── Pago automático de los rangos ───────────────────────────────────────────
# "Pagar" un rango es ACREDITAR su bono al saldo del creador (después él pide el retiro y ahí sí paga el admin). Ya no hace falta
# apretar el botón: apenas el creador llega a los usos de un rango, el bono se acredita solo. Lo hace el proceso de fondo cada
# RANK_SWEEP_EVERY_SECONDS y también al abrir el creador su panel. Se puede apagar en Admin → Código de creador → Configuración.

RANK_AUTO_PAY_KEY = 'mini_rank_auto_pay'
RANK_LOCK_TTL_SECONDS = 30
RANK_SWEEP_EVERY_SECONDS = 120


def rank_auto_pay_enabled():
    setting = Setting.query.filter_by(key=RANK_AUTO_PAY_KEY).first()
    return (setting.value if setting else 'true') != 'false'


def award_pending_rank_bonuses(affiliate):
    """Acredita al saldo TODOS los rangos que el creador ya alcanzó y no se le han pagado. Devuelve [(rango, monto), ...].

    Idempotente y seguro con dos llamadas a la vez: lock por creador y se vuelve a leer al creador dentro del lock, así el mismo
    rango nunca se acredita dos veces (queda anotado en ranks_paid en el mismo commit que el saldo). No hace nada si el pago
    automático está apagado, si el creador no está aprobado/activo o si ya está al día."""
    try:
        if affiliate is None or not rank_auto_pay_enabled():
            return []
        if not unpaid_ranks_reached(affiliate):
            return []   # al día: ni se toma el candado (esto corre cada vez que un creador abre su panel)
    except Exception:
        db.session.rollback()
        return []
    lock_key = f'mini_rank:{affiliate.id}'
    holder = uuid4().hex
    if not acquire_lock(lock_key, RANK_LOCK_TTL_SECONDS, holder):
        return []
    try:
        aff = db.session.get(Affiliate, affiliate.id)
        if aff is None or not aff.is_mini or aff.status != 'approved' or not aff.is_active:
            return []
        db.session.refresh(aff)
        pending = unpaid_ranks_reached(aff)
        if not pending:
            return []

        paid = _ranks_paid_list(aff.ranks_paid)
        granted = []
        total_amount = 0.0
        for rank in pending:
            amount = float(rank.bonus_amount or 0)
            paid.append(rank.name)
            total_amount += amount
            granted.append((rank.name, amount))
            if amount > 0:
                notify_mini(aff, f'🏆 ¡Alcanzaste el rango {rank.name}! Se acreditaron ${amount:.2f} a tu saldo; ya puedes retirarlos en Retiros.')
            else:
                notify_mini(aff, f'🏆 ¡Alcanzaste el rango {rank.name}!')
        # UNA sola suma hecha por la base (balance = balance + total): no pisa un retiro o una comisión guardados al mismo tiempo.
        # (Una asignación por rango no sirve: la última reemplazaría a las anteriores y solo se acreditaría el último.)
        total_amount = round(total_amount, 2)
        aff.balance = Affiliate.balance + total_amount
        aff.total_earned = Affiliate.total_earned + total_amount
        aff.ranks_paid = ','.join(paid)
        db.session.commit()
        return granted
    except Exception:
        db.session.rollback()
        return []
    finally:
        release_lock(lock_key, holder)


def sweep_rank_bonuses():
    """Revisa a todos los creadores aprobados y acredita los rangos alcanzados que falten. Devuelve cuántos rangos se acreditaron."""
    if not rank_auto_pay_enabled():
        return 0
    ranks = MiniRank.query.all()
    if not ranks:
        return 0
    # Usos que hacen falta para el primer rango de la escalera (los demás se cuentan desde ahí).
    min_uses = ladder_with_totals(ranks)[0][1]
    query = Affiliate.query.filter(Affiliate.is_mini.is_(True), Affiliate.status == 'approved', Affiliate.is_active.is_(True))
    if min_uses > 0:
        # Primero solo los que tienen órdenes cobradas de sobra para el primer rango (una sola consulta); la cuenta exacta
        # (sin sus compras propias) la hace award_pending_rank_bonuses.
        rows = (
            db.session.query(Order.affiliate_id)
            .filter(Order.affiliate_id.isnot(None), Order.status.in_(['approved', 'completed']))
            .group_by(Order.affiliate_id)
            .having(db.func.count(Order.id) >= min_uses)
            .all()
        )
        query = query.filter(Affiliate.id.in_([r[0] for r in rows] or [0]))
    total = 0
    for aff in query.all():
        # Un creador que falle (por ejemplo, borrado a mitad del barrido) no frena a los demás.
        try:
            total += len(award_pending_rank_bonuses(aff))
        except Exception:
            db.session.rollback()
    return total


_sweep_guard = threading.Lock()
_last_sweep_attempt = 0.0


def run_scheduled_rank_sweep():
    """Lo llama el proceso de fondo en cada vuelta: solo trabaja una vez cada RANK_SWEEP_EVERY_SECONDS entre todos los workers
    (el lock NO se libera: su vencimiento es lo que espacia las revisiones)."""
    global _last_sweep_attempt
    with _sweep_guard:
        now = time.monotonic()
        if now - _last_sweep_attempt < 30:
            return None
        _last_sweep_attempt = now
    if not acquire_lock('mini_rank_sweep', RANK_SWEEP_EVERY_SECONDS, f'{threading.get_ident()}:{id(_sweep_guard)}'):
        return None
    return sweep_rank_bonuses()


# ─── Retiros ─────────────────────────────────────────────────────────────────

def approve_withdrawal(withdrawal, proof_path=None):
    """Aprueba un retiro y debita el balance del afiliado. Lock por
    afiliado + revalidación de saldo dentro del lock: dos aprobaciones casi
    simultáneas (doble clic, dos pestañas admin) no pueden dejar el balance
    negativo — la segunda ve el balance ya descontado y falla en vez de
    debitar de más.

    `proof_path` es la captura de pago (opcional) que el admin adjunta al
    aprobar, para que el mini la vea en su propio panel."""
    lock_key = f'mini_withdrawal:{withdrawal.affiliate_id}'
    holder = uuid4().hex
    if not acquire_lock(lock_key, WITHDRAWAL_APPROVE_LOCK_TTL_SECONDS, holder):
        return False, 'Este retiro se está procesando en este momento, intenta de nuevo en unos segundos.'

    try:
        db.session.refresh(withdrawal)
        if withdrawal.status != 'pending':
            return False, 'Este retiro ya fue procesado.'

        affiliate = withdrawal.affiliate
        db.session.refresh(affiliate)
        if float(affiliate.balance or 0) < float(withdrawal.amount):
            return False, 'El afiliado ya no tiene saldo suficiente para este retiro.'

        # Resta hecha por la base (balance = balance - monto): no pisa un bono o una comisión que se acrediten al mismo tiempo.
        affiliate.balance = Affiliate.balance - float(withdrawal.amount)
        withdrawal.status = 'approved'
        withdrawal.reviewed_at = datetime.utcnow()
        if proof_path:
            withdrawal.payment_proof = proof_path
        notify_mini(affiliate, f'✅ Tu retiro de ${float(withdrawal.amount):.2f} fue aprobado.' + (' Ya puedes ver tu comprobante de pago en Retiros.' if proof_path else ''))
        db.session.commit()
        return True, None
    finally:
        release_lock(lock_key, holder)


def attach_withdrawal_proof(withdrawal, proof_path):
    """Pone (o cambia) el comprobante de pago de un retiro YA aprobado y le avisa al mini. Devuelve (ok, error, comprobante_anterior).
    El archivo viejo lo borra quien llama (la ruta del admin), solo si esto salió bien."""
    if withdrawal.status != 'approved':
        return False, 'Solo se puede adjuntar el comprobante a un retiro aprobado.', None
    if not proof_path:
        return False, 'No se pudo guardar el comprobante.', None
    previous = withdrawal.payment_proof
    withdrawal.payment_proof = proof_path
    notify_mini(withdrawal.affiliate, f'📎 Ya puedes ver el comprobante de tu retiro de ${float(withdrawal.amount):.2f}. Está en la sección Retiros.')
    db.session.commit()
    return True, None, previous


def reject_withdrawal(withdrawal, reason=''):
    if withdrawal.status != 'pending':
        return False, 'Este retiro ya fue procesado.'
    withdrawal.status = 'rejected'
    withdrawal.rejection_reason = (reason or '').strip()[:300] or None
    withdrawal.reviewed_at = datetime.utcnow()
    reason_txt = f' Motivo: {withdrawal.rejection_reason}' if withdrawal.rejection_reason else ''
    notify_mini(withdrawal.affiliate, f'❌ Tu retiro de ${float(withdrawal.amount):.2f} fue rechazado.{reason_txt}')
    db.session.commit()
    return True, None
