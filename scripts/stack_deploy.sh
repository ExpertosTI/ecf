#!/usr/bin/env bash
# =============================================================================
# stack_deploy.sh — Despliegue Swarm RENECF con gate de certificación DGII
#
# Uso (en REY):
#   cd /opt/ecf && bash scripts/stack_deploy.sh certificacion
#   cd /opt/ecf && bash scripts/stack_deploy.sh produccion
#
# Sin ambiente explícito → certificacion (fase CerteCF).
# Si faltan requisitos de certificación, el script ABORTA (no despliega a medias).
# =============================================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()   { echo -e "${GREEN}[stack]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

AMBIENTE="${1:-certificacion}"
case "$AMBIENTE" in
    certificacion|certecf|CerteCF) AMBIENTE="certificacion" ;;
    produccion|eCF|ecf)            AMBIENTE="produccion" ;;
    simulacion)                    AMBIENTE="simulacion" ;;
    *)
        error "Ambiente inválido: '$1'. Use: certificacion | produccion | simulacion"
        ;;
esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ── FASE 0: herramientas ────────────────────────────────────────────────────
for cmd in docker git curl; do
    command -v "$cmd" &>/dev/null || error "$cmd no está instalado"
done
docker info &>/dev/null || error "Docker no responde (¿daemon / Swarm?)"

# ── FASE 1: .env + variables críticas ───────────────────────────────────────
log "=============================================="
log "FASE 1: Preflight certificación ($AMBIENTE)"
log "=============================================="

[[ -f .env ]] || error ".env no encontrado en $ROOT"

set -a
# shellcheck disable=SC1091
source .env
set +a

# Forzar ambiente del argumento (no dejar que un default de compose mande a eCF)
export ECF_AMBIENTE="$AMBIENTE"
# Alinear .env en disco para próximos deploys / restarts
if grep -q '^ECF_AMBIENTE=' .env; then
    sed -i.bak "s/^ECF_AMBIENTE=.*/ECF_AMBIENTE=${AMBIENTE}/" .env && rm -f .env.bak
else
    echo "ECF_AMBIENTE=${AMBIENTE}" >> .env
fi

REQUIRED_VARS=(
    DB_PASSWORD
    REDIS_PASSWORD
    VAULT_MASTER_KEY
    ECF_DOMAIN
    ADMIN_API_KEY
    DGII_SOFTWARE_NAME
    DGII_SOFTWARE_VERSION
    DGII_SOFTWARE_TIPO
)

if [[ "$AMBIENTE" == "produccion" ]]; then
    REQUIRED_VARS+=(ACME_EMAIL ALLOWED_ORIGINS)
fi

MISSING=0
for var in "${REQUIRED_VARS[@]}"; do
    val="${!var:-}"
    if [[ -z "$val" ]]; then
        warn "Falta o vacía: $var"
        MISSING=$((MISSING + 1))
    fi
done
[[ "$MISSING" -eq 0 ]] || error "$MISSING variable(s) obligatoria(s). Complete .env y reintente."

# Identidad software ante DGII (postulación / CerteCF)
SOFT_NAME="${DGII_SOFTWARE_NAME:-}"
SOFT_VER="${DGII_SOFTWARE_VERSION:-}"
SOFT_TIPO="${DGII_SOFTWARE_TIPO:-}"
[[ "$SOFT_NAME" == "RENECF" ]] || error "DGII_SOFTWARE_NAME debe ser RENECF (actual: '$SOFT_NAME')"
[[ "$SOFT_VER" =~ ^[0-9]+\.[0-9]+$ ]] || error "DGII_SOFTWARE_VERSION debe ser xs:double (ej. 2.5), no semver. Actual: '$SOFT_VER'"
[[ "$SOFT_TIPO" == "PROPIO" ]] || warn "DGII_SOFTWARE_TIPO='$SOFT_TIPO' (esperado PROPIO)"

# XSD: obligatorio en certificación/producción
SKIP_XSD="${SKIP_XSD_VALIDATION:-false}"
export SKIP_XSD_VALIDATION="$SKIP_XSD"
if [[ "$AMBIENTE" != "simulacion" && "${SKIP_XSD,,}" == "true" ]]; then
    error "SKIP_XSD_VALIDATION=true prohibido en ambiente $AMBIENTE (DGII exige validación XSD)"
fi

