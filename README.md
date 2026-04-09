# SaaS ECF DGII — Documentación Completa

## Arquitectura general

```
Tenants (Odoo 18 + módulo ecf_connector)
        │  POST /v1/ecf/emitir  (X-API-Key por tenant)
        ▼
API Gateway (FastAPI)
  ├─ Autenticación por API key
  ├─ Validación del payload
  ├─ Asignación atómica de NCF
  └─ Encolado en Redis
        │
        ▼
Queue Worker (asyncio)
  ├─ Genera XML según esquema DGII
  ├─ Valida contra XSD oficial
  ├─ Firma con .p12 del tenant (AES-GCM vault)
  ├─ Envía a DGII con mTLS (cert PSFE)
  ├─ Reintentos con backoff exponencial
  └─ Callback a Odoo (HMAC-SHA256)
        │
        ▼
API DGII (ecf.dgii.gov.do)
```

## Estructura del proyecto

```
saas_ecf/
├── db/
│   └── 001_schema.sql          # Schema PostgreSQL multitenant
├── ecf_core/
│   ├── ecf_core_service.py     # Generación XML, firma, CUFE, validación XSD
│   ├── cert_vault.py           # Almacenamiento cifrado de .p12 (AES-256-GCM)
│   ├── dgii_client.py          # Cliente mTLS para la API DGII
│   ├── queue_worker.py         # Worker Redis con reintentos y callbacks
│   ├── scheduler.py            # Jobs periódicos (alertas cert, reset contadores)
│   └── worker_main.py          # Entry point del worker
├── api_gateway/
│   ├── main.py                 # API FastAPI — endpoints para Odoo
│   └── admin.py                # Admin API — gestión de tenants y certs
├── scripts/
│   ├── crear_tenant.py         # CLI para crear tenants
│   └── subir_certificado.py    # CLI para subir certificados .p12
├── odoo_module/
│   └── ecf_connector/          # Módulo Odoo 18 instalable
│       ├── __manifest__.py
│       ├── models/models.py
│       ├── views/account_move_views.xml
│       └── controllers/webhook.py
├── tests/
│   ├── test_homologacion.py    # Suite completa de pruebas DGII
│   └── test_api.py             # Tests API Gateway + Admin
├── landing/
│   └── index.html              # Landing page
├── docker-compose.yml
├── deploy.sh                   # Script de despliegue con backup automático
├── requirements.txt
└── .env.example
```


## Setup rápido

### 1. Preparar el entorno

```bash
cp .env.example .env

# Generar VAULT_MASTER_KEY (obligatorio — guárdala en lugar seguro)
python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
# Pega el resultado en VAULT_MASTER_KEY del .env

# Llenar las demás variables (ver sección de certificados más abajo)
nano .env
```

### 2. Certificados

**Para desarrollo / certificación:**
La DGII provee certificados de prueba durante la homologación.
Solicitarlos en: https://dgii.gov.do/ecf/

**Para producción:**
- Obtener certificado digital ante la Cámara de Comercio
- El certificado .p12 de cada tenant se sube desde el portal admin
- Tu certificado PSFE (para mTLS con la DGII) lo recibes al ser certificado

```bash
# Convertir cert a base64 para .env
base64 -w0 mi_psfe_cert.pem > /tmp/cert_b64.txt
base64 -w0 mi_psfe_key.pem  > /tmp/key_b64.txt
base64 -w0 dgii_ca.pem       > /tmp/ca_b64.txt
```

### 3. Levantar servicios

```bash
docker-compose up -d

# Verificar que todo está OK
docker-compose ps
curl http://localhost:8000/health
```

### 4. Crear primer tenant

```bash
# Generar ADMIN_API_KEY y agregarla al .env
python3 -c "import secrets; print(secrets.token_hex(32))"
# Pegar en ADMIN_API_KEY del .env

# Crear tenant con el script CLI
python scripts/crear_tenant.py \
  --rnc 130000001 \
  --razon-social "Mi Empresa SRL" \
  --email admin@miempresa.do \
  --plan basico \
  --ambiente certificacion

# El script devuelve API Key y Webhook Secret — GUARDAR en lugar seguro

# Subir certificado .p12 del tenant
python scripts/subir_certificado.py \
  --tenant-id <UUID-del-tenant> \
  --cert /ruta/a/certificado.p12
```

### 5. Instalar módulo en Odoo

```bash
# Copiar el módulo al addons path de Odoo
cp -r odoo_module/ecf_connector /path/to/odoo/addons/

# Desde Odoo: Ajustes → Activar modo desarrollador → Apps → Actualizar lista → Instalar ECF Connector

# Configurar: Ajustes → e-CF DGII → llenar URL, API Key, Webhook Secret
```


## Proceso de homologación DGII

La DGII exige pasar por estas fases antes de operar en producción:

### Fase 1 — Registro como PSFE

Documentos requeridos:
- Formulario de solicitud PSFE (descarga en dgii.gov.do)
- Copia del RNC vigente
- Documentos legales de la empresa (acta constitutiva, etc.)
- Descripción técnica del sistema (usar este README como base)
- Plan de contingencia documentado

Contacto: dgii@dgii.gov.do / Área de Tecnología DGII

### Fase 2 — Ambiente de certificación

