#!/bin/bash
set -e

echo "[CP] Aguardando PostgreSQL em $POSTGRES_HOST:$POSTGRES_PORT..."
until pg_isready -h "$POSTGRES_HOST" -p "${POSTGRES_PORT:-5432}" -U "$POSTGRES_USER" -q; do
  sleep 2
done
echo "[CP] PostgreSQL disponível."

python manage.py migrate --noinput
python manage.py loaddata registry/fixtures/initial_data.json

if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
  python manage.py createsuperuser \
    --noinput \
    --username "$DJANGO_SUPERUSER_USERNAME" \
    --email "${DJANGO_SUPERUSER_EMAIL:-admin@${CP_DOMAIN:-localhost}}" \
    2>/dev/null || true
fi

python manage.py collectstatic --noinput --clear

exec gunicorn core.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-2}" \
  --timeout 120 \
  --access-logfile - \
  --error-logfile -
