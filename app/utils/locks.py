"""Bloqueo distribuido simple respaldado por la base de datos.

Reemplaza los locks en memoria (threading.Lock) usados antes para "solo 1
verificación de Pabilo a la vez": ese lock solo protege dentro de un mismo
proceso Python. En producción la app corre con `gunicorn -w 3`, es decir 3
procesos separados, cada uno con su propio Lock() — no se coordinan entre
sí. Usar una fila de `process_locks` con expiración sí funciona igual sin
importar cuántos workers/procesos estén corriendo.
"""
import ipaddress
import os
import time
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from ..models import ApiRateLimitHit, ProcessLock, db


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


def check_rate_limit(bucket_key, limit, window_seconds=60):
    """Cuenta peticiones por ventana de tiempo fija (respaldado por la BD,
    igual razón que acquire_lock: un contador en memoria no se comparte
    entre los workers de Gunicorn). Devuelve True si la petición está
    permitida, False si ya se pasó del límite en la ventana actual.

    Falla "abierto": si algo sale mal guardando el conteo, deja pasar la
    petición en vez de bloquear a usuarios normales por un error interno.
    """
    epoch = int(time.time())
    window_epoch = epoch - (epoch % window_seconds)
    window_start = datetime.utcfromtimestamp(window_epoch)

    try:
        updated = (
            db.session.query(ApiRateLimitHit)
            .filter_by(bucket_key=bucket_key, window_start=window_start)
            .update({'count': ApiRateLimitHit.count + 1}, synchronize_session=False)
        )
        if updated:
            db.session.commit()
        else:
            db.session.add(ApiRateLimitHit(bucket_key=bucket_key, window_start=window_start, count=1))
            db.session.commit()
    except IntegrityError:
        # Otra petición creó la fila de esta ventana en el mismo instante:
        # ya existe, solo falta sumarle nuestra cuenta.
        db.session.rollback()
        try:
            db.session.query(ApiRateLimitHit).filter_by(
                bucket_key=bucket_key, window_start=window_start,
            ).update({'count': ApiRateLimitHit.count + 1}, synchronize_session=False)
            db.session.commit()
        except Exception:
            db.session.rollback()
            return True
    except Exception:
        db.session.rollback()
        return True

    row = ApiRateLimitHit.query.filter_by(bucket_key=bucket_key, window_start=window_start).first()
    return (row.count if row else 0) <= limit


RATE_LIMIT_RETENTION_HOURS = 6   # la ventana más larga que usa el sitio es de 1 hora
LOCK_RETENTION_DAYS = 1


def purge_stale_counters():
    """Borra los contadores de topes y los locks vencidos hace rato: sin esto `api_rate_limit_hits` crece para siempre
    (una fila por visitante y ventana). Lo corre el proceso de fondo cada pocos minutos; devuelve (contadores, locks) borrados."""
    removed = (0, 0)
    try:
        counters = (
            db.session.query(ApiRateLimitHit)
            .filter(ApiRateLimitHit.window_start < datetime.utcnow() - timedelta(hours=RATE_LIMIT_RETENTION_HOURS))
            .delete(synchronize_session=False)
        )
        locks = (
            db.session.query(ProcessLock)
            .filter(ProcessLock.expires_at < datetime.utcnow() - timedelta(days=LOCK_RETENTION_DAYS))
            .delete(synchronize_session=False)
        )
        db.session.commit()
        removed = (counters or 0, locks or 0)
    except Exception:
        db.session.rollback()
    return removed


def rate_limit_count(bucket_key, window_seconds):
    """Cuántos intentos lleva la ventana actual de `bucket_key`, SIN contar uno nuevo (si falla la base, 0)."""
    epoch = int(time.time())
    window_start = datetime.utcfromtimestamp(epoch - (epoch % window_seconds))
    try:
        row = ApiRateLimitHit.query.filter_by(bucket_key=bucket_key, window_start=window_start).first()
        return int(row.count) if row else 0
    except Exception:
        db.session.rollback()
        return 0


# Freno general de la clave del administrador: cuenta las claves EQUIVOCADAS que alguien probó para el correo/usuario
# del admin, sumando todas las IP. Un humano no llega a 60 en 15 minutos; un programa que prueba claves sí.
ADMIN_FAIL_BUCKET = 'admin_password_failures'
ADMIN_FAIL_MAX = 60
ADMIN_FAIL_WINDOW_SECONDS = 15 * 60


ADMIN_DEVICE_COOKIE = 'admin_device'
ADMIN_DEVICE_MAX_AGE = 30 * 24 * 3600


