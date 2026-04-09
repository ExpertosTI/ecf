# Schemas XSD DGII

Estos archivos son los XSD oficiales descargados directamente del portal DGII.

Para actualizar a la versión más reciente:
```bash
bash scripts/actualizar_xsd.sh
```

## Archivos presentes

| Archivo | Tipo e-CF | Descripción |
|---|---|---|
| `ECF-31.xsd` | 31 | Factura de Crédito Fiscal Electrónica |
| `ECF-32.xsd` | 32 | Factura de Consumo Electrónica |
| `ECF-33.xsd` | 33 | Nota de Débito Electrónica |
| `ECF-34.xsd` | 34 | Nota de Crédito Electrónica |
| `ECF-41.xsd` | 41 | Comprobante de Compras Electrónico |
| `ECF-43.xsd` | 43 | Gastos Menores Electrónico |
| `ECF-44.xsd` | 44 | Regímenes Especiales Electrónico |
| `ECF-45.xsd` | 45 | Gubernamental Electrónico |
| `ECF-46.xsd` | 46 | Comprobante para Exportaciones |
| `ECF-47.xsd` | 47 | Comprobante para Pagos al Exterior |
| `RFCE-32.xsd` | — | Resumen Factura Consumo Electrónico |
| `ARECF.xsd` | — | Acuse de Recibo |
| `ANECF.xsd` | — | Anulación de e-CF |
| `ACECF.xsd` | — | Aprobación Comercial |
| `Semilla.xsd` | — | Semilla de autenticación |

## Fuente

Portal DGII — Documentación Técnica (XSD):
https://dgii.gov.do/cicloContribuyente/facturacion/comprobantesFiscalesElectronicosE-CF/Paginas/documentacionSobreE-CF.aspx

## Variable de entorno

En producción (`eCF`) la validación XSD es **obligatoria** y siempre activa.

Para entornos de prueba/certificación durante homologación DGII,
se puede omitir temporalmente con:
```
SKIP_XSD_VALIDATION=true
```
**NUNCA activar en producción.**
