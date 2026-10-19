"""Recuperación automática de órdenes en segundo plano.

Antes de esto, la única forma de que una orden avanzara (verificación de
pago en Pabilo, o recarga en Revendedores atascada en "procesando"/error)
era que alguien tuviera abierta la página de estado del pedido o el panel
de admin, ya que ambas dependen de JavaScript haciendo polling desde el
navegador. Si el cliente cerraba la pestaña o ningún admin estaba mirando
esa orden en ese momento, la orden simplemente se quedaba parada — sin
ningún proceso en el servidor que la retomara.

Este módulo agrega un hilo en segundo plano, uno por proceso/worker, que
cada _TICK_INTERVAL_SECONDS revisa las órdenes 'pending' que necesitan
seguimiento y las reintenta usando las mismas funciones que ya usan las
rutas manuales (auto_verify_and_process_order, process_order_delivery).
Como la app corre con varios workers de Gunicorn, cada worker tiene su
propio hilo; el lock de `order_recovery_scheduler` (ver utils/locks.py)
asegura que en cada tick solo uno de ellos haga el trabajo, para no
disparar la misma verificación varias veces en paralelo.
"""
import json
import threading
import time
from datetime import datetime

from .locks import acquire_lock, release_lock

_TICK_INTERVAL_SECONDS = 20
_INITIAL_DELAY_SECONDS = 15
_LEADER_LOCK_KEY = 'order_recovery_scheduler'
_MAX_ORDERS_PER_TICK = 25
# Peor caso de un tick: _MAX_ORDERS_PER_TICK órdenes, cada una con una
# llamada externa de hasta 120s, más margen.
_LEADER_LOCK_TTL_SECONDS = 15 * 60

_scheduler_started = False
_start_guard = threading.Lock()


def start_order_recovery_scheduler(app):
    """Arranca el hilo de recuperación una sola vez por proceso."""
    global _scheduler_started
    with _start_guard:
        if _scheduler_started:
            return
        _scheduler_started = True

    thread = threading.Thread(
        target=_loop,
        args=(app,),
        daemon=True,
        name='order-recovery-scheduler',
    )
    thread.start()


def _loop(app):
    time.sleep(_INITIAL_DELAY_SECONDS)
    while True:
        # Todo el ciclo (lock + tick) corre dentro de un único app_context:
        # acquire_lock/release_lock usan db.session igual que cualquier otra
        # consulta, así que necesitan un contexto de aplicación activo tanto
        # como _run_tick. Sin esto, acquire_lock revienta con "Working
        # outside of application context" en cada ciclo y el scheduler
        # nunca llega a procesar nada.
        with app.app_context():
            holder = f'{threading.get_ident()}:{id(app)}'
            try:
                # El TTL tiene que cubrir un tick COMPLETO en el peor caso, no
                # solo el intervalo: un tick puede tocar varias órdenes y cada
                # llamada a Pabilo/Revendedores tarda hasta 120s. Con el TTL
                # corto que había antes, el lock caducaba a mitad del tick,
                # otro worker se declaraba líder y ambos procesaban las mismas
                # órdenes en paralelo. Si el worker muere de verdad, el lock
                # igual se libera solo al vencer.
                if acquire_lock(_LEADER_LOCK_KEY, _LEADER_LOCK_TTL_SECONDS, holder):
                    try:
                        _run_tick(app)
                        _run_promos_tick()
                    finally:
                        release_lock(_LEADER_LOCK_KEY, holder)
            except Exception as exc:
                print(f'[OrderScheduler] Error en tick: {exc}')
        time.sleep(_TICK_INTERVAL_SECONDS)


