"""Freno progresivo del login del admin por IP.

Regla pedida por el dueño:
- Los primeros 3 intentos fallidos pasan.
- Después, solo 1 intento cada 5 minutos.
- Al llegar a 10 fallos, la IP queda bloqueada 24 h para el login del admin,
  salvo que sea un navegador ya reconocido del dueño (`admin_device`).

Se guarda en `admin_login_attempts` (una fila por IP y tipo: password / 2fa).
"""
from datetime import datetime, timedelta

from ..models import AdminLoginAttempt, db

FIRST_ATTEMPTS = 3
RETRY_AFTER_SECONDS = 5 * 60
BLOCK_AFTER_FAILURES = 10
BLOCK_HOURS = 24


def _now():
    return datetime.utcnow()


def _row(ip, kind):
    return AdminLoginAttempt.query.filter_by(ip=ip, kind=kind).first()


def check(ip, kind='password', trusted=False, trusted_skips_wait=True):
    """Devuelve (permitido, motivo, segundos_para_reintentar).

    `trusted_skips_wait=True` deja pasar de inmediato al navegador del dueño
    (para la clave). Para el código 2FA se usa False: un dispositivo de
    confianza no recibe el bloqueo de 24 h, pero sí espera tras 3 fallos.
    """
    ip = (ip or '').strip()
    if not ip:
        return False, 'sin_ip', 0
    if trusted and trusted_skips_wait:
        return True, '', 0
    row = _row(ip, kind)
    if not row:
        return True, '', 0
    now = _now()
    if row.blocked_until and row.blocked_until > now and not trusted:
        seconds = max(1, int((row.blocked_until - now).total_seconds()))
        return False, 'blocked', seconds
    # Bloqueo vencido (o intentos muy viejos): se empieza de cero. Sin esto,
    # tras las 24 h el contador seguía en 10 y un solo error volvía a bloquear
    # otro día entero.
    if (row.blocked_until and row.blocked_until <= now) or (
        row.last_failure_at and row.last_failure_at < now - timedelta(hours=BLOCK_HOURS)
    ):
        row.failures = 0
        row.blocked_until = None
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    if int(row.failures or 0) >= FIRST_ATTEMPTS and row.last_failure_at:
        next_at = row.last_failure_at + timedelta(seconds=RETRY_AFTER_SECONDS)
        if now < next_at:
            return False, 'wait', max(1, int((next_at - now).total_seconds()))
    return True, '', 0


def register_failure(ip, kind='password', trusted=False):
    """Anota un fallo. Si es un dispositivo de confianza no bloquea la IP."""
    ip = (ip or '').strip()
    if not ip:
        return None
    row = _row(ip, kind)
    if not row:
        row = AdminLoginAttempt(ip=ip, kind=kind, failures=0)
        db.session.add(row)
        try:
            db.session.flush()
        except Exception:
            db.session.rollback()
            row = _row(ip, kind)
            if not row:
                return None
    now = _now()
    row.failures = int(row.failures or 0) + 1
    row.last_failure_at = now
    if not trusted and row.failures >= BLOCK_AFTER_FAILURES:
        row.blocked_until = now + timedelta(hours=BLOCK_HOURS)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return row


def reset(ip, kind=None):
    """Un login completo y correcto limpia los fallos de esa IP."""
    ip = (ip or '').strip()
    if not ip:
        return
    try:
        query = AdminLoginAttempt.query.filter_by(ip=ip)
        if kind:
            query = query.filter_by(kind=kind)
        query.delete(synchronize_session=False)
        db.session.commit()
    except Exception:
        db.session.rollback()


def active_blocks():
    now = _now()
    return (
        AdminLoginAttempt.query
        .filter(AdminLoginAttempt.blocked_until.isnot(None))
        .filter(AdminLoginAttempt.blocked_until > now)
        .order_by(AdminLoginAttempt.blocked_until.asc())
        .all()
    )


def unblock(ip, kind=None):
    reset(ip, kind)
