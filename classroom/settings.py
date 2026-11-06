import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CLASSROOM_DATA_DIR", BASE_DIR / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
if os.name == "posix":
    DATA_DIR.chmod(0o700)
    # Protect existing databases as well as newly created journal/sidecar files.
    os.umask(0o077)
    for existing in DATA_DIR.glob("classroom.sqlite3*"):
        if existing.is_file():
            existing.chmod(0o600)


def persistent_secret(name):
    import secrets
    path = DATA_DIR / name
    try:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            handle.write(secrets.token_urlsafe(32))
        path.chmod(0o600)
    except FileExistsError:
        if os.name == "posix":
            path.chmod(0o600)
    return path.read_text().strip()


SECRET_KEY = os.environ.get("CLASSROOM_SECRET_KEY") or persistent_secret(".django-key")
HOST_KEY = os.environ.get("CLASSROOM_HOST_KEY") or persistent_secret(".host-key")
DEBUG = False
# The LAN boundary middleware limits the default wildcard to literal IPs/localhost.
ALLOWED_HOSTS = [host.strip() for host in os.environ.get("CLASSROOM_ALLOWED_HOSTS", "*").split(",") if host.strip()]
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.sessions", "classroom"]
MIDDLEWARE = [
    "classroom.security.LANBoundaryMiddleware",
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
DATA_UPLOAD_MAX_MEMORY_SIZE = 16 * 1024
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}]
SOURCE_URL = os.environ.get("CLASSROOM_SOURCE_URL", "https://github.com/SimplePotat/anime-quiz-classroom")
