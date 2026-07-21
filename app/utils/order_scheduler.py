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
rutas manuales (auto_verify_and_process_order, process_revendedores_queue).
Como la app corre con varios workers de Gunicorn, cada worker tiene su
propio hilo; el lock de `order_recovery_scheduler` (ver utils/locks.py)
asegura que en cada tick solo uno de ellos haga el trabajo, para no
disparar la misma verificación varias veces en paralelo.
"""
import json
import threading
import time

from .locks import acquire_lock, release_lock

_TICK_INTERVAL_SECONDS = 20
_INITIAL_DELAY_SECONDS = 15
_LEADER_LOCK_KEY = 'order_recovery_scheduler'
_MAX_ORDERS_PER_TICK = 25

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
                # TTL algo mayor al intervalo entre ticks: si este worker se
                # cae a mitad de un tick, el lock expira solo y otro worker
                # puede tomar el siguiente tick sin quedar bloqueado.
                if acquire_lock(_LEADER_LOCK_KEY, _TICK_INTERVAL_SECONDS + 30, holder):
                    try:
                        _run_tick(app)
                    finally:
                        release_lock(_LEADER_LOCK_KEY, holder)
            except Exception as exc:
                print(f'[OrderScheduler] Error en tick: {exc}')
        time.sleep(_TICK_INTERVAL_SECONDS)


def _run_tick(app):
    from ..models import Order, db
    from .payment_verification import is_auto_verify_enabled

    if not is_auto_verify_enabled():
        return

    # Imports diferidos: evitan depender del orden de carga de módulos
    # al arrancar la app (routes.checkout ya importa de utils en su
    # nivel superior).
    from ..routes.checkout import (
        auto_verify_and_process_order,
        order_supports_background_payment_verification,
    )
    from .order_processing import process_revendedores_queue

    pending_orders = (
        Order.query
        .filter(Order.status == 'pending')
        .order_by(Order.id.asc())
        .limit(200)
        .all()
    )

    processed = 0
    for order in pending_orders:
        if processed >= _MAX_ORDERS_PER_TICK:
            break

        try:
            auto_resp = json.loads(order.automation_response or '{}')
        except Exception:
            auto_resp = {}

        try:
            if auto_resp.get('pending_verification'):
                process_revendedores_queue(order, base_state=auto_resp, force=False)
                processed += 1
            elif order_supports_background_payment_verification(order):
                auto_verify_and_process_order(order)
                processed += 1
        except Exception as exc:
            order_number = getattr(order, 'order_number', order.id)
            print(f'[OrderScheduler] Error procesando orden #{order_number}: {exc}')
            try:
                db.session.rollback()
            except Exception:
                pass
