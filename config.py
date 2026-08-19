import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'), override=True)
DEFAULT_DATA_DIR = os.environ.get('DATA_DIR', os.path.join(BASE_DIR, 'data'))


class Config:
    DATA_DIR = DEFAULT_DATA_DIR
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL',
        'sqlite:///' + os.path.join(DATA_DIR, 'app.db')
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    AUTOMATION_SERVICE_URL = os.environ.get('AUTOMATION_SERVICE_URL', 'http://localhost:8000')
    VPS_REDEEM_URL = os.environ.get('VPS_REDEEM_URL', AUTOMATION_SERVICE_URL.rstrip('/') + '/redeem')
    # Endpoint del bot que responde si un PIN ya fue canjeado SIN canjearlo.
    # Es lo que permite resolver un timeout o un `manual_review` sin adivinar.
    VPS_VERIFY_URL = os.environ.get('VPS_VERIFY_URL', AUTOMATION_SERVICE_URL.rstrip('/') + '/redeem/verify')
    VPS_VERIFY_TIMEOUT = int(os.environ.get('VPS_VERIFY_TIMEOUT', 60))
    VPS_TIMEOUT = int(os.environ.get('VPS_TIMEOUT', 120))
    VPS_COUNTRY = os.environ.get('VPS_COUNTRY', 'Venezuela')
    VPS_FULL_NAME = os.environ.get('VPS_FULL_NAME', 'Usuario Recarga')
    VPS_BIRTH_DATE = os.environ.get('VPS_BIRTH_DATE', '01/01/1995')
    # Código extra para entrar a Stock PINs dentro del admin. Sirve para que
    # quien atiende la web pueda usar el resto del panel sin llegar a los
    # códigos. Si se deja vacío, la sección queda abierta como antes.
    STOCK_PINS_ACCESS_CODE = os.environ.get('STOCK_PINS_ACCESS_CODE', '')
    # Minutos que dura el desbloqueo antes de volver a pedir el código.
    STOCK_PINS_UNLOCK_MINUTES = int(os.environ.get('STOCK_PINS_UNLOCK_MINUTES', 30))
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'app', 'static', 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB
    ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'webm', 'mov', 'm4v'}
    REVENDEDORES_BASE_URL = os.environ.get('REVENDEDORES_BASE_URL', '')
    REVENDEDORES_API_KEY = os.environ.get('REVENDEDORES_API_KEY', '')
    GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '').strip()
    GEMINI_REFERENCE_TIMEOUT = int(os.environ.get('GEMINI_REFERENCE_TIMEOUT', 25))
    # Binance Pay auto-verification
    BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '').strip()
    BINANCE_API_SECRET = os.environ.get('BINANCE_API_SECRET', '').strip()
    BINANCE_PROXY = os.environ.get('BINANCE_PROXY', '').strip()
    BINANCE_REQUEST_TIMEOUT = float(os.environ.get('BINANCE_REQUEST_TIMEOUT_SECONDS', '15'))
    # Cuánto tiempo ANTES de que se cree la orden se buscan transacciones. El
    # cliente ve el código en el checkout, paga en la app de Binance y recién
    # entonces vuelve a confirmar, así que el pago casi siempre es anterior a
    # la orden. Con los 2 minutos que se usaban antes, cualquier cliente que
    # tardara un poco en volver quedaba fuera de la ventana de búsqueda.
    BINANCE_LOOKBACK_MINUTES = int(os.environ.get('BINANCE_LOOKBACK_MINUTES', '90'))
    # Cuánto tiempo se sigue buscando el pago de una orden pendiente.
    BINANCE_VERIFY_WINDOW_MINUTES = int(os.environ.get('BINANCE_VERIFY_WINDOW_MINUTES', '180'))
    # Diferencia máxima aceptada por debajo del monto esperado (USDT).
    BINANCE_AMOUNT_TOLERANCE = float(os.environ.get('BINANCE_AMOUNT_TOLERANCE', '0.02'))
    # Permite acreditar un pago cuyo memo llegó vacío emparejando monto +
    # ventana de tiempo, siempre que no haya ambigüedad (ver binance_pay.py).
    BINANCE_MATCH_BY_AMOUNT = os.environ.get('BINANCE_MATCH_BY_AMOUNT', 'true').strip().lower() == 'true'
    PABILO_BASE_URL = os.environ.get('PABILO_BASE_URL', 'https://api.pabilo.app')
    PABILO_TIMEOUT = int(os.environ.get('PABILO_TIMEOUT', 30))
    SCRAPE_ENABLED = os.environ.get('SCRAPE_ENABLED', 'true').strip().lower() == 'true'
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME', '')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD', '')
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'true').strip().lower() == 'true'
    MAIL_USE_SSL = os.environ.get('MAIL_USE_SSL', 'false').strip().lower() == 'true'
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER', MAIL_USERNAME)
    MAIL_BRAND_NAME = os.environ.get('MAIL_BRAND_NAME', '3S Recargas')
    SUPPORT_EMAIL = os.environ.get('SUPPORT_EMAIL', 'soporte@3srecargas.com')
    SUPPORT_WHATSAPP = os.environ.get('SUPPORT_WHATSAPP', 'https://wa.me/584120000000')
    ADMIN_NOTIFY_EMAIL = os.environ.get('ADMIN_NOTIFY_EMAIL', '')
    MINIGAME_DEV_MODE = os.environ.get('MINIGAME_DEV_MODE', '').strip().lower() in {'1', 'true', 'yes', 'on', 'dev'}