La DGII habilitará acceso a:
- `ecf-cert.dgii.gov.do` — API de certificación
- Certificados de prueba
- Credenciales de test

### Fase 3 — Casos de prueba requeridos

Ejecutar la suite completa:

```bash
# Configurar variables para certificación
export ECF_AMBIENTE=certificacion
export PSFE_CERT_B64=$(base64 -w0 cert_prueba.pem)
export PSFE_KEY_B64=$(base64 -w0 key_prueba.pem)
export DGII_CA_B64=$(base64 -w0 dgii_ca_cert.pem)

pytest tests/test_homologacion.py -v --tb=short
```

Casos obligatorios que la DGII evalúa:
1. ✅ e-CF tipo 31 (Crédito Fiscal) con RNC válido
2. ✅ e-CF tipo 32 (Consumo) sin RNC
3. ✅ e-CF tipo 33 (Nota de Débito) con NCF referencia
4. ✅ e-CF tipo 34 (Nota de Crédito) con NCF referencia
5. ✅ ITBIS exento (tasa 0%)
6. ✅ CUFE correcto (SHA-384, 96 chars hex)
7. ✅ Firma digital XML válida (RSA-SHA256)
8. ✅ NCF con formato correcto y secuencia sin saltos
9. ✅ Factura en moneda extranjera (USD con tipo de cambio)
10. ✅ Manejo de contingencia (timeout → reintento → DLQ)

### Fase 4 — SLA requerido por DGII

Antes de ir a producción documentar y demostrar:
- Disponibilidad 99.5% (monitorear con UptimeRobot u similar)
- Tiempo de respuesta < 5 segundos (p99)
- Plan de contingencia para cuando la DGII no responde
- Retención de XML firmados por 10 años
- Backup diario de la base de datos


## Seguridad — checklist antes de producción

- [ ] VAULT_MASTER_KEY generada con `os.urandom(32)`, guardada fuera del repo
- [ ] .env no commiteado (verificar .gitignore)
- [ ] Certificados .p12 de tenants nunca en logs
- [ ] HTTPS obligatorio (Nginx con TLS 1.2+)
- [ ] mTLS habilitado en todas las llamadas a la DGII
- [ ] Webhook callbacks verificados con HMAC-SHA256
- [ ] Backup automático de pgdata + VAULT_MASTER_KEY en lugares separados
- [ ] Rotación de API keys implementada
- [ ] Alertas de vencimiento de certificados (30 días de anticipación)
- [ ] Rate limiting habilitado en el API Gateway
- [ ] Logs de auditoría completos (system_audit_log)


## Tipos de e-CF soportados

| Código | Nombre | Requiere RNC comprador | Requiere NCF ref. |
|--------|--------|------------------------|-------------------|
| 31 | Crédito Fiscal | Sí (RNC) | No |
| 32 | Consumo | Opcional | No |
| 33 | Nota de Débito | Sí | Sí |
| 34 | Nota de Crédito | Sí | Sí |
| 41 | Compras | Sí | No |
| 43 | Gastos Menores | Opcional | No |
| 44 | Regímenes Especiales | Sí | No |
| 45 | Gubernamental | Sí | No |
| 46 | Exportaciones | Sí | No |
| 47 | Pagos al Exterior | Sí | No |


## Admin API

La Admin API permite gestionar tenants, certificados y DLQ. Requiere header `Authorization: Bearer <ADMIN_API_KEY>`.

### Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| `POST` | `/v1/admin/tenants` | Crear tenant (genera API key + webhook secret) |
| `GET` | `/v1/admin/tenants` | Listar tenants |
| `GET` | `/v1/admin/tenants/{id}` | Detalle de un tenant |
| `PATCH` | `/v1/admin/tenants/{id}` | Actualizar tenant |
| `DELETE` | `/v1/admin/tenants/{id}` | Desactivar tenant |
| `POST` | `/v1/admin/tenants/{id}/rotate-key` | Rotar API key |
| `POST` | `/v1/admin/tenants/{id}/certs` | Subir certificado .p12 |
| `GET` | `/v1/admin/tenants/{id}/certs` | Listar certificados |
| `POST` | `/v1/admin/tenants/{id}/ncf-sequences` | Crear secuencia NCF |
| `GET` | `/v1/admin/tenants/{id}/ncf-sequences` | Listar secuencias NCF |
| `GET` | `/v1/admin/dlq` | Ver Dead Letter Queue |
| `DELETE` | `/v1/admin/dlq/{index}` | Eliminar item de DLQ |
| `POST` | `/v1/admin/dlq/{index}/retry` | Reintentar item de DLQ |
| `GET` | `/v1/admin/stats` | Estadísticas del sistema |

### Ejemplo: crear tenant via cURL

```bash
curl -X POST http://localhost:8000/v1/admin/tenants \
  -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "rnc": "130000001",
    "razon_social": "Mi Empresa SRL",
    "email": "admin@miempresa.do",
    "plan": "basico",
    "ambiente": "certificacion"
  }'
```


## Tests

```bash
# Tests de API Gateway
pytest tests/test_api.py -v

# Tests de homologación (requiere ambiente de certificación)
pytest tests/test_homologacion.py -v
```


## Soporte

Para dudas sobre homologación: consultar el Manual Técnico e-CF en dgii.gov.do
