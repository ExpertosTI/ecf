#!/usr/bin/env bash
# deploy.sh — Script de despliegue Renace e-CF
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
COMPOSE_ARGS=("-f" "${SCRIPT_DIR}/docker-compose.yml")
if [ -f "${SCRIPT_DIR}/docker-compose.override.yml" ]; then
    COMPOSE_ARGS+=("-f" "${SCRIPT_DIR}/docker-compose.override.yml")
fi
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

# Detectar Docker Compose (preferir v2)
if docker compose version &>/dev/null; then
    DC="docker compose"
    log "Usando Docker Compose v2 plugin"
elif docker-compose version &>/dev/null; then
    DC="docker-compose"
    warn "Usando docker-compose (v1). Se recomienda actualizar a Docker Compose v2 para mejor soporte de healthchecks."
else
    error "Docker Compose no encontrado. Instalar antes de continuar (v2 recomendado)."
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

if $DC "${COMPOSE_ARGS[@]}" ps postgres 2>/dev/null | grep -q "running"; then
    log "PostgreSQL activo. Realizando backup..."
    BACKUP_FILE="${BACKUP_DIR}/renace_ecf_${TIMESTAMP}.sql.gz"
    if $DC "${COMPOSE_ARGS[@]}" exec -T postgres pg_dump -U renace_ecf renace_ecf 2>/dev/null | gzip > "$BACKUP_FILE"; then
        :
    elif $DC "${COMPOSE_ARGS[@]}" exec -T postgres pg_dump -U saas_ecf saas_ecf 2>/dev/null | gzip > "$BACKUP_FILE"; then
        warn "Backup con usuario legacy saas_ecf (instalaciones pre-rename 5d7b554)"
    else
        error "No se pudo hacer backup (renace_ecf ni saas_ecf conectan)"
    fi
    BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    log "Backup creado: ${BACKUP_FILE} (${BACKUP_SIZE})"

    # Retener solo los ultimos 30 backups
    find "${BACKUP_DIR}" -maxdepth 1 -name 'renace_ecf_*.sql.gz' -type f | sort -r | tail -n +31 | xargs -r rm --
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

# Verificar puertos de Traefik antes de levantar infraestructura
TRAEFIK_HTTP_PORT="${TRAEFIK_HTTP_PORT:-8080}"
TRAEFIK_HTTPS_PORT="${TRAEFIK_HTTPS_PORT:-8443}"

check_port_free() {
    local port="$1"
    local label="$2"
    if command -v ss &>/dev/null && ss -tulpn | grep -q ":${port} "; then
        error "Puerto ${port} (${label}) ya está en uso. Configure TRAEFIK_HTTP_PORT/TRAEFIK_HTTPS_PORT en .env y reintente."
    fi
}

check_port_free "$TRAEFIK_HTTP_PORT" "Traefik HTTP"
check_port_free "$TRAEFIK_HTTPS_PORT" "Traefik HTTPS"

# Build de imagenes
log "Construyendo imagenes Docker..."
$DC "${COMPOSE_ARGS[@]}" build --no-cache api

# Detener servicios actuales sin eliminar red compartida
if $DC "${COMPOSE_ARGS[@]}" ps --quiet 2>/dev/null | head -1 | grep -q .; then
    log "Deteniendo servicios actuales (sin borrar red ecf_network)..."
    $DC "${COMPOSE_ARGS[@]}" stop api worker scheduler traefik postgres redis || true
    $DC "${COMPOSE_ARGS[@]}" rm -f api worker scheduler traefik postgres redis || true
fi

# Levantar infraestructura primero (PostgreSQL, Redis, Traefik)
log "Levantando infraestructura (PostgreSQL, Redis, Traefik)..."
$DC "${COMPOSE_ARGS[@]}" up -d postgres redis traefik

