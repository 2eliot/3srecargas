"""Cifra los códigos que ya existen en la base (stock y códigos entregados).

Uso en el servidor, dentro de la carpeta de la app:

    DISABLE_SCHEDULER=1 venv/bin/python scripts/encrypt_codes.py

Es idempotente: los códigos que ya están cifrados no se vuelven a tocar.
No imprime ningún código.
"""
import argparse
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

os.environ['DISABLE_SCHEDULER'] = '1'

from app import create_app, db
from app.models import Order, Pin
from app.utils.code_crypto import encrypt_code, is_encrypted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Solo contar, sin guardar')
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        pins = 0
        orders = 0
        last_id = 0
        while True:
            batch = (
                Pin.query.filter(Pin.id > last_id)
                .order_by(Pin.id.asc())
                .limit(200)
                .all()
            )
            if not batch:
                break
            for pin in batch:
                if pin.code and not is_encrypted(pin.code):
                    pins += 1
                    if not args.dry_run:
                        pin.code = encrypt_code(pin.code)
            if not args.dry_run:
                db.session.commit()
            last_id = batch[-1].id

        last_id = 0
        while True:
            batch = (
                Order.query
                .filter(Order.id > last_id, Order.pin_delivered.isnot(None), Order.pin_delivered != '')
                .order_by(Order.id.asc())
                .limit(200)
                .all()
            )
            if not batch:
                break
            for order in batch:
                if order.pin_delivered and not is_encrypted(order.pin_delivered):
                    orders += 1
                    if not args.dry_run:
                        order.pin_delivered = encrypt_code(order.pin_delivered)
            if not args.dry_run:
                db.session.commit()
            last_id = batch[-1].id

        mode = 'CONTADOS' if args.dry_run else 'CIFRADOS'
        print(f"{mode}: {pins} PINs de stock; {orders} órdenes con código entregado.")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
