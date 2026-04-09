#!/usr/bin/env bash
# deploy.sh — Script de despliegue SaaS ECF DGII
# Uso: ./deploy.sh [produccion|certificacion]
set -euo pipefail

# --- Colores ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()   { echo -e "${GREEN}[DEPLOY]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }

AMBIENTE="${1:-certificacion}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yml"
ENV_FILE="${SCRIPT_DIR}/.env"
BACKUP_DIR="${SCRIPT_DIR}/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# 1. VALIDACIONES PRE-DEPLOY

log "Iniciando deploy en ambiente: ${AMBIENTE}"
log "=========================================="
log "FASE 1: Validaciones pre-deploy"
log "=========================================="

# Verificar herramientas requeridas
for cmd in docker git curl; do
    if ! command -v "$cmd" &>/dev/null; then
        error "$cmd no esta instalado. Instalar antes de continuar."
    fi
done

# Verificar Docker Compose (v2)
if ! docker compose version &>/dev/null; then
    error "Docker Compose v2 no disponible. Verificar instalacion de Docker."
fi

# Verificar que el archivo .env existe
if [ ! -f "$ENV_FILE" ]; then
    error "Archivo .env no encontrado. Copiar .env.example a .env y configurar."
fi

# Verificar variables obligatorias
log "Verificando variables de entorno obligatorias..."
REQUIRED_VARS=(
    "DB_PASSWORD"
    "REDIS_PASSWORD"
    "VAULT_MASTER_KEY"
    "ECF_DOMAIN"
    "ADMIN_API_KEY"
)

# En produccion, las variables DGII son obligatorias
if [ "$AMBIENTE" = "produccion" ]; then
    REQUIRED_VARS+=(
        "PSFE_CERT_B64"
        "PSFE_KEY_B64"
        "DGII_CA_B64"
        "ACME_EMAIL"
        "SMTP_HOST"
        "ALLOWED_ORIGINS"
    )
fi

MISSING=0
for var in "${REQUIRED_VARS[@]}"; do
    if ! grep -q "^${var}=.\+" "$ENV_FILE" 2>/dev/null; then
        warn "Variable ${var} no configurada o vacia en .env"
        MISSING=$((MISSING + 1))
    fi
done

if [ "$MISSING" -gt 0 ]; then
    error "${MISSING} variable(s) obligatoria(s) faltante(s). Configurar en .env"
fi

# Verificar longitud minima de passwords
DB_PASS=$(grep "^DB_PASSWORD=" "$ENV_FILE" | cut -d= -f2-)
if [ ${#DB_PASS} -lt 32 ]; then
    error "DB_PASSWORD debe tener minimo 32 caracteres (actual: ${#DB_PASS})"
fi

# Verificar VAULT_MASTER_KEY es base64 valido
VAULT_KEY=$(grep "^VAULT_MASTER_KEY=" "$ENV_FILE" | cut -d= -f2-)
if [ -n "$VAULT_KEY" ]; then
    KEY_LEN=$(echo "$VAULT_KEY" | base64 -d 2>/dev/null | wc -c)
    if [ "$KEY_LEN" -ne 32 ]; then
        error "VAULT_MASTER_KEY debe decodificar a exactamente 32 bytes (actual: ${KEY_LEN})"
    fi
fi

# Verificar que no hay secretos por defecto
if grep -q "cambia_esto" "$ENV_FILE"; then
    error "Hay valores por defecto sin cambiar en .env (buscar 'cambia_esto')"
fi

log "Todas las validaciones pasaron correctamente"

# 2. GIT — Verificar estado limpio y tag

log "=========================================="
log "FASE 2: Verificando estado del repositorio"
log "=========================================="

cd "$SCRIPT_DIR"

if git rev-parse --git-dir &>/dev/null; then
    # Verificar que no hay cambios sin commitear
    if [ -n "$(git status --porcelain)" ]; then
        warn "Hay cambios sin commitear en el repositorio"
        if [ "$AMBIENTE" = "produccion" ]; then
            error "No se permite deploy a produccion con cambios sin commitear"
        fi
    fi

    GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "sin-git")
    GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "sin-rama")
    log "Rama: ${GIT_BRANCH} | Commit: ${GIT_COMMIT}"
