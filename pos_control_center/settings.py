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
    'billing',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # Serves admin static files in production without a separate web server.
    # Must sit directly after SecurityMiddleware per WhiteNoise docs.
    'whitenoise.middleware.WhiteNoiseMiddleware',
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
# CONN_MAX_AGE keeps the connection warm between requests instead of
# reconnecting on every heartbeat — drops a noticeable chunk of p95 latency.
if os.environ.get('DB_ENGINE'):
    DATABASES = {
        'default': {
            'ENGINE': os.environ['DB_ENGINE'],
            'NAME': os.environ.get('DB_NAME', 'pos_control_center'),
            'USER': os.environ.get('DB_USER', 'pos_control_center'),
            'PASSWORD': os.environ.get('DB_PASSWORD', ''),
            'HOST': os.environ.get('DB_HOST', 'db'),
            'PORT': os.environ.get('DB_PORT', '5432'),
            'CONN_MAX_AGE': int(os.environ.get('DB_CONN_MAX_AGE', '60')),
            'CONN_HEALTH_CHECKS': True,
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

# WhiteNoise: hashed + gzip/brotli-compressed static files so the admin
# dashboard loads its CSS/JS straight from gunicorn.
STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

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
    # Only redirect to HTTPS when explicitly opted in, so a reverse-proxy
    # deployment (the typical one) doesn't end up in a redirect loop.
    SECURE_SSL_REDIRECT = os.environ.get(
        'SECURE_SSL_REDIRECT', 'False'
    ).lower() in ('true', '1', 'yes')
    # Trust X-Forwarded-Proto from a known-good reverse proxy terminating TLS.
    # Configure only when actually behind such a proxy.
    if os.environ.get('TRUST_FORWARDED_PROTO', '').lower() in ('true', '1', 'yes'):
        SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True

# Admin login posts a CSRF-protected form; Django 4+ checks Origin against
# this list. Set to the dashboard's public origin(s) when serving over HTTPS
# behind a proxy, e.g. CSRF_TRUSTED_ORIGINS=https://control.example.com
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]

# Logging: console handler so register/heartbeat logger.exception() output
# lands in `docker logs` (and journald / systemd) rather than being swallowed.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {
            'format': '{asctime} {levelname} {name}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'standard',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': os.environ.get('LOG_LEVEL', 'INFO'),
    },
}

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

# --- Payment providers (top-ups credit the tenant's prepaid balance) ---
# All credits flow in through the payment provider webhooks — the control
# center has no manual top-up form. Click.uz merchant credentials:
CLICK_SERVICE_ID = os.environ.get('CLICK_SERVICE_ID', '')
CLICK_MERCHANT_ID = os.environ.get('CLICK_MERCHANT_ID', '')
CLICK_SECRET_KEY = os.environ.get('CLICK_SECRET_KEY', '')
# Payme.uz (Paycom) merchant key:
PAYME_MERCHANT_KEY = os.environ.get('PAYME_MERCHANT_KEY', '')


# --- Email (warning + lockout notifications) ----------------------------------
# Sent by `python manage.py bill_subscriptions` (run daily from cron). In
# development the console backend prints emails to stdout; in production set
# EMAIL_HOST / EMAIL_HOST_USER / EMAIL_HOST_PASSWORD via env and the SMTP
# backend takes over automatically.
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
if EMAIL_HOST:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
    EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
    EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
    EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True').lower() in (
        'true', '1', 'yes',
    )
    EMAIL_USE_SSL = os.environ.get('EMAIL_USE_SSL', 'False').lower() in (
        'true', '1', 'yes',
    )
    EMAIL_TIMEOUT = int(os.environ.get('EMAIL_TIMEOUT', '15'))
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL', 'POS Control Center <noreply@control.local>',
)
