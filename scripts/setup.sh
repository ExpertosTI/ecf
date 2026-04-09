#!/usr/bin/env bash
# setup.sh — Configuración inicial de SaaS ECF DGII
# Genera secretos, crea .env y descarga XSD en un solo comando.
# Uso: bash setup.sh

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()      { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()     { echo -e "${RED}[ERR ]${NC} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

echo ""
echo "======================================================="
echo "  SAAS ECF DGII — Configuración Inicial"
echo "======================================================="
echo ""

# Verificar dependencias
command -v python3 >/dev/null 2>&1 || die "python3 es requerido"
command -v curl    >/dev/null 2>&1 || die "curl es requerido"
command -v docker  >/dev/null 2>&1 || die "docker es requerido"

# ───────────────────────────────────────────────
# 1. Generar .env si no existe
# ───────────────────────────────────────────────
ENV_FILE="$ROOT_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
    warn ".env ya existe — no se sobreescribirá. Editalo manualmente si es necesario."
else
    info "Generando secretos..."

    VAULT_KEY=$(python3 -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())")
    DB_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
    REDIS_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
    ADMIN_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    METRICS_KEY=$(python3 -c "import secrets; print(secrets.token_hex(16))")

    cat > "$ENV_FILE" <<EOF
# SaaS ECF DGII — Variables de entorno generadas por setup.sh
# COMPLETAR: ECF_DOMAIN, ACME_EMAIL, PSFE_CERT_B64, PSFE_KEY_B64, DGII_CA_B64

# --- Dominio ---
ECF_DOMAIN=ecf.renace.tech
PORTAL_DOMAIN=portal.ecf.renace.tech
ACME_EMAIL=admin@renace.tech

# --- Base de datos ---
DB_PASSWORD=${DB_PASS}

# --- Redis ---
REDIS_PASSWORD=${REDIS_PASS}

# --- Vault: llave maestra AES-256 ---
VAULT_MASTER_KEY=${VAULT_KEY}

# --- Certificados DGII (completar con los archivos reales de la DGII) ---
# Convertir: base64 -w0 psfe_cert.pem > /tmp/cert.b64
PSFE_CERT_B64=
PSFE_KEY_B64=
DGII_CA_B64=

# --- Admin API ---
ADMIN_API_KEY=${ADMIN_KEY}

# --- Métricas ---
METRICS_API_KEY=${METRICS_KEY}

# --- CORS ---
ALLOWED_ORIGINS=https://ecf.renace.tech,https://portal.ecf.renace.tech

# --- Rate limiting ---
RATE_LIMIT_MAX=60
RATE_LIMIT_WINDOW=60

# --- SMTP para alertas ---
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
ALERT_FROM_EMAIL=no-reply@renace.tech

# --- XSD: poner TRUE solo en ambiente TesteCF/CerteCF durante homologación ---
SKIP_XSD_VALIDATION=false
EOF

    ok ".env generado con secretos aleatorios"
    echo ""
    echo -e "  ${YELLOW}IMPORTANTE:${NC} Completar los siguientes campos en .env:"
    echo "    • PSFE_CERT_B64   — certificado de cliente DGII"
    echo "    • PSFE_KEY_B64    — llave privada del cert PSFE"
    echo "    • DGII_CA_B64     — CA raíz de la DGII"
    echo "    • SMTP_*          — servidor de correo para alertas"
    echo ""
fi

# ───────────────────────────────────────────────
# 2. Descargar XSD oficiales de la DGII
# ───────────────────────────────────────────────
XSD_DIR="$ROOT_DIR/xsd"

# Contar cuántos XSD de e-CF ya existen
XSD_COUNT=$(find "$XSD_DIR" -name "ECF-*.xsd" 2>/dev/null | wc -l | tr -d ' ')

if [[ "$XSD_COUNT" -ge 10 ]]; then
    ok "XSD ya descargados ($XSD_COUNT archivos). Para actualizar: bash scripts/actualizar_xsd.sh"
else
    info "Descargando XSD oficiales de la DGII ($XSD_COUNT/10 presentes)..."
    bash "$SCRIPT_DIR/actualizar_xsd.sh" "$XSD_DIR" && ok "XSD descargados correctamente"
fi

# ───────────────────────────────────────────────
# 3. Verificar estructura Docker
# ───────────────────────────────────────────────
info "Verificando configuración Docker..."

cd "$ROOT_DIR"

if docker compose config --quiet 2>/dev/null; then
    ok "docker-compose.yml válido"
else
    warn "Error en docker-compose.yml — revisar antes de deploy"
fi

# ───────────────────────────────────────────────
# 4. Resumen
# ───────────────────────────────────────────────
echo ""
echo "======================================================="
echo "  PRÓXIMOS PASOS"
echo "======================================================="
echo ""
echo "  1. Completar .env con certificados DGII (PSFE_CERT_B64, etc.)"
echo ""
echo "  2. Subir servidor y lanzar:"
echo "       docker compose up -d"
echo ""
echo "  3. Crear el primer tenant:"
echo "       python scripts/crear_tenant.py \\"
echo "         --rnc TU_RNC \\"
echo "         --razon-social 'Mi Empresa SRL' \\"
echo "         --email admin@empresa.do \\"
echo "         --ambiente certificacion"
echo ""
echo "  4. Subir certificado .p12 del tenant:"
echo "       python scripts/subir_certificado.py --tenant-id UUID --cert mi.p12"
echo ""
echo "  5. Ejecutar homologación DGII (ambiente CerteCF)"
echo "       Referencia: https://dgii.gov.do/.../documentacionSobreE-CF.aspx"
echo ""
echo "  Para actualizar los XSD en el futuro:"
echo "       bash scripts/actualizar_xsd.sh"
echo ""
