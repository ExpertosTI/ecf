#!/usr/bin/env bash
# Despliegue Swarm (servidor REY) — Traefik en 443, NO docker-compose en 8443
# Uso: cd /opt/ecf && bash scripts/stack_deploy.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
    echo "ERROR: .env no encontrado"
    exit 1
fi

set -a
source .env
set +a

STACK="${STACK_NAME:-ecf}"

echo "[stack] Build imagen..."
docker build -t ecf_api:latest -f Dockerfile.api .

echo "[stack] Red RenaceNet..."
docker network create -d overlay RenaceNet 2>/dev/null || true
touch traefik_dynamic.yml 2>/dev/null || true

echo "[stack] Deploy stack ${STACK}..."
docker stack deploy -c docker-compose.prod.yml "$STACK"

echo "[stack] Esperando servicios (90s)..."
for i in $(seq 1 45); do
    API=$(docker service ls --filter "name=${STACK}_api" --format '{{.Replicas}}' 2>/dev/null || echo "0/1")
    if [[ "$API" == "1/1" ]]; then
        echo "[stack] ecf_api 1/1"
        break
    fi
    sleep 2
done

echo ""
docker service ls
echo ""

API_REP=$(docker service ls --filter "name=${STACK}_api" --format '{{.Replicas}}')
if [[ "$API_REP" != "1/1" ]]; then
    echo "ERROR: ${STACK}_api = ${API_REP}"
    echo "--- docker service ps ${STACK}_api ---"
    docker service ps "${STACK}_api" --no-trunc 2>&1 | head -5
    echo "--- logs ---"
    docker service logs "${STACK}_api" --tail 25 2>&1 || true
    exit 1
fi

curl -sk -o /dev/null -w "https://${ECF_DOMAIN}/ → HTTP %{http_code}\n" "https://${ECF_DOMAIN}/"
echo "[stack] Listo."