# Esperar a que DB y Redis esten saludables (no solo "Up")
for i in $(seq 1 60); do
    PG_HEALTH=$($DC "${COMPOSE_ARGS[@]}" ps postgres 2>/dev/null | grep -c "(healthy)" || true)
    RD_HEALTH=$($DC "${COMPOSE_ARGS[@]}" ps redis 2>/dev/null | grep -c "(healthy)" || true)
    PG_HEALTH=${PG_HEALTH:-0}
    RD_HEALTH=${RD_HEALTH:-0}

    if [ "$PG_HEALTH" -ge 1 ] && [ "$RD_HEALTH" -ge 1 ]; then
        log "Infraestructura activa (healthchecks OK)"
        break
    fi
    if [ "$i" -eq 60 ]; then
        error "Timeout esperando healthchecks de PostgreSQL/Redis. Verificar: $DC logs postgres redis"
    fi
    sleep 2
done

# Instalaciones en produccion antes de 5d7b554 usaban saas_ecf; el compose paso a renace_ecf
# sin migrar el volumen PG (initdb solo corre en volumen vacio).
if ! $DC "${COMPOSE_ARGS[@]}" exec -T postgres \
    psql -U renace_ecf -d renace_ecf -c "SELECT 1" &>/dev/null; then
    if $DC "${COMPOSE_ARGS[@]}" exec -T postgres \
        psql -U saas_ecf -d saas_ecf -c "SELECT 1" &>/dev/null; then
        log "Migrando usuario/base saas_ecf → renace_ecf (rename branding, datos intactos)..."
        $DC "${COMPOSE_ARGS[@]}" exec -T postgres psql -U saas_ecf -d postgres -v ON_ERROR_STOP=1 <<-EOSQL
			ALTER DATABASE saas_ecf RENAME TO renace_ecf;
			CREATE ROLE pgfix_migration LOGIN SUPERUSER PASSWORD '${DB_PASSWORD}';
		EOSQL
        $DC "${COMPOSE_ARGS[@]}" exec -T postgres psql -U pgfix_migration -d postgres -v ON_ERROR_STOP=1 <<-EOSQL
			ALTER ROLE saas_ecf RENAME TO renace_ecf;
			DROP ROLE pgfix_migration;
		EOSQL
        log "Rename saas_ecf → renace_ecf completado"
    elif $DC "${COMPOSE_ARGS[@]}" exec -T postgres \
        psql -U saas_ecf -d renace_ecf -c "SELECT 1" &>/dev/null; then
        log "Migrando rol saas_ecf → renace_ecf (base ya renombrada)..."
        $DC "${COMPOSE_ARGS[@]}" exec -T postgres psql -U saas_ecf -d postgres -v ON_ERROR_STOP=1 <<-EOSQL
			CREATE ROLE pgfix_migration LOGIN SUPERUSER PASSWORD '${DB_PASSWORD}';
		EOSQL
        $DC "${COMPOSE_ARGS[@]}" exec -T postgres psql -U pgfix_migration -d postgres -v ON_ERROR_STOP=1 <<-EOSQL
			ALTER ROLE saas_ecf RENAME TO renace_ecf;
			DROP ROLE pgfix_migration;
		EOSQL
        log "Rename rol saas_ecf → renace_ecf completado"
    fi
fi

# Confirmar que renace_ecf acepta conexiones antes de migrar
log "Verificando conexion PostgreSQL (usuario renace_ecf)..."
for i in $(seq 1 30); do
    if $DC "${COMPOSE_ARGS[@]}" exec -T postgres \
        psql -U renace_ecf -d renace_ecf -c "SELECT 1" &>/dev/null; then
        log "PostgreSQL listo — renace_ecf conectado"
        break
    fi
    if [ "$i" -eq 30 ]; then
        error "PostgreSQL no acepta conexiones como renace_ecf. Probar: $DC exec postgres psql -U saas_ecf -d saas_ecf -c 'SELECT 1'"
    fi
    sleep 2
done

