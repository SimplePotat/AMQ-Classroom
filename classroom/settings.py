import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CLASSROOM_DATA_DIR", BASE_DIR / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def persistent_secret(name):
    import secrets
    path = DATA_DIR / name
    try:
        with path.open("x") as handle:
            handle.write(secrets.token_urlsafe(32))
        path.chmod(0o600)
    except FileExistsError:
        pass
    return path.read_text().strip()


SECRET_KEY = os.environ.get("CLASSROOM_SECRET_KEY") or persistent_secret(".django-key")
HOST_KEY = os.environ.get("CLASSROOM_HOST_KEY") or persistent_secret(".host-key")
DEBUG = False
# IP/port LAN access is intentional. CSRF and host credentials protect mutations.
ALLOWED_HOSTS = os.environ.get("CLASSROOM_ALLOWED_HOSTS", "*").split(",")
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.sessions", "classroom"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "classroom.urls"
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3", "NAME": DATA_DIR / "classroom.sqlite3",
    "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 90
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
CSRF_COOKIE_SAMESITE = "Strict"
SESSION_COOKIE_SECURE = False  # Plain HTTP on a trusted LAN is the supported use case.
USE_TZ = True
TIME_ZONE = "UTC"
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}]
SOURCE_URL = os.environ.get("CLASSROOM_SOURCE_URL", "https://github.com/SimplePotat/anime-quiz-classroom")
