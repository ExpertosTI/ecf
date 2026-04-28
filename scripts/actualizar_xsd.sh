#!/usr/bin/env bash
# actualizar_xsd.sh — Descarga los XSD oficiales de la DGII
# Uso: bash scripts/actualizar_xsd.sh [directorio_destino]
# El portal DGII requiere un User-Agent de navegador para servir los archivos.

set -euo pipefail

DEST="${1:-$(dirname "$0")/../xsd}"
mkdir -p "$DEST"

BASE="https://dgii.gov.do/cicloContribuyente/facturacion/comprobantesFiscalesElectronicosE-CF/Documentacin%20sobre%20eCF/Documentaci%C3%B3n%20T%C3%A9cnica%20(XSD)"
UA="Mozilla/5.0 (compatible; renace_ecf/1.0)"

echo "=== Descargando XSD oficiales DGII → $DEST ==="

declare -A FILES=(
  ["ECF-31.xsd"]="e-CF%2031%20v.1.0.xsd"
  ["ECF-32.xsd"]="e-CF%2032%20v.1.0.xsd"
  ["ECF-33.xsd"]="e-CF%2033%20v.1.0.xsd"
  ["ECF-34.xsd"]="e-CF%2034%20v.1.0.xsd"
  ["ECF-41.xsd"]="e-CF%2041%20v.1.0.xsd"
  ["ECF-43.xsd"]="e-CF%2043%20v.1.0.xsd"
  ["ECF-44.xsd"]="e-CF%2044%20v.1.0.xsd"
  ["ECF-45.xsd"]="e-CF%2045%20v.1.0.xsd"
  ["ECF-46.xsd"]="e-CF%2046%20v.1.0.xsd"
  ["ECF-47.xsd"]="e-CF%2047%20v.1.0.xsd"
  ["RFCE-32.xsd"]="RFCE%2032%20v.1.0.xsd"
  ["ARECF.xsd"]="ARECF%20v1.0.xsd"
  ["ANECF.xsd"]="ANECF%20v.1.0.xsd"
  ["ACECF.xsd"]="ACECF%20v.1.0.xsd"
  ["Semilla.xsd"]="Semilla%20v.1.0.xsd"
)

OK=0
FAIL=0

for local_name in "${!FILES[@]}"; do
  remote_name="${FILES[$local_name]}"
  url="${BASE}/${remote_name}"
  outfile="${DEST}/${local_name}"

  if curl -sL --fail -A "$UA" "$url" -o "$outfile"; then
    # Verificar que es XML y no HTML de error
    if head -1 "$outfile" | grep -q '<?xml'; then
      size=$(wc -c < "$outfile")
      echo "  ✓ $local_name (${size} bytes)"
      OK=$((OK + 1))
    else
      echo "  ✗ $local_name — respuesta no es XSD válido (¿cambió la URL?)"
      rm -f "$outfile"
      FAIL=$((FAIL + 1))
    fi
  else
    echo "  ✗ $local_name — error HTTP"
    FAIL=$((FAIL + 1))
  fi
done

echo ""
echo "=== Resultado: $OK descargados, $FAIL fallidos ==="

if [[ $FAIL -gt 0 ]]; then
  echo "AVISO: Algunos XSD no se descargaron. Verifique manualmente:"
  echo "  $BASE"
  exit 1
fi

echo "Todos los XSD están actualizados. La validación local estará activa."
