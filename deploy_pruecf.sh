#!/usr/bin/env bash
# deploy_pruecf.sh — Script de despliegue para el Simulador Interno (pruecf.renace.tech)
set -euo pipefail

# --- Colores ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()   { echo -e "${GREEN}[DEPLOY PRUECF]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }

# Detectar Docker Compose (preferir v2)
if docker compose version &>/dev/null; then
    DC="docker compose"
elif docker-compose version &>/dev/null; then
    DC="docker-compose"
else
    error "Docker Compose no encontrado."
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_ARGS=("-p" "pruecf" "-f" "${SCRIPT_DIR}/docker-compose.pruecf.yml")
ENV_FILE="${SCRIPT_DIR}/.env.pruecf"

log "Iniciando despliegue de la instancia de Staging (pruecf.renace.tech)"

if [ ! -f "$ENV_FILE" ]; then
    warn "No se encontro .env.pruecf. Creando uno basado en .env.example..."
    cp "${SCRIPT_DIR}/.env.example" "$ENV_FILE"
    # Reemplazar ECF_DOMAIN
    sed -i 's/ECF_DOMAIN=.*/ECF_DOMAIN=pruecf.renace.tech/' "$ENV_FILE"
    error "Edita .env.pruecf con los valores correctos (claves, contraseñas, etc) y vuelve a ejecutar."
fi

# Cargar variables
set -a
source "$ENV_FILE"
set +a

# Verificar que la red ecf_network existe (la crea la instancia ecf principal)
if ! docker network inspect ecf_network &>/dev/null; then
    warn "La red ecf_network no existe. Creandola para que pruecf pueda comunicarse..."
    docker network create ecf_network
fi

# Build de imagenes
log "Construyendo imagenes Docker..."
$DC "${COMPOSE_ARGS[@]}" build --no-cache api

# Detener si existe
if $DC "${COMPOSE_ARGS[@]}" ps --quiet 2>/dev/null | head -1 | grep -q .; then
    log "Deteniendo servicios pruecf..."
    $DC "${COMPOSE_ARGS[@]}" down --timeout 30
fi

# Levantar infraestructura
log "Levantando PostgreSQL y Redis para pruecf..."
$DC "${COMPOSE_ARGS[@]}" up -d postgres redis

log "Esperando servicios..."
sleep 10

# Migraciones
log "Ejecutando migraciones SQL para pruecf..."
for migration in "${SCRIPT_DIR}"/db/0[0-9][0-9]_*.sql; do
    if [ -f "$migration" ]; then
        MIGRATION_NAME=$(basename "$migration")
        if [ "$MIGRATION_NAME" = "001_schema.sql" ]; then
            continue
        fi
        log "Aplicando migracion: ${MIGRATION_NAME}"
        $DC "${COMPOSE_ARGS[@]}" exec -T postgres \
            psql -U renace_ecf -d renace_ecf -f "/docker-entrypoint-initdb.d/${MIGRATION_NAME}" 2>&1 || true
    fi
done

# Levantar resto
log "Levantando api, worker, scheduler para pruecf..."
$DC "${COMPOSE_ARGS[@]}" up -d api scheduler worker

log "=========================================="
log "DEPLOY PRUECF COMPLETADO EXITOSAMENTE"
log "=========================================="
info "Dominio: https://pruecf.renace.tech"
info "Para ver logs: $DC -p pruecf -f docker-compose.pruecf.yml logs -f"
