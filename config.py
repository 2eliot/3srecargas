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
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'app', 'static', 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB
    ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
