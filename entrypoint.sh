#!/bin/sh
set -e

# Migrations are NOT run here — a dedicated one-shot `migrate` service
# owns them so that scaling the backend to N replicas doesn't run N
# concurrent `alembic upgrade` against the same database. The backend
# waits on that service via `depends_on` in docker-compose.

# One uvicorn worker per container by design: horizontal scale comes
# from container replicas behind the nginx load balancer, which keeps
# the in-process /metrics counters coherent per replica for the
# autoscaler. Override with UVICORN_WORKERS if you really want process
# fan-out inside a container.
echo "==> Starting Eduru API (workers=${UVICORN_WORKERS:-1})..."
exec uvicorn backend.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers "${UVICORN_WORKERS:-1}"
