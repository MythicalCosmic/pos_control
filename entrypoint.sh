#!/bin/sh
set -eu

# The control center can run on SQLite (no DB_ENGINE) or Postgres. Only wait
# for a TCP DB when one is actually configured via DB_HOST.
if [ -n "${DB_ENGINE:-}" ] && [ -n "${DB_HOST:-}" ]; then
    DB_PORT="${DB_PORT:-5432}"
    echo "Waiting for ${DB_HOST}:${DB_PORT}..."
    i=0
    until python -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('${DB_HOST}', ${DB_PORT})) == 0 else 1)" 2>/dev/null; do
        i=$((i + 1))
        if [ "${i}" -ge 60 ]; then
            echo "DB not reachable after ${i}s, exiting." >&2
            exit 1
        fi
        sleep 1
    done
    echo "DB reachable."
fi

echo "Running migrations..."
python manage.py migrate --noinput

echo "Starting gunicorn..."
exec gunicorn pos_control_center.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-3}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --access-logfile - \
    --error-logfile -
