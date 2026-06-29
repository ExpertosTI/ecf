#!/usr/bin/env bash
# Recuperación one-shot — servidor REY (/opt/ecf)
# Uso: cd /opt/ecf && source .env && bash scripts/server_recovery.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source .env

DC="docker-compose"
log() { echo "[RECOVERY] $*"; }

# 1. Traefik debe enrutar todo el host (landing en /), no solo /v1
if grep -q 'PathPrefix(`/v1`)' docker-compose.yml; then
    sed -i 's| && PathPrefix(`/v1`)||' docker-compose.yml
    log "Traefik: quitado PathPrefix(/v1)"
fi

# 2. Rol PG legacy saas_ecf → renace_ecf (si aplica)
$DC up -d postgres
sleep 8
if ! $DC exec -T postgres psql -U renace_ecf -d renace_ecf -c "SELECT 1" &>/dev/null; then
    if $DC exec -T postgres psql -U saas_ecf -d renace_ecf -c "SELECT 1" &>/dev/null; then
        log "PG: renombrando rol saas_ecf → renace_ecf..."
        $DC exec -T postgres psql -U saas_ecf -d postgres -c \
            "CREATE ROLE pgfix_migration LOGIN SUPERUSER PASSWORD '$DB_PASSWORD';" 2>/dev/null || true
        $DC exec -T postgres psql -U pgfix_migration -d postgres -c \
            "ALTER ROLE saas_ecf RENAME TO renace_ecf;"
        $DC exec -T postgres psql -U renace_ecf -d postgres -c \
            "DROP ROLE IF EXISTS pgfix_migration;"
        log "PG: rol renombrado"
    fi
fi

# 3. Alinear puerto Traefik con nginx del host (nginx suele apuntar a 8443)
HTTPS_PORT="${TRAEFIK_HTTPS_PORT:-8443}"
if [ "$HTTPS_PORT" != "8443" ] && grep -rq '127.0.0.1:8443' /etc/nginx/ 2>/dev/null; then
    log "Ajustando TRAEFIK_HTTPS_PORT 8443 (nginx del host usa 8443)"
    if grep -q '^TRAEFIK_HTTPS_PORT=' .env; then
        sed -i 's/^TRAEFIK_HTTPS_PORT=.*/TRAEFIK_HTTPS_PORT=8443/' .env
    else
        echo 'TRAEFIK_HTTPS_PORT=8443' >> .env
    fi
    source .env
fi

# 4. Levantar stack (down/up evita bug ContainerConfig de compose v1)
log "Reiniciando stack..."
$DC down 2>/dev/null || true
$DC up -d
$DC up -d --scale worker=2 worker 2>/dev/null || $DC up -d worker

log "Esperando API (60s)..."
for i in $(seq 1 30); do
    if $DC exec -T api curl -sf http://localhost:8000/health &>/dev/null; then
        log "API health OK"
        break
    fi
    sleep 2
done

# 5. Verificación
echo ""
$DC ps
echo ""
if $DC exec -T api curl -sf http://localhost:8000/health; then
    echo "OK — API interna"
else
    echo "FALLO — API interna. Ver: $DC logs --tail=30 api"
    exit 1
fi

EXT=$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:${TRAEFIK_HTTPS_PORT:-8443}/" -H "Host: ${ECF_DOMAIN}")
echo "Traefik local (${TRAEFIK_HTTPS_PORT:-8443}): HTTP $EXT"
EXT2=$(curl -sk -o /dev/null -w '%{http_code}' "https://${ECF_DOMAIN}/")
echo "Público https://${ECF_DOMAIN}/: HTTP $EXT2"
if [ "$EXT2" != "200" ] && [ "$EXT" = "200" ]; then
    echo ""
    echo ">>> nginx no apunta al puerto Traefik correcto."
    echo ">>> Editar /etc/nginx/sites-enabled/*ecf* y usar:"
    echo ">>>   proxy_pass https://127.0.0.1:${TRAEFIK_HTTPS_PORT:-8443};"
    echo ">>> Luego: nginx -t && systemctl reload nginx"
fi
