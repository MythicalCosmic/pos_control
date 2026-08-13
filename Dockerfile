FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Every pinned dependency publishes a Python 3.13 Linux wheel, including
# psycopg2-binary and cryptography, so a compiler toolchain is unnecessary.
# Keeping it out makes the runtime image smaller and reduces build surface.
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Collect admin static into STATIC_ROOT for WhiteNoise. Needs a SECRET_KEY
# present even with DEBUG, so pass a throwaway one for the build step only.
RUN SECRET_KEY=build-only DEBUG=True python manage.py collectstatic --noinput

# Run as a non-root user.
RUN groupadd --system app && useradd --system --gid app --home /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app

COPY --chown=app:app entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["/entrypoint.sh"]
