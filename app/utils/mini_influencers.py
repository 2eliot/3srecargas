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
from datetime import datetime, timedelta
from uuid import uuid4

from ..models import Affiliate, AffiliateWithdrawal, MiniNotification, MiniRank, Order, db
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


def get_rank_progress(affiliate):
    uses = count_qualifying_uses(affiliate.id)
    ranks = MiniRank.query.order_by(MiniRank.uses_required.asc()).all()
    paid = set(_ranks_paid_list(affiliate.ranks_paid))

    reached = [r for r in ranks if uses >= r.uses_required]
    upcoming = [r for r in ranks if uses < r.uses_required]
    current = reached[-1] if reached else None
    nxt = upcoming[0] if upcoming else None

    progress_percent = 100.0
    if nxt:
        base = current.uses_required if current else 0
        span = max(nxt.uses_required - base, 1)
        progress_percent = round(min(max((uses - base) / span * 100, 0), 100), 1)

    return {
        'uses': uses,
        'current': current,
        'next': nxt,
        'uses_to_next': max(nxt.uses_required - uses, 0) if nxt else 0,
        'progress_percent': progress_percent,
        'paid': sorted(paid),
        'unpaid': [r for r in reached if r.name not in paid],
    }


def unpaid_ranks_reached(affiliate):
    return get_rank_progress(affiliate)['unpaid']


def award_rank_bonus(affiliate, rank_name, bonus_amount=None):
    """Paga el bono de un rango ya alcanzado y no cobrado. Idempotente: si
    el rango no está en unpaid_ranks_reached (ya pagado, o no alcanzado
    todavía), no hace nada."""
    match = next((r for r in unpaid_ranks_reached(affiliate) if r.name == rank_name), None)
    if not match:
        return False, 'Ese rango no está pendiente de pago.'

    amount = float(bonus_amount) if bonus_amount is not None else float(match.bonus_amount)
    if amount < 0:
        return False, 'Monto inválido.'

    paid = _ranks_paid_list(affiliate.ranks_paid)
    paid.append(match.name)
    affiliate.ranks_paid = ','.join(paid)
    affiliate.balance = float(affiliate.balance or 0) + amount
    affiliate.total_earned = float(affiliate.total_earned or 0) + amount
    notify_mini(affiliate, f'🏆 ¡Alcanzaste el rango {match.name}! Se te acreditó un premio de ${amount:.2f}.')
    db.session.commit()
    return True, None


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

        affiliate.balance = float(affiliate.balance or 0) - float(withdrawal.amount)
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
