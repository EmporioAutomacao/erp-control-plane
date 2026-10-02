#!/bin/bash
set -e

# Worker Celery de nó: só consome a fila deste nó. Não roda migrate (o CP/celery do manager já faz).
: "${NODE_HOSTNAME:?NODE_HOSTNAME nao definido}"

echo "[CP Agente] Aguardando PostgreSQL em $POSTGRES_HOST:$POSTGRES_PORT..."
until pg_isready -h "$POSTGRES_HOST" -p "${POSTGRES_PORT:-5432}" -U "$POSTGRES_USER" -q; do
  sleep 2
done

exec celery -A core worker --loglevel=info --concurrency=2 \
  -Q "no.${NODE_HOSTNAME}" -n "agente@${NODE_HOSTNAME}"
