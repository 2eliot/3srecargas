import os
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
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
    VPS_TIMEOUT = int(os.environ.get('VPS_TIMEOUT', 120))
    VPS_COUNTRY = os.environ.get('VPS_COUNTRY', 'Venezuela')
    VPS_FULL_NAME = os.environ.get('VPS_FULL_NAME', 'Usuario Recarga')
    VPS_BIRTH_DATE = os.environ.get('VPS_BIRTH_DATE', '01/01/1995')
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'app', 'static', 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB
    ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    REVENDEDORES_BASE_URL = os.environ.get('REVENDEDORES_BASE_URL', '')
    REVENDEDORES_API_KEY = os.environ.get('REVENDEDORES_API_KEY', '')
    # Binance Pay auto-verification
    BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY', '').strip()
    BINANCE_API_SECRET = os.environ.get('BINANCE_API_SECRET', '').strip()
    BINANCE_PROXY = os.environ.get('BINANCE_PROXY', '').strip()
    BINANCE_REQUEST_TIMEOUT = float(os.environ.get('BINANCE_REQUEST_TIMEOUT_SECONDS', '4'))
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
