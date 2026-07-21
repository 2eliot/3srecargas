"""Bloqueo distribuido simple respaldado por la base de datos.

Reemplaza los locks en memoria (threading.Lock) usados antes para "solo 1
verificación de Pabilo a la vez": ese lock solo protege dentro de un mismo
proceso Python. En producción la app corre con `gunicorn -w 3`, es decir 3
procesos separados, cada uno con su propio Lock() — no se coordinan entre
sí. Usar una fila de `process_locks` con expiración sí funciona igual sin
importar cuántos workers/procesos estén corriendo.
"""
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from ..models import ProcessLock, db


def acquire_lock(key, ttl_seconds, holder):
    """Intenta tomar el lock `key` de forma no bloqueante.

    Devuelve True si se obtuvo el lock (nuevo o porque el anterior ya
    expiró), False si otro proceso lo tiene vigente en este momento.
    """
    now = datetime.utcnow()
    expires_at = now + timedelta(seconds=ttl_seconds)

    try:
        updated = (
            db.session.query(ProcessLock)
            .filter(ProcessLock.key == key, ProcessLock.expires_at < now)
            .update({'holder': holder, 'expires_at': expires_at}, synchronize_session=False)
        )
        if updated:
            db.session.commit()
            return True

        if ProcessLock.query.filter_by(key=key).first():
            db.session.rollback()
            return False

        db.session.add(ProcessLock(key=key, holder=holder, expires_at=expires_at))
        db.session.commit()
        return True
    except IntegrityError:
        db.session.rollback()
        return False
    except Exception:
        db.session.rollback()
        return False


def release_lock(key, holder):
    """Libera el lock si `holder` es quien lo tiene actualmente.

    Si el lock ya expiró o lo tomó otro proceso, no hace nada (evita
    liberar por error un lock que ya no es nuestro).
    """
    try:
        db.session.query(ProcessLock).filter(
            ProcessLock.key == key, ProcessLock.holder == holder
        ).update({'expires_at': datetime.utcnow() - timedelta(seconds=1)}, synchronize_session=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