# Bundle XSD en el repo (debe entrar a la imagen)
REQUIRED_XSD=(
    ECF-31.xsd ECF-32.xsd ECF-33.xsd ECF-34.xsd
    ECF-41.xsd ECF-43.xsd ECF-44.xsd ECF-45.xsd ECF-46.xsd ECF-47.xsd
    RFCE-32.xsd ACECF.xsd ARECF.xsd ANECF.xsd Semilla.xsd
)
XSD_MISS=0
for x in "${REQUIRED_XSD[@]}"; do
    if [[ ! -f "xsd/$x" ]]; then
        warn "XSD ausente: xsd/$x"
        XSD_MISS=$((XSD_MISS + 1))
    fi
done
[[ "$XSD_MISS" -eq 0 ]] || error "$XSD_MISS XSD faltante(s). Sin ellos CerteCF rechaza / firma inválida."

# PSFE: en certificación debe existir en .env O ya en DB (portal). Advertimos fuerte.
PSFE_OK=0
if [[ -n "${PSFE_CERT_B64:-}" && -n "${PSFE_KEY_B64:-}" && -n "${DGII_CA_B64:-}" ]]; then
    PSFE_OK=1
    log "PSFE presente en .env (mTLS CerteCF)"
else
    warn "PSFE_* / DGII_CA_B64 no están en .env — se esperan cargados vía portal (platform_psfe)"
    warn "Sin PSFE, Autenticación Semilla / Probar CerteCF fallará tras el deploy"
fi

if [[ "$AMBIENTE" == "produccion" && "$PSFE_OK" -ne 1 ]]; then
    error "Producción exige PSFE_CERT_B64 + PSFE_KEY_B64 + DGII_CA_B64 en .env"
fi

# Operador Renace
export PLATFORM_OPERATOR_RNC="${PLATFORM_OPERATOR_RNC:-132842316}"
export ALLOW_CLIENT_ONBOARDING="${ALLOW_CLIENT_ONBOARDING:-false}"
export DGII_SOFTWARE_NAME="$SOFT_NAME"
export DGII_SOFTWARE_VERSION="$SOFT_VER"
export DGII_SOFTWARE_TIPO="$SOFT_TIPO"

if grep -q 'cambia_esto' .env 2>/dev/null; then
    error "Hay placeholders 'cambia_esto' en .env — complete secretos reales"
fi

DB_PASS_LEN=${#DB_PASSWORD}
[[ "$DB_PASS_LEN" -ge 32 ]] || error "DB_PASSWORD debe tener ≥32 caracteres (actual: $DB_PASS_LEN)"

GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "sin-git")
GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "sin-rama")
log "Rama=$GIT_BRANCH Commit=$GIT_COMMIT Ambiente=$AMBIENTE Software=$SOFT_NAME/$SOFT_VER"

STACK="${STACK_NAME:-ecf}"

# ── FASE 2: build imagen con XSD ─────────────────────────────────────────────
log "=============================================="
log "FASE 2: Build imagen (incluye xsd/)"
log "=============================================="

docker build -t ecf_api:latest -f Dockerfile.api .

# Verificar que la imagen trae los XSD (gate duro)
log "Verificando XSD dentro de la imagen..."
MISSING_IN_IMG=0
for x in ECF-31.xsd ECF-32.xsd RFCE-32.xsd ACECF.xsd; do
    if ! docker run --rm --entrypoint test ecf_api:latest -f "/app/xsd/$x"; then
        warn "Imagen sin /app/xsd/$x"
        MISSING_IN_IMG=$((MISSING_IN_IMG + 1))
    fi
done
[[ "$MISSING_IN_IMG" -eq 0 ]] || error "Build incompleto: faltan XSD en la imagen. Abortando deploy."

# Smoke import Python (cert path)
log "Smoke import ecf_core + api_gateway..."
docker run --rm \
    -e ECF_AMBIENTE="$AMBIENTE" \
    -e SKIP_XSD_VALIDATION=false \
    -e VAULT_MASTER_KEY="${VAULT_MASTER_KEY}" \
    --entrypoint python ecf_api:latest -c \
    'from ecf_core.platform_config import software_identity; from ecf_core import dgii_client; s=software_identity(); assert s["nombre"]=="RENECF"; assert s["version"]=="'"$SOFT_VER"'"; print("OK", s)'

# ── FASE 3: red + stack ─────────────────────────────────────────────────────
log "=============================================="
log "FASE 3: Swarm stack deploy"
log "=============================================="

