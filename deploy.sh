#!/bin/bash
# deploy.sh — Protocolo Renace para e-CF (Servidor REY)
set -e

STACK_NAME="ecf"
PROJECT_DIR="/opt/ecf"
SERVICE_NAME="${STACK_NAME}_api"

# 1. Validaciones
if [ ! -f ".env" ]; then
    echo "❌ Error: Archivo .env no encontrado. Copiar de .env.example."
    exit 1
fi

# 2. Migración Nginx -> Traefik (Solo si Nginx está activo)
if systemctl is-active --quiet nginx; then
    echo "⚠️  Nginx detectado. Iniciando migración a Traefik..."
    systemctl stop nginx
    systemctl disable nginx
    echo "✅ Nginx detenido y deshabilitado."
fi

# 3. Preparar Red Overlay
docker network ls | grep RenaceNet > /dev/null || \
    docker network create --driver overlay --attachable RenaceNet

# 4. Build de Imágenes
echo "🏗️  Construyendo imágenes (Zero-Build Protocol)..."
if docker compose version >/dev/null 2>&1; then
    docker compose build api
elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose build api
else
    echo "❌ Error: Ni 'docker compose' ni 'docker-compose' están instalados."
    exit 1
fi

# 5. Despliegue de Stack
echo "🚀 Desplegando stack: $STACK_NAME"
set -a; source .env; set +a
docker stack deploy -c docker-compose.yml $STACK_NAME

# 6. Sincronización Forzada
echo "🔄 Sincronizando servicios..."
docker service update --force ${STACK_NAME}_traefik
docker service update --force ${STACK_NAME}_api
docker service update --force ${STACK_NAME}_worker

# 7. Cleanup
echo "🧹 Limpiando imágenes antiguas..."
docker image prune -f

echo "=========================================="
echo "✅ DEPLOY COMPLETADO EXITOSAMENTE"
echo "=========================================="
echo "Dominio: ${ECF_DOMAIN:-ecf.renace.tech}"
echo "Check: docker service logs -f ${STACK_NAME}_api"