def _admin_device_serializer():
    from flask import current_app
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.secret_key, salt='admin-device-v1')


def _admin_password_fingerprint():
    import hashlib
    password = (os.environ.get('ADMIN_PASSWORD') or '').strip()
    return hashlib.sha256(('admin-device:' + password).encode('utf-8')).hexdigest()[:24]


def remember_admin_device(response):
    """Marca ESTE navegador como el del dueño (cookie firmada, 30 días) después de un acceso correcto. Así el freno general de claves
    equivocadas nunca deja al dueño afuera aunque otra persona haya fallado 60 veces; si cambia ADMIN_PASSWORD la marca deja de valer."""
    from flask import request
    try:
        token = _admin_device_serializer().dumps({'p': _admin_password_fingerprint()})
        response.set_cookie(
            ADMIN_DEVICE_COOKIE, token, max_age=ADMIN_DEVICE_MAX_AGE, httponly=True, samesite='Lax',
            secure=request.is_secure,
        )
    except Exception:
        pass
    return response


def admin_device_trusted():
    from flask import has_request_context, request
    if not has_request_context():
        return False
    token = request.cookies.get(ADMIN_DEVICE_COOKIE) or ''
    if not token:
        return False
    try:
        data = _admin_device_serializer().loads(token, max_age=ADMIN_DEVICE_MAX_AGE)
    except Exception:
        return False
    return isinstance(data, dict) and data.get('p') == _admin_password_fingerprint()


def admin_login_locked():
    """True cuando hubo demasiadas claves equivocadas del admin en total Y quien entra no es un navegador ya reconocido del dueño."""
    if rate_limit_count(ADMIN_FAIL_BUCKET, ADMIN_FAIL_WINDOW_SECONDS) < ADMIN_FAIL_MAX:
        return False
    return not admin_device_trusted()


def register_admin_login_failure():
    check_rate_limit(ADMIN_FAIL_BUCKET, ADMIN_FAIL_MAX, ADMIN_FAIL_WINDOW_SECONDS)


def _trusted_proxy_count():
    """Cuántos servidores de TU lado (nginx, y Cloudflare si lo usas) agregan su dato a X-Forwarded-For. Por defecto 1: solo nginx."""
    try:
        return min(max(int(os.environ.get('TRUSTED_PROXY_COUNT') or 1), 1), 5)
    except ValueError:
        return 1


def client_ip():
    """IP real del visitante detrás de nginx (remote_addr a secas sería 127.0.0.1).

    NO se usa el primer valor de X-Forwarded-For: ese lo escribe el propio visitante y con él cualquiera
    podía inventarse una IP nueva en cada intento y saltarse todos los topes. El que sí es de fiar es el
    ÚLTIMO (lo agrega nginx con la IP que de verdad le llegó), así que se cuenta desde la derecha.
    Si nginx sobrescribe la cabecera con $remote_addr (como pide el README) hay un solo valor y da lo mismo.
    """
    from flask import request

    peer = request.remote_addr or ''
    # X-Forwarded-For solo se cree si la conexión viene de TU nginx (la misma máquina o una red privada, o una IP que pongas en
    # TRUSTED_PROXY_IPS). Si alguien le habla directo a gunicorn (puerto abierto al mundo), esa cabecera la escribe él: se ignora.
    if _peer_is_trusted_proxy(peer):
        parts = [p.strip() for p in (request.headers.get('X-Forwarded-For') or '').split(',') if p.strip()]
        if parts:
            hops = _trusted_proxy_count()
            candidate = parts[-hops] if len(parts) >= hops else parts[0]
            normalized = _normalize_ip(candidate)
            if normalized:
                return normalized
    return _normalize_ip(peer) or peer


def _peer_is_trusted_proxy(peer):
    try:
        address = ipaddress.ip_address((peer or '').split('%')[0])
    except ValueError:
        return False
    if address.is_loopback or address.is_private:
        return True
    extra = [p.strip() for p in (os.environ.get('TRUSTED_PROXY_IPS') or '').split(',') if p.strip()]
    return str(address) in extra


def _normalize_ip(value):
    """IP en texto canónico. Las IPv6 se agrupan por /64 (una conexión doméstica reparte miles de direcciones distintas: sin esto cada
    petición podía parecer un visitante nuevo y saltarse los topes) y se descarta el '%scope' que Python acepta."""
    try:
        address = ipaddress.ip_address((value or '').split('%')[0])
    except ValueError:
        return ''
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f'{address}/64', strict=False).network_address)
    return str(address)