def _run_tick(app):
    from ..models import Order, db
    from .binance_pay import (
        binance_verify_window_seconds,
        is_binance_auto_enabled,
        is_binance_auto_order,
        try_verify_binance_order,
    )
    from .payment_verification import is_auto_verify_enabled

    auto_verify_on = is_auto_verify_enabled()
    binance_on = is_binance_auto_enabled(app)

    # Imports diferidos: evitan depender del orden de carga de módulos
    # al arrancar la app (routes.checkout ya importa de utils en su
    # nivel superior).
    from ..routes.checkout import (
        auto_verify_and_process_order,
        order_supports_background_payment_verification,
    )
    from .order_processing import process_order_delivery

    # Se recorren las 500 pendientes más viejas, pero solo las que de verdad hicieron trabajo gastan un turno (más abajo):
    # antes, unas 25 órdenes viejas atascadas (intentos agotados, esperando el resto del pago o ya verificadas en espera
    # de revisión manual) se llevaban todos los turnos y las órdenes nuevas nunca llegaban a revisarse.
    pending_orders = (
        Order.query
        .filter(Order.status == 'pending')
        .order_by(Order.id.asc())
        .limit(500)
        .all()
    )
    binance_window = binance_verify_window_seconds(app) if binance_on else 0

    processed = 0
    for order in pending_orders:
        if processed >= _MAX_ORDERS_PER_TICK:
            break

        # Pausa de emergencia (botón del admin): el sistema no toca esta orden hasta que se reanude, y no gasta turno.
        if order.is_paused:
            continue

        try:
            auto_resp = json.loads(order.automation_response or '{}')
        except Exception:
            auto_resp = {}

        try:
            if auto_resp.get('pending_verification'):
                # Reintenta el siguiente paso de un mapeo con varios items
                # (cantidad ≥ 2), una recarga en espera de saldo o una que
                # el proveedor aún está procesando: es continuar una entrega YA
                # aprobada, no verificar un pago nuevo, así que no depende
                # de "auto_verify_payments" ni de Binance — si dependiera,
                # con ese interruptor apagado (como en este sitio) el paso
                # 2 en adelante se quedaba en "procesando" para siempre.
                process_order_delivery(order, base_state=auto_resp, force=False)
                processed += 1
            elif binance_on and is_binance_auto_order(order):
                # El hilo dedicado que se lanza al crear la orden vive dentro
                # de un worker concreto: si ese worker se recicla o se
                # despliega una versión nueva, el hilo desaparece y antes
                # nadie retomaba la orden — el cliente había pagado con el
                # código correcto y la orden se quedaba pendiente para
                # siempre. Aquí se reintenta desde el scheduler, que sí
                # sobrevive a los reinicios.
                age = (datetime.utcnow() - (order.created_at or datetime.utcnow())).total_seconds()
                if age <= binance_window:
                    binance_result = try_verify_binance_order(order, app) or {}
                    # Solo gasta turno si hubo una consulta real a Binance (la orden sigue sin verificar): una ya verificada, sin
                    # credenciales o con la ventana agotada contesta al instante y no debe quitarles el turno a las demás.
                    if not binance_result.get('done'):
                        processed += 1
            elif auto_verify_on and order_supports_background_payment_verification(order):
                result = auto_verify_and_process_order(order) or {}
                # Solo cuenta si hubo un intento real; las que salen de inmediato (intentos agotados, en espera del resto
                # del pago, en cooldown o ya verificadas) no le quitan el turno a las demás.
                if result.get('checked') and not result.get('noop'):
                    processed += 1
        except Exception as exc:
            order_number = getattr(order, 'order_number', order.id)
            print(f'[OrderScheduler] Error procesando orden #{order_number}: {exc}')
            try:
                db.session.rollback()
            except Exception:
                pass


def _run_promos_tick():
    """Corre el sorteo diario de cada juego que ya llegó a su hora de
    sorteo. Aparte del tick normal de órdenes porque no depende de que
    haya órdenes pendientes: puede no correr nunca si nadie más dispara
    esta revisión."""
    from . import hordas
    from .promos import cleanup_horde_replays, run_daily_raffle_draws, run_weekly_horde_awards
    from ..models import db

    try:
        run_daily_raffle_draws()
    except Exception as exc:
        print(f'[OrderScheduler] Error en sorteo diario: {exc}')
        try:
            db.session.rollback()
        except Exception:
            pass

    # Hordas por cuenta: ganadores de la semana que acaba de cerrar (quedan pendientes hasta que el admin apruebe).
    try:
        hordas.run_weekly_awards()
    except Exception as exc:
        print(f'[OrderScheduler] Error en premio semanal de Hordas: {exc}')
        try:
            db.session.rollback()
        except Exception:
            pass

    # Solo para cerrar la última semana jugada por ID (12-18/10/2026): ver utils/promos.py.
    try:
        run_weekly_horde_awards()
    except Exception as exc:
        print(f'[OrderScheduler] Error en premio semanal de Hordas (por ID): {exc}')
        try:
            db.session.rollback()
        except Exception:
            pass

    # Semana cerrada: solo quedan las repeticiones del top 5.
    for cleanup in (hordas.cleanup_replays, cleanup_horde_replays):
        try:
            cleanup()
        except Exception as exc:
            print(f'[OrderScheduler] Error limpiando repeticiones de Hordas: {exc}')
            try:
                db.session.rollback()
            except Exception:
                pass

    _run_background_housekeeping()


# Trabajo que ANTES se hacía dentro de la petición de un visitante (y lo dejaba esperando segundos o minutos): cierre del mes del
# ranking con sus premios, recálculo de todos los perfiles y chats inactivos. Ahora lo hace este hilo, que ya corre cada 20 segundos.
_HOUSEKEEPING_EVERY_SECONDS = 60
_PURGE_EVERY_SECONDS = 10 * 60
_housekeeping_last = {'steps': 0.0, 'purge': 0.0}


def _run_background_housekeeping():
    from ..models import db

    def guarded(label, func):
        try:
            func()
        except Exception as exc:
            print(f'[OrderScheduler] Error en {label}: {exc}')
            try:
                db.session.rollback()
            except Exception:
                pass

    now = time.monotonic()
    if now - _housekeeping_last['steps'] >= _HOUSEKEEPING_EVERY_SECONDS:
        _housekeeping_last['steps'] = now

        def archive_month():
            from ..routes.main import archive_previous_month_rankings_if_needed
            archive_previous_month_rankings_if_needed()

        def refresh_profiles():
            from .profile_social import ensure_fresh
            ensure_fresh()

        def close_chats():
            from .support import close_idle_chats
            close_idle_chats()

        guarded('cierre mensual del ranking', archive_month)
        guarded('recálculo de perfiles', refresh_profiles)
        guarded('cierre de chats inactivos', close_chats)

    if now - _housekeeping_last['purge'] >= _PURGE_EVERY_SECONDS:
        _housekeeping_last['purge'] = now

        def purge():
            from .locks import purge_stale_counters
            purge_stale_counters()

        def optimize_images():
            from .image_optimize import optimize_branding_images
            optimize_branding_images()

        guarded('limpieza de topes viejos', purge)
        guarded('optimización de imágenes de fondo', optimize_images)
