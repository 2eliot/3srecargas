"""Binance Pay automatic payment verification.

Flujo:
  1. El cliente elige Binance Pay en el checkout.
  2. El sistema genera un código numérico único de 6 dígitos (ej. "483921").
  3. El cliente transfiere el monto exacto en USDT a la dirección de Binance
     Pay configurada y escribe el código de 6 dígitos en la Nota/Memo.
  4. Al guardar la orden se lanza un hilo dedicado SOLO para esa orden, y
     además el scheduler de recuperación en segundo plano
     (utils/order_scheduler.py) la reintenta periódicamente — así la
     verificación sobrevive a un reinicio de worker o a un despliegue.
  5. Todas las llamadas a Binance salen por BINANCE_PROXY si está definido.

Cómo se empareja un pago con una orden
--------------------------------------
El memo del emisor NO siempre llega al historial del receptor: en producción
una parte importante de las transferencias C2C entrantes reales aparece con
``note: ''`` aunque el cliente sí escribió el código. Por eso hay dos
estrategias, en este orden:

  1. **Por código** — el memo contiene los 6 dígitos. Es la vía normal.
  2. **Por monto** (respaldo, `BINANCE_MATCH_BY_AMOUNT`) — transacción
     entrante en USDT, dentro de la ventana de tiempo de la orden, con el
     monto esperado. Solo se acepta si NO hay ambigüedad: si otra orden
     pendiente de Binance espera ese mismo monto, o si hay más de una
     transacción candidata, no se acredita nada y se deja una nota para
     revisión manual. Sin esto, todo pago con memo vacío quedaba sin
     verificar y había que aprobarlo a mano.

Cada transacción acreditada queda registrada en `Order.binance_tx_id` (índice
único), así que una misma transferencia jamás puede saldar dos órdenes.

Variables de entorno:
  BINANCE_API_KEY                  – API key de Binance (permisos pay-history)
  BINANCE_API_SECRET               – API secret
  BINANCE_PROXY                    – (opcional) ej. http://user:pass@host:port
  BINANCE_REQUEST_TIMEOUT_SECONDS  – timeout por endpoint (default 15)
  BINANCE_LOOKBACK_MINUTES         – cuánto se mira hacia atrás (default 90)
  BINANCE_VERIFY_WINDOW_MINUTES    – cuánto se sigue buscando (default 180)
  BINANCE_AMOUNT_TOLERANCE         – margen bajo el monto esperado (default 0.02)
  BINANCE_MATCH_BY_AMOUNT          – 'false' desactiva el respaldo por monto

Settings en BD (modelo Setting):
  binance_auto_enabled     – '1' activa, '0' / ausente desactiva
  binance_wallet_address   – dirección/correo que se muestra al cliente
"""

import hashlib
import hmac
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import requests

_BINANCE_ENDPOINTS = [
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api.binance.com",
]
_BINANCE_PAY_PATH = "/sapi/v1/pay/transactions"

# Intervalo entre consultas del hilo dedicado a una orden.
_ORDER_POLL_INTERVAL = 30
# Espera inicial para que el commit de la orden sea visible.
_ORDER_POLL_INITIAL_DELAY = 12
# TTL del lock por orden: cubre una verificación + aprobación completa.
_VERIFY_LOCK_TTL_SECONDS = 300


# ── Firma ─────────────────────────────────────────────────────────────────────

