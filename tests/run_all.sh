#!/bin/bash
# 🚀 Renace e-CF — Test Runner Automático
# Detecta el .env y ejecuta las pruebas smoke.

# Colores
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo "🔍 Buscando configuración..."

# Intentar cargar .env desde ecf/ o desde el root
if [ -f "ecf/.env" ]; then
    export $(grep -v '^#' ecf/.env | xargs)
    echo -e "${GREEN}✅ .env encontrado en ecf/.env${NC}"
elif [ -f ".env" ]; then
    # Verificar si es el .env de ECF o de otra app
    if grep -q "ADMIN_API_KEY" .env; then
        export $(grep -v '^#' .env | xargs)
        echo -e "${GREEN}✅ .env encontrado en el root${NC}"
    else
        echo -e "${RED}❌ .env del root no parece ser de ECF.${NC}"
    fi
else
    echo -e "${RED}❌ No se encontró archivo .env.${NC}"
fi

# Ejecutar el test
python3 ecf/tests/smoke_test_ecf.py