docker network create -d overlay RenaceNet 2>/dev/null || true
touch traefik_dynamic.yml 2>/dev/null || true

docker stack deploy -c docker-compose.prod.yml "$STACK"

# ── FASE 4: force-update app services (imagen + env) ─────────────────────────
log "=============================================="
log "FASE 4: Force-update api / worker / scheduler"
log "=============================================="

for svc in api worker scheduler; do
    log "Updating ${STACK}_${svc}..."
    docker service update --force --image ecf_api:latest \
        --env-add "ECF_AMBIENTE=${AMBIENTE}" \
        --env-add "SKIP_XSD_VALIDATION=false" \
        --env-add "DGII_SOFTWARE_NAME=${SOFT_NAME}" \
        --env-add "DGII_SOFTWARE_VERSION=${SOFT_VER}" \
        --env-add "DGII_SOFTWARE_TIPO=${SOFT_TIPO}" \
        --env-add "PLATFORM_OPERATOR_RNC=${PLATFORM_OPERATOR_RNC}" \
        "${STACK}_${svc}" >/dev/null
done

log "Esperando réplicas (hasta 120s)..."
READY=0
for i in $(seq 1 60); do
    API=$(docker service ls --filter "name=${STACK}_api" --format '{{.Replicas}}' 2>/dev/null || echo "0/1")
    WRK=$(docker service ls --filter "name=${STACK}_worker" --format '{{.Replicas}}' 2>/dev/null || echo "0/1")
    SCH=$(docker service ls --filter "name=${STACK}_scheduler" --format '{{.Replicas}}' 2>/dev/null || echo "0/1")
    if [[ "$API" == "1/1" && "$WRK" == "1/1" && "$SCH" == "1/1" ]]; then
        READY=1
        break
    fi
    sleep 2
done

echo ""
docker service ls
echo ""

if [[ "$READY" -ne 1 ]]; then
    error "Servicios no alcanzaron 1/1. Ver: docker service ps ${STACK}_api --no-trunc; docker service logs ${STACK}_api --tail 40"
fi

# ── FASE 5: health + checklist certificación ────────────────────────────────
log "=============================================="
log "FASE 5: Health + checklist CerteCF"
log "=============================================="

HEALTH_OK=0
for i in $(seq 1 20); do
    if curl -sf "https://${ECF_DOMAIN}/health" >/dev/null 2>&1 \
        || curl -sf "https://${ECF_DOMAIN}/v1/health" >/dev/null 2>&1 \
        || curl -sf "http://127.0.0.1:8000/health" >/dev/null 2>&1; then
        HEALTH_OK=1
        break
    fi
    sleep 2
done
[[ "$HEALTH_OK" -eq 1 ]] || error "Health check falló tras deploy. No marque certificación como lista."

BODY=$(curl -sf "https://${ECF_DOMAIN}/health" 2>/dev/null \
    || curl -sf "http://127.0.0.1:8000/health" 2>/dev/null \
    || echo '{}')
info "Health: $BODY"

check_ok() {
    local desc="$1" ok="$2"
    if [[ "$ok" == "1" ]]; then
        echo -e "  ${GREEN}[OK]${NC} $desc"
    else
        echo -e "  ${RED}[FAIL]${NC} $desc"
    fi
}

echo ""
log "CHECKLIST CERTIFICACIÓN DGII"
check_ok "Ambiente Swarm = $AMBIENTE" 1
check_ok "Software RENECF ${SOFT_VER} (PROPIO)" 1
check_ok "XSD embebidos en imagen" 1
check_ok "SKIP_XSD_VALIDATION=false" 1
check_ok "API/worker/scheduler 1/1" 1
check_ok "HTTPS/health responde" "$HEALTH_OK"
if [[ "$PSFE_OK" -eq 1 ]]; then
    check_ok "PSFE en .env" 1
else
    echo -e "  ${YELLOW}[!!]${NC} PSFE: verificar en portal → Plataforma → Probar CerteCF"
fi
check_ok "Operador RNC ${PLATFORM_OPERATOR_RNC}" 1

echo ""
log "DEPLOY CERTIFICACIÓN COMPLETADO"
info "Commit:    $GIT_COMMIT"
info "Ambiente:  $AMBIENTE → CerteCF (ecf.dgii.gov.do/CerteCF)"
info "Portal:    https://${ECF_DOMAIN}/portal/"
info "Siguiente: Probar CerteCF → Set de Pruebas Odoo → Actualizar Estado hasta Aprobado"
echo ""
