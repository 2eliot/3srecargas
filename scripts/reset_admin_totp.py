"""Desactiva Google Authenticator para un admin (uso de emergencia).

Ejecutar en el servidor, dentro de la carpeta de la app:

    venv/bin/python scripts/reset_admin_totp.py --username 3srecargas

Después de ejecutarlo, ese admin puede entrar con su clave y volver a
configurar el autenticador. No imprime secretos ni contraseñas.
"""
import argparse
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import create_app, db
from app.models import AdminUser

# Este script es de mantenimiento: no debe arrancar el scheduler de órdenes
# (se quedaría con el liderazgo y frenaría la recuperación de los workers).
os.environ['DISABLE_SCHEDULER'] = '1'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--username', default='', help='Usuario del admin (si hay uno solo, se puede omitir)')
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        query = AdminUser.query
        if args.username:
            query = query.filter_by(username=args.username)
        admin = query.order_by(AdminUser.id.asc()).first()
        if not admin:
            print('No encontré ese admin.')
            return 1
        admin.totp_enabled = False
        admin.totp_secret = None
        admin.totp_recovery_hashes = None
        admin.totp_last_counter = 0
        admin.totp_confirmed_at = None
        db.session.commit()
        print(f'Google Authenticator desactivado para {admin.username}. Vuelve a configurarlo al entrar.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
