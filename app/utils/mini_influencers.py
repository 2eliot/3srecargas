"""Lógica del autoservicio de mini influencers (/minis).

Un mini influencer es un `Affiliate` con is_mini=True: mismo código, mismo
descuento al cliente y misma comisión por venta que cualquier afiliado
(process_affiliate_commission en order_processing.py no cambia). Lo que
agrega este módulo es la capa de encima: progreso de rango, videos como
prueba de alcance y retiro de balance — cada una con su propio guard de
idempotencia porque, a diferencia de las órdenes, no hay un lock de
aprobación compartido que ya las proteja.
"""

import re
from datetime import datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

from ..models import Affiliate, AffiliateWithdrawal, MiniRank, MiniVideo, MiniViewTier, Order, db
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


# ─── Video: plataforma y código temporal ────────────────────────────────────

def detect_platform(url):
    host = (urlparse(url).netloc or '').lower()
    if 'tiktok.' in host:
        return 'tiktok'
    if 'youtube.' in host or 'youtu.be' in host:
        return 'youtube'
    if 'instagram.' in host:
        return 'instagram'
    if 'facebook.' in host or 'fb.watch' in host:
        return 'facebook'
    if 'kwai' in host:
        return 'kwai'
    return 'otro'


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


def suggested_reward_for_views(views):
    tier = (
        MiniViewTier.query
        .filter(MiniViewTier.min_views <= views)
        .filter((MiniViewTier.max_views.is_(None)) | (MiniViewTier.max_views >= views))
        .order_by(MiniViewTier.min_views.desc())
        .first()
    )
    return float(tier.reward_amount) if tier else 0.0


# ─── Rango ───────────────────────────────────────────────────────────────────

def count_qualifying_uses(affiliate_id):
    """Órdenes con el código del mini que ya cobraron: mismo criterio
    (status approved/completed) que ya dispara process_affiliate_commission
    en order_processing.py, así rango y comisión quedan sincronizados sin
    un contador aparte."""
    return Order.query.filter(
        Order.affiliate_id == affiliate_id,
        Order.status.in_(['approved', 'completed']),
    ).count()


def _ranks_paid_list(raw):
    return [name.strip() for name in (raw or '').split(',') if name.strip()]


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
    db.session.commit()
    return True, None


# ─── Videos ──────────────────────────────────────────────────────────────────

def review_mini_video(video, action, reward_amount=None, note=''):
    """Aprueba (acreditando el monto que confirme el admin) o rechaza un
    video. Idempotente: un video que ya salió de 'pending' no se puede
    revisar de nuevo, así un doble clic del admin no acredita dos veces."""
    if video.status != 'pending':
        return False, 'Este video ya fue revisado.'

    video.note = (note or '').strip()[:500]
    video.reviewed_at = datetime.utcnow()

    if action == 'approve':
        amount = round(float(reward_amount or 0), 2)
        if amount < 0:
            return False, 'Monto inválido.'
        video.status = 'approved'
        video.reward_amount = amount
        if amount > 0:
            affiliate = video.affiliate
            affiliate.balance = float(affiliate.balance or 0) + amount
            affiliate.total_earned = float(affiliate.total_earned or 0) + amount
    elif action == 'reject':
        video.status = 'rejected'
        video.reward_amount = 0
    else:
        return False, 'Acción inválida.'

    db.session.commit()
    return True, None


# ─── Retiros ─────────────────────────────────────────────────────────────────

def approve_withdrawal(withdrawal):
    """Aprueba un retiro y debita el balance del afiliado. Lock por
    afiliado + revalidación de saldo dentro del lock: dos aprobaciones casi
    simultáneas (doble clic, dos pestañas admin) no pueden dejar el balance
    negativo — la segunda ve el balance ya descontado y falla en vez de
    debitar de más."""
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
        db.session.commit()
        return True, None
    finally:
        release_lock(lock_key, holder)


def reject_withdrawal(withdrawal, reason=''):
    if withdrawal.status != 'pending':
        return False, 'Este retiro ya fue procesado.'
    withdrawal.status = 'rejected'
    withdrawal.rejection_reason = (reason or '').strip()[:300] or None
    withdrawal.reviewed_at = datetime.utcnow()
    db.session.commit()
    return True, None