def _sign(secret: str, query_string: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _to_epoch_ms(value: datetime) -> int:
    """Convierte un datetime a epoch en milisegundos tratándolo como UTC.

    Los `created_at` de la app se guardan con `datetime.utcnow()`, es decir
    naive pero en UTC. `datetime.timestamp()` sobre un naive lo interpreta
    como hora LOCAL del servidor: en una máquina con TZ distinta de UTC eso
    desplazaba `startTime` varias horas (hacia el futuro con TZ negativa) y
    Binance devolvía cero transacciones siempre.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)


# ── Consulta de transacciones vía proxy ───────────────────────────────────────

def _fetch_pay_transactions(api_key: str, api_secret: str, proxy: str, timeout: float,
                            start_time_ms: int, limit: int = 100):
    """Consulta el historial de Binance Pay a través del proxy configurado.

    Devuelve la lista de transacciones, o None si ningún endpoint respondió
    correctamente (para poder distinguir "no encontrado todavía" de "no
    pudimos preguntar").
    """
    ts = int(time.time() * 1000)
    params = f"startTime={start_time_ms}&limit={limit}&timestamp={ts}"
    sig = _sign(api_secret, params)
    qs = f"{params}&signature={sig}"
    headers = {"X-MBX-APIKEY": api_key}
    # Siempre por el proxy cuando está configurado
    proxies = {"https": proxy, "http": proxy} if proxy else None

    last_error = ""
    for base in _BINANCE_ENDPOINTS:
        try:
            resp = requests.get(
                f"{base}{_BINANCE_PAY_PATH}?{qs}",
                headers=headers,
                proxies=proxies,
                timeout=timeout,
            )
            if resp.ok:
                data = resp.json()
                return data.get("data") or data.get("rows") or []
            # Un 401/418/429 se repite en todos los endpoints: vale la pena
            # dejar rastro en el log en vez de fallar en silencio como antes.
            last_error = f"HTTP {resp.status_code} {resp.text[:120]}"
        except Exception as exc:
            last_error = str(exc)
            continue
    if last_error:
        print(f"[BinanceAuto] No se pudo consultar el historial de Pay: {last_error}")
    return None  # todos los endpoints fallaron


# ── Lectura de campos de una transacción ─────────────────────────────────────

def _tx_note(tx) -> str:
    return str(
        tx.get("orderMemo")
        or tx.get("remark")
        or tx.get("note")
        or ""
    ).upper().strip()


def _tx_currency(tx) -> str:
    funds = tx.get("fundsDetail") or []
    if isinstance(funds, list) and funds:
        currency = str(funds[0].get("currency") or "").upper()
        if currency:
            return currency
    return str(tx.get("transactedCurrency") or tx.get("currency") or "").upper()


def _tx_amount(tx) -> float:
    """Monto de la transacción. Positivo entrante, negativo saliente."""
    funds = tx.get("fundsDetail") or []
    if isinstance(funds, list) and funds:
        try:
            amount = float(funds[0].get("amount") or 0)
            if amount:
                return amount
        except Exception:
            pass
    try:
        return float(tx.get("transactedAmount") or tx.get("amount") or 0)
    except Exception:
        return 0.0


def _tx_id(tx) -> str:
    return str(
        tx.get("transactionId")
        or tx.get("orderId")
        or tx.get("transactionTime")
        or ""
    ).strip()


def _tx_time_ms(tx) -> int:
    try:
        return int(tx.get("transactionTime") or 0)
    except Exception:
        return 0


def _is_incoming_usdt(tx) -> bool:
    """Solo cobros entrantes en USDT.

    Antes no se filtraba el signo, así que un retiro propio (monto negativo)
    con un memo que contuviera el código podía darse por válido.
    """
    return _tx_amount(tx) > 0 and _tx_currency(tx) in ("", "USDT")


# ── Emparejamiento ────────────────────────────────────────────────────────────

def find_matching_transaction(txs, order_reference: str, expected_usdt: float,
                              tolerance: float = 0.02, allow_amount_match: bool = True,
                              claimed_tx_ids=None, ambiguous_amount: bool = False):
    """Busca la transacción que corresponde a esta orden.

    Devuelve `(tx, motivo)` o `(None, motivo_del_rechazo)`.

    `claimed_tx_ids` son transacciones ya acreditadas a otras órdenes.
    `ambiguous_amount` indica que otra orden pendiente espera el mismo monto,
    en cuyo caso el respaldo por monto se desactiva para no acreditarle el
    pago a la orden equivocada.
    """
    claimed = set(claimed_tx_ids or ())
    ref_upper = str(order_reference).upper().strip()
    candidates = [
        tx for tx in txs
        if _is_incoming_usdt(tx) and _tx_id(tx) not in claimed
    ]

    # 1) Por código en el memo — vía normal, sin ambigüedad posible.
    for tx in candidates:
        if ref_upper and ref_upper in _tx_note(tx):
            if _tx_amount(tx) + tolerance < expected_usdt:
                # Se pagó de menos: no se acredita solo, pero se distingue
                # del caso "no encontrado" para que el admin lo vea.
                return None, (
                    f'underpaid:{_tx_amount(tx):.2f}'
                )
            return tx, 'code'

    if not allow_amount_match or ambiguous_amount:
        return None, 'not_found'

    # 2) Respaldo por monto: solo si hay exactamente UNA transacción sin
    #    memo cuyo monto cuadra. Con dos o más no hay forma de saber cuál
    #    es de este cliente, así que se deja para revisión manual.
    amount_matches = [
        tx for tx in candidates
        if not _tx_note(tx) and _tx_amount(tx) + tolerance >= expected_usdt
        and _tx_amount(tx) - expected_usdt < max(expected_usdt * 0.5, 1.0)
    ]
    if len(amount_matches) == 1:
        return amount_matches[0], 'amount'
    if len(amount_matches) > 1:
        return None, 'ambiguous'
    return None, 'not_found'


def verify_binance_payment(api_key: str, api_secret: str, proxy: str, timeout: float,
                           order_reference: str, expected_usdt: float, since_ms: int,
                           tolerance: float = 0.02, allow_amount_match: bool = True,
                           claimed_tx_ids=None, ambiguous_amount: bool = False):
    """Comprueba si algún cobro de Binance Pay corresponde a esta orden.

    Devuelve `(resultado, tx, motivo)` donde resultado es:
        True  – pago verificado (`tx` trae la transacción)
        False – todavía no aparece
        None  – la API de Binance no respondió (seguir intentando)
    """
    txs = _fetch_pay_transactions(api_key, api_secret, proxy, timeout, since_ms)
    if txs is None:
        return None, None, 'api_unreachable'
    if not txs:
        return False, None, 'not_found'

    tx, reason = find_matching_transaction(
        txs,
        order_reference=order_reference,
        expected_usdt=expected_usdt,
        tolerance=tolerance,
        allow_amount_match=allow_amount_match,
        claimed_tx_ids=claimed_tx_ids,
        ambiguous_amount=ambiguous_amount,
    )
    if tx is not None:
        return True, tx, reason
    return False, None, reason


# ── Helpers de código ─────────────────────────────────────────────────────────

def is_binance_auto_reference(reference: str) -> bool:
    """True si la referencia es un código auto de Binance: 6 dígitos."""
    r = str(reference or "").strip()
    return len(r) == 6 and r.isdigit()


def generate_binance_auto_code(app) -> str:
    """Genera un código de 6 dígitos que no esté en uso por otra orden.

    Se excluyen también las órdenes recientes ya aprobadas/completadas: el
    checkout rechaza referencias repetidas contra ese mismo conjunto de
    estados, así que reutilizar un código de ayer hacía fallar la confirmación
    del cliente con un mensaje de "referencia ya registrada".
    """
    chars = "0123456789"
    with app.app_context():
        from ..models import Order
        cutoff = datetime.utcnow() - timedelta(days=30)
        for _ in range(50):
            code = "".join(random.choices(chars, k=6))
            clash = (
                Order.query
                .filter(Order.payment_reference == code)
                .filter(
                    (Order.status == 'pending')
                    | (Order.created_at >= cutoff)
                )
                .first()
            )
            if not clash:
                return code
    return f"{random.randint(0, 999999):06d}"


# ── Settings ──────────────────────────────────────────────────────────────────

def is_binance_auto_enabled(app) -> bool:
    """True solo cuando binance_auto_enabled == '1' en los settings."""
    with app.app_context():
        from ..models import Setting
        s = Setting.query.filter_by(key="binance_auto_enabled").first()
        return bool(s and s.value == "1")


def is_binance_auto_order(order) -> bool:
    """True si la orden se paga con el flujo automático de Binance Pay."""
    return (
        order is not None
        and (order.payment_method or '').strip().lower() == 'binance'
        and is_binance_auto_reference(order.payment_reference or '')
    )


# ── Verificación de una orden ────────────────────────────────────────────────

def _binance_settings(app):
    api_key = app.config.get("BINANCE_API_KEY", "").strip()
    api_secret = app.config.get("BINANCE_API_SECRET", "").strip()
    return {
        'api_key': api_key,
        'api_secret': api_secret,
        'proxy': app.config.get("BINANCE_PROXY", "").strip(),
        'timeout': float(app.config.get("BINANCE_REQUEST_TIMEOUT", 15)),
        'lookback': int(app.config.get("BINANCE_LOOKBACK_MINUTES", 90)),
        'window': int(app.config.get("BINANCE_VERIFY_WINDOW_MINUTES", 180)),
        'tolerance': float(app.config.get("BINANCE_AMOUNT_TOLERANCE", 0.02)),
        'match_by_amount': bool(app.config.get("BINANCE_MATCH_BY_AMOUNT", True)),
    }


def binance_verify_window_seconds(app) -> int:
    return int(_binance_settings(app)['window']) * 60


def _other_pending_expects_same_amount(order, expected_usdt, tolerance):
    """¿Hay otra orden pendiente de Binance esperando este mismo monto?

    Si la hay, el respaldo por monto no puede decidir a cuál de las dos
    pertenece un pago sin memo, así que se desactiva para esta ronda.
    """
    from ..models import Order

    others = (
        Order.query
        .filter(Order.status == 'pending')
        .filter(Order.payment_method == 'binance')
        .filter(Order.id != order.id)
        .all()
    )
    for other in others:
        if not is_binance_auto_reference(other.payment_reference or ''):
            continue
        if abs(float(other.amount or 0) - expected_usdt) <= max(tolerance, 0.01):
            return True
    return False


def _claimed_tx_ids():
    from ..models import Order

    rows = (
        Order.query
        .with_entities(Order.binance_tx_id)
        .filter(Order.binance_tx_id.isnot(None))
        .all()
    )
    return {row[0] for row in rows if row[0]}


def try_verify_binance_order(order, app):
    """Intenta verificar y aprobar UNA orden de Binance Pay.

    Es el único punto donde se acredita un pago de Binance: lo usan tanto el
    hilo dedicado por orden como el scheduler en segundo plano, y está
    protegido con un lock por orden en la BD para que ambos (o dos workers
    distintos) no puedan aprobar la misma orden a la vez.

    Devuelve un dict con `done` (True cuando ya no hay nada más que
    intentar), `verified` y `message`.
    """
    from ..models import Order, db
    from .locks import acquire_lock, release_lock
    from .minigames import ensure_minigame_opportunity
    from .order_processing import approve_order, order_qualifies_for_auto_fulfillment

    log = f"[BinanceAuto #{order.order_number}]"
    cfg = _binance_settings(app)

    if not cfg['api_key'] or not cfg['api_secret']:
        print(f"{log} Faltan credenciales de la API de Binance.")
        return {'done': True, 'verified': False, 'message': 'Credenciales de Binance no configuradas.'}

    if (order.status or '').lower() != 'pending':
        return {'done': True, 'verified': False, 'message': f"La orden ya está en '{order.status}'."}

    if order.payment_verified_at:
        # Ya se verificó el pago antes (producto de entrega manual que se
        # dejó 'pending' a propósito para revisión). No hay que volver a
        # consultar la API de Binance en cada tick del scheduler.
        return {'done': True, 'verified': True, 'message': 'El pago ya estaba verificado; queda pendiente de revisión manual.'}

    if not is_binance_auto_reference(order.payment_reference):
        return {'done': True, 'verified': False, 'message': 'La referencia no tiene el formato de código Binance.'}

    expected_usdt = float(order.amount or 0.0)
    if expected_usdt <= 0:
        return {'done': True, 'verified': False, 'message': 'El monto de la orden es 0.'}

    created_at = order.created_at or datetime.utcnow()
    age = (datetime.utcnow() - created_at).total_seconds()
    if age > cfg['window'] * 60:
        return {'done': True, 'verified': False, 'message': 'Se agotó la ventana de verificación automática.'}

    lock_key = f'binance_verify:{order.id}'
    lock_holder = uuid4().hex
    if not acquire_lock(lock_key, _VERIFY_LOCK_TTL_SECONDS, lock_holder):
        return {'done': False, 'verified': False, 'message': 'Otra verificación de esta orden está en curso.'}

    try:
        db.session.refresh(order)
        if (order.status or '').lower() != 'pending':
            return {'done': True, 'verified': False, 'message': f"La orden ya está en '{order.status}'."}

        since_ms = _to_epoch_ms(created_at - timedelta(minutes=cfg['lookback']))
        ambiguous = cfg['match_by_amount'] and _other_pending_expects_same_amount(
            order, expected_usdt, cfg['tolerance']
        )

        result, tx, reason = verify_binance_payment(
            cfg['api_key'], cfg['api_secret'], cfg['proxy'], cfg['timeout'],
            order_reference=str(order.payment_reference).upper(),
            expected_usdt=expected_usdt,
            since_ms=since_ms,
            tolerance=cfg['tolerance'],
            allow_amount_match=cfg['match_by_amount'],
            claimed_tx_ids=_claimed_tx_ids(),
            ambiguous_amount=ambiguous,
        )

        if result is None:
            return {'done': False, 'verified': False, 'message': 'La API de Binance no respondió.'}

        if result is not True:
            # Los casos que necesitan un humano se anotan en la orden una
            # sola vez, para que el admin no tenga que adivinar por qué un
            # pago que sí existe no se acreditó.
            _note_manual_review(order, reason, expected_usdt)
            print(f"{log} Sin coincidencia todavía (motivo={reason}).")
            return {'done': False, 'verified': False, 'message': f'Pago no encontrado ({reason}).'}

        tx_id = _tx_id(tx)
        paid = _tx_amount(tx)
        order.binance_tx_id = tx_id or None
        note = (
            f"[Binance] Pago verificado automáticamente ({'código en memo' if reason == 'code' else 'monto exacto sin memo'}). "
            f"TX: {tx_id or 'N/A'} — {paid} USDT."
        )
        if note not in (order.notes or ''):
            order.notes = ((order.notes or '') + '\n' + note).strip()
        order.payment_verified_at = datetime.utcnow()

        # Si el producto no tiene forma de entregarse solo (sin stock de
        # PINs ni mapeo de Revendedores, p.ej. Zinli o un juego al que le
        # quitaron el mapeo), NO se aprueba en automático: el pago llegó,
        # pero la recarga la tiene que hacer un admin a mano. Antes se
        # llamaba a approve_order() sin distinción y la orden quedaba
        # 'approved' de una sola vez, sin que nadie se enterara de que
        # faltaba procesarla manualmente.
        auto_fulfillable = order_qualifies_for_auto_fulfillment(order)
        if not auto_fulfillable:
            manual_note = (
                '[Binance] Pago verificado automáticamente, pero este producto requiere '
                'recarga manual. La orden queda pendiente para que un admin la procese.'
            )
            if manual_note not in (order.notes or ''):
                order.notes = ((order.notes or '') + '\n' + manual_note).strip()
            # El pago ya está confirmado: el cliente no debería perder su
            # giro gratis solo porque la entrega no es automática.
            ensure_minigame_opportunity(order)

        try:
            db.session.commit()
        except Exception as exc:
            # El índice único de binance_tx_id evita que dos órdenes se
            # queden con la misma transacción: si esta perdió la carrera,
            # simplemente no era suya.
            db.session.rollback()
            print(f"{log} No se pudo registrar la transacción {tx_id}: {exc}")
            return {'done': False, 'verified': False, 'message': 'La transacción ya fue acreditada a otra orden.'}

        if not auto_fulfillable:
            print(f"{log} Pago verificado ({reason}) — requiere recarga manual, queda pendiente.")
            return {
                'done': True,
                'verified': True,
                'manual_review_required': True,
                'message': 'Pago verificado. La orden requiere recarga manual y queda pendiente de revisión.',
            }

        print(f"{log} Pago verificado ({reason}) — aprobando.")
        approval = approve_order(order)
        print(f"{log} Resultado de aprobación: {approval.get('message', '')}")
        return {
            'done': True,
            'verified': True,
            'message': approval.get('message', ''),
            'approval': approval,
        }
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        print(f"{log} Error verificando: {exc}")
        return {'done': False, 'verified': False, 'message': str(exc)}
    finally:
        release_lock(lock_key, lock_holder)


def _note_manual_review(order, reason, expected_usdt):
    """Deja constancia en la orden de los casos que un humano debe mirar."""
    from ..models import db

    if reason == 'ambiguous':
        note = (
            '[Binance] Llegaron varios pagos sin memo con este monto y no se pudo '
            'determinar cuál corresponde a esta orden. Requiere revisión manual.'
        )
    elif str(reason).startswith('underpaid:'):
        paid = str(reason).split(':', 1)[1]
        note = (
            f'[Binance] Se encontró un pago con el código correcto pero por {paid} USDT '
            f'en vez de {expected_usdt:.2f} USDT. Requiere revisión manual.'
        )
    else:
        return

    if note in (order.notes or ''):
        return
    order.notes = ((order.notes or '') + '\n' + note).strip()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


# ── Hilo dedicado por orden ──────────────────────────────────────────────────

def _verify_order_thread(order_id: int, order_number: str, app):
    """Hilo daemon para UNA orden, que consulta Binance cada 30 s.

    Da la respuesta rápida mientras el cliente mira la pantalla de estado.
    Si el worker muere o se reinicia la app, el hilo se pierde — de eso se
    encarga el scheduler en segundo plano, que reintenta las mismas órdenes
    llamando a `try_verify_binance_order`.
    """
    log = f"[BinanceAuto #{order_number}]"
    time.sleep(_ORDER_POLL_INITIAL_DELAY)

    while True:
        try:
            if not is_binance_auto_enabled(app):
                print(f"{log} Función desactivada — se detiene el hilo.")
                return

            with app.app_context():
                from ..models import Order

                order = Order.query.get(order_id)
                if order is None:
                    print(f"{log} La orden ya no existe — se detiene el hilo.")
                    return

                outcome = try_verify_binance_order(order, app)
                if outcome.get('done'):
                    return
        except Exception as exc:
            print(f"{log} Error en el hilo: {exc}")

        time.sleep(_ORDER_POLL_INTERVAL)


def start_order_verification(order, app):
    """Lanza el hilo de verificación dedicado para una orden de Binance Pay.

    Se llama justo después de guardar la orden. Nunca se arranca ningún hilo
    al iniciar la app, solo cuando existe una orden real.
    """
    t = threading.Thread(
        target=_verify_order_thread,
        args=(order.id, order.order_number, app),
        daemon=True,
        name=f"binance-{order.order_number}",
    )
    t.start()
    print(f"[BinanceAuto] Hilo de verificación iniciado para la orden #{order.order_number}.")