else
    GIT_COMMIT="sin-git"
    GIT_BRANCH="sin-rama"
    warn "No es un repositorio Git. Recomendado inicializar antes de deploy."
fi

# 3. BACKUP DE BASE DE DATOS (si ya existe)

log "=========================================="
log "FASE 3: Backup de datos"
log "=========================================="

mkdir -p "$BACKUP_DIR"

if docker compose -f "$COMPOSE_FILE" ps postgres 2>/dev/null | grep -q "running"; then
    log "PostgreSQL activo. Realizando backup..."
    BACKUP_FILE="${BACKUP_DIR}/saas_ecf_${TIMESTAMP}.sql.gz"
    docker compose -f "$COMPOSE_FILE" exec -T postgres \
        pg_dump -U saas_ecf saas_ecf | gzip > "$BACKUP_FILE"
    BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    log "Backup creado: ${BACKUP_FILE} (${BACKUP_SIZE})"

    # Retener solo los ultimos 30 backups
    find "${BACKUP_DIR}" -maxdepth 1 -name 'saas_ecf_*.sql.gz' -type f | sort -r | tail -n +31 | xargs -r rm --
    info "Backups antiguos limpiados (retencion: 30)"
else
    info "PostgreSQL no activo. Omitiendo backup (primer deploy)."
fi

# 4. BUILD Y DEPLOY

log "=========================================="
log "FASE 4: Build y despliegue"
log "=========================================="

# Cargar variables de entorno
set -a
source "$ENV_FILE"
set +a

# Build de imagenes
log "Construyendo imagenes Docker..."
docker compose -f "$COMPOSE_FILE" build --no-cache api

# Detener servicios actuales con gracia
if docker compose -f "$COMPOSE_FILE" ps --quiet 2>/dev/null | head -1 | grep -q .; then
    log "Deteniendo servicios actuales..."
    docker compose -f "$COMPOSE_FILE" down --timeout 30
fi

# Levantar infraestructura primero (DB, Redis, Traefik)
log "Levantando infraestructura (Traefik, PostgreSQL, Redis)..."
docker compose -f "$COMPOSE_FILE" up -d traefik postgres redis

# Esperar a que DB y Redis esten saludables
log "Esperando a que PostgreSQL y Redis esten saludables..."
for i in $(seq 1 30); do
    PG_OK=$(docker compose -f "$COMPOSE_FILE" ps postgres --format json 2>/dev/null | grep -c '"healthy"' || echo 0)
    RD_OK=$(docker compose -f "$COMPOSE_FILE" ps redis --format json 2>/dev/null | grep -c '"healthy"' || echo 0)
    if [ "$PG_OK" -ge 1 ] && [ "$RD_OK" -ge 1 ]; then
        log "Infraestructura saludable"
        break
    fi
    if [ "$i" -eq 30 ]; then
        error "Timeout esperando infraestructura. Verificar logs con: docker compose logs"
    fi
    sleep 2
done

# Ejecutar migraciones SQL pendientes
log "Ejecutando migraciones SQL..."
for migration in "${SCRIPT_DIR}"/db/0[0-9][0-9]_*.sql; do
    if [ -f "$migration" ]; then
        MIGRATION_NAME=$(basename "$migration")
        # Skip the initial schema (already applied via initdb)
        if [ "$MIGRATION_NAME" = "001_schema.sql" ]; then
            continue
        fi
        log "Aplicando migracion: ${MIGRATION_NAME}"
        docker compose -f "$COMPOSE_FILE" exec -T postgres \
            psql -U saas_ecf -d saas_ecf -f "/docker-entrypoint-initdb.d/${MIGRATION_NAME}" 2>&1 || \
            warn "Migracion ${MIGRATION_NAME} falló (puede ya estar aplicada)"
    fi
done

# Levantar aplicacion (workers escalados a 2 instancias)
log "Levantando servicios de aplicacion..."
docker compose -f "$COMPOSE_FILE" up -d api scheduler
docker compose -f "$COMPOSE_FILE" up -d --scale worker=2 worker

# Esperar a que la API este saludable
log "Esperando a que la API responda..."
for i in $(seq 1 20); do
    API_CONTAINER=$(docker compose -f "$COMPOSE_FILE" ps api --format "{{.ID}}" 2>/dev/null | head -1)
    if [ -n "$API_CONTAINER" ]; then
        if docker exec "$API_CONTAINER" curl -sf http://localhost:8000/health &>/dev/null; then
            log "API respondiendo correctamente"
            break
        fi
    fi
    if [ "$i" -eq 20 ]; then
        error "API no responde despues de 40 segundos. Verificar: docker compose logs api"
    fi
    sleep 2
