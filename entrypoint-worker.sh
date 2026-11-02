#!/bin/bash
set -e

echo "[CP Worker] Aguardando PostgreSQL em $POSTGRES_HOST:$POSTGRES_PORT..."
until pg_isready -h "$POSTGRES_HOST" -p "${POSTGRES_PORT:-5432}" -U "$POSTGRES_USER" -q; do
  sleep 2
done
echo "[CP Worker] PostgreSQL disponível."

# Trava anti-downgrade: com set -e, versão mais antiga que a registrada não sobe.
python manage.py guarda_versao verificar
python manage.py guarda_versao registrar
python manage.py migrate --noinput

exec "$@"