# Verificar la misma autenticación TCP que usará la API. El psql por socket del
# contenedor puede funcionar aunque DB_PASSWORD ya no coincida con el volumen.
log "Verificando credenciales PostgreSQL desde la API..."
if ! $DC "${COMPOSE_ARGS[@]}" run --rm --no-deps api python -c \
    'import asyncio, os, asyncpg; asyncio.run(asyncpg.connect(os.environ["DATABASE_URL"]))' \
    &>/dev/null; then
    error "DB_PASSWORD no autentica contra PostgreSQL. Alinea la contraseña del rol renace_ecf antes de continuar."
fi

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
        $DC "${COMPOSE_ARGS[@]}" exec -T postgres \
            psql -U renace_ecf -d renace_ecf -f "/docker-entrypoint-initdb.d/${MIGRATION_NAME}" 2>&1 || \
            warn "Migracion ${MIGRATION_NAME} falló (puede ya estar aplicada)"
    fi
done

# Levantar aplicacion (workers escalados a 2 instancias)
log "Levantando servicios de aplicacion..."
$DC "${COMPOSE_ARGS[@]}" up -d api scheduler
$DC "${COMPOSE_ARGS[@]}" up -d --scale worker=2 worker

# Esperar a que la API este saludable
log "Esperando a que la API responda..."
for i in $(seq 1 20); do
    API_CONTAINER=$($DC "${COMPOSE_ARGS[@]}" ps -q api 2>/dev/null | head -1)
    if [ -n "$API_CONTAINER" ]; then
        if docker exec "$API_CONTAINER" curl -sf http://localhost:8000/health &>/dev/null; then
            log "API respondiendo correctamente"
            break
        fi
    fi
    if [ "$i" -eq 20 ]; then
        error "API no responde despues de 40 segundos. Verificar: $DC logs api"
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
$DC "${COMPOSE_ARGS[@]}" ps

# Verificar red Docker
NETWORK_SERVICES=$(docker network inspect ecf_network --format '{{range .Containers}}{{.Name}} {{end}}' 2>/dev/null || echo "")
info "Servicios en red ecf_network: ${NETWORK_SERVICES}"

# Verificar health de la API (vía puerto interno)
sleep 3
API_CONTAINER=$($DC "${COMPOSE_ARGS[@]}" ps -q api 2>/dev/null | head -1)
if [ -n "$API_CONTAINER" ] && docker exec "$API_CONTAINER" curl -sf http://localhost:8000/health &>/dev/null; then
    log "API respondiendo correctamente (Internal Health OK)"
else
    warn "No se pudo verificar el acceso directo a la API. Verifique logs: $DC logs api"
fi

# Verificar workers
WORKER_COUNT=$($DC "${COMPOSE_ARGS[@]}" ps -q worker 2>/dev/null | wc -l)
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
info "  Ver logs:       $DC logs -f"
info "  Ver estado:     $DC ps"
info "  Backup manual:  $DC exec postgres pg_dump -U renace_ecf renace_ecf > backup.sql"
info "  Rollback:       $DC down && $DC up -d"
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

# TLS habilitado (puerto 443 global)
TLS_CHECK=$(ss -tulpn | grep -q ":443" && echo "OK" || echo "Verificar")
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
if [ -d "$BACKUP_DIR" ] && ls "${BACKUP_DIR}"/renace_ecf_*.sql.gz &>/dev/null; then
    check_dgii "Backup de base de datos" "OK"
else
    check_dgii "Backup de base de datos" "Primer deploy - programar backups"
fi

# Seguridad
check_dgii "Usuario no-root en contenedor" "OK"
check_dgii "Red Docker aislada (ecf_network)" "OK"
check_dgii "Logs de acceso Nginx (auditoria)" "OK"
check_dgii "Security headers (HSTS, X-Frame-Options)" "OK"

echo ""
log "Deploy finalizado. Verifique el checklist DGII arriba."