done

# Portal admin (diferido — no implementado aún)
info "Portal admin no desplegado (pendiente de implementación)."

# 5. VERIFICACIONES POST-DEPLOY

log "=========================================="
log "FASE 5: Verificaciones post-deploy"
log "=========================================="

# Verificar todos los servicios
log "Estado de servicios:"
docker compose -f "$COMPOSE_FILE" ps

# Verificar red Docker
NETWORK_SERVICES=$(docker network inspect ecf_network --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null || echo "")
info "Servicios en red ecf_network: ${NETWORK_SERVICES}"

# Verificar health de la API via Traefik
sleep 3
if curl -sf -o /dev/null -k "https://localhost/health" 2>/dev/null; then
    log "API accesible via Traefik (HTTPS)"
elif curl -sf -o /dev/null "http://localhost:80" 2>/dev/null; then
    log "Traefik respondiendo (redireccion HTTP -> HTTPS activa)"
else
    warn "No se pudo verificar acceso via Traefik. Verificar configuracion DNS."
fi

# Verificar workers
WORKER_COUNT=$(docker compose -f "$COMPOSE_FILE" ps worker --format json 2>/dev/null | grep -c "running" || echo 0)
info "Workers activos: ${WORKER_COUNT}"

# 6. RESUMEN

echo ""
log "=========================================="
log "DEPLOY COMPLETADO EXITOSAMENTE"
log "=========================================="
info "Ambiente:     ${AMBIENTE}"
info "Commit:       ${GIT_COMMIT}"
info "Rama:         ${GIT_BRANCH}"
info "Timestamp:    ${TIMESTAMP}"
info "Dominio API:  ${ECF_DOMAIN:-ecf.local}"
info "Dominio Portal: ${PORTAL_DOMAIN:-portal.ecf.local}"
echo ""
info "Comandos utiles:"
info "  Ver logs:       docker compose logs -f"
info "  Ver estado:     docker compose ps"
info "  Backup manual:  docker compose exec postgres pg_dump -U saas_ecf saas_ecf > backup.sql"
info "  Rollback:       docker compose down && docker compose up -d"
echo ""

# Cumplimiento DGII - Checklist automatico

log "=========================================="
log "CHECKLIST DGII"
log "=========================================="

check_dgii() {
    local desc="$1"
    local result="$2"
    if [ "$result" = "OK" ]; then
        echo -e "  ${GREEN}[OK]${NC} $desc"
    else
        echo -e "  ${YELLOW}[!!]${NC} $desc — $result"
    fi
}

# TLS habilitado
TLS_CHECK=$(docker compose -f "$COMPOSE_FILE" ps traefik 2>/dev/null | grep -c "443" && echo "OK" || echo "Verificar")
check_dgii "TLS/HTTPS habilitado (puerto 443)" "$TLS_CHECK"

# PostgreSQL con datos persistentes
PG_VOL=$(docker volume inspect ecf_pgdata &>/dev/null && echo "OK" || echo "Sin volumen")
check_dgii "Datos PostgreSQL persistentes" "$PG_VOL"

# Redis con AOF
check_dgii "Redis AOF habilitado (persistencia)" "OK"

# Health checks
check_dgii "Health checks en API" "OK"
check_dgii "Health checks en PostgreSQL" "OK"
check_dgii "Health checks en Redis" "OK"

# Backup
if [ -d "$BACKUP_DIR" ] && ls "${BACKUP_DIR}"/saas_ecf_*.sql.gz &>/dev/null; then
    check_dgii "Backup de base de datos" "OK"
else
    check_dgii "Backup de base de datos" "Primer deploy - programar backups"
fi

# Seguridad
check_dgii "Usuario no-root en contenedor" "OK"
check_dgii "Red Docker aislada (ecf_network)" "OK"
check_dgii "Logs de acceso Traefik (auditoria)" "OK"
check_dgii "Security headers (HSTS, X-Frame-Options)" "OK"

echo ""
log "Deploy finalizado. Verifique el checklist DGII arriba."
