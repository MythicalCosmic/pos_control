"""Settings for the POS control center.

This is a separate Django project (NOT alpha_pos). It runs on a vendor-
operated VPS and is the central authority for all alpha_pos installs in
the field: issues license keys, records heartbeats, holds the
suspend/resume + banner-message admin actions.

Plan: /home/cosmic/.claude/plans/kind-gliding-kay.md
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.environ.get('DEBUG', 'False').lower() in ('true', '1', 'yes')

# Mirror the alpha_pos pattern: dev-only fallback SECRET_KEY, hard-fail in
# production. The control center holds every customer's license key hash;
# losing the SECRET_KEY would not directly leak keys (they're sha256'd),
# but it's still the cookie-signing key for the Django admin.
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-dev-only-control-center-key'
    else:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured(
            'SECRET_KEY environment variable must be set when DEBUG is False.'
        )

if DEBUG:
    ALLOWED_HOSTS = ['*']
else:
    ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'tenants',
    'licenses',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'pos_control_center.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'pos_control_center.wsgi.application'


# SQLite locally; Postgres in production via DB_ENGINE=django.db.backends.postgresql.
if os.environ.get('DB_ENGINE'):
    DATABASES = {
        'default': {
            'ENGINE': os.environ['DB_ENGINE'],
            'NAME': os.environ.get('DB_NAME', 'pos_control_center'),
            'USER': os.environ.get('DB_USER', 'pos_control_center'),
            'PASSWORD': os.environ.get('DB_PASSWORD', ''),
            'HOST': os.environ.get('DB_HOST', 'db'),
            'PORT': os.environ.get('DB_PORT', '5432'),
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Production hardening — matches the patterns used by alpha_pos.
if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
    X_FRAME_OPTIONS = 'DENY'

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True

# Ed25519 private key (hex) used to sign perpetual-unlock files for
# customers if the vendor ever shuts down. NEVER commit this. In
# production this should live in a hardware vault, NOT on the control-
# center server — the dashboard's "Generate perpetual unlock" action
# accepts the signed file pasted in, it does not sign on-the-fly.
LICENSE_VENDOR_PRIVATE_KEY = os.environ.get('LICENSE_VENDOR_PRIVATE_KEY', '')

# Public counterpart — embedded in every alpha_pos build so installs can
# verify unlock signatures. Surfaced here only for the dashboard to
# display alongside generated unlock files.
LICENSE_VENDOR_PUBLIC_KEY = os.environ.get('LICENSE_VENDOR_PUBLIC_KEY', '')
