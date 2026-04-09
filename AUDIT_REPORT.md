# AUDITORÍA FINAL — SaaS ECF DGII

**Fecha:** Auditoría pre-producción  
**Alcance:** Todos los archivos del workspace  
**Archivos auditados:** 35+  
**Nota:** Este reporte lista issues **PENDIENTES** que aún existen en el código después de las 5 sesiones previas de correcciones (66 fixes aplicados).

---

## 🔴 CRITICAL — Rompen ejecución o comprometen seguridad

### C1. Anulación en worker callback siempre dice "anulado" aunque DGII rechazó

**Archivo:** `ecf_core/queue_worker.py` línea ~250  
**Problema:** En `_procesar_anulacion`, el callback a Odoo siempre envía `"anulado"` como estado, incluso cuando `estado_final` es `"anulacion_fallida"`:

```python
# Callback a Odoo
if tenant.get("odoo_webhook_url"):
    ecf_data = await self._get_ecf(schema, ecf_id)
    await self._callback_odoo(tenant, ecf_data, respuesta, "anulado")  # ← HARDCODED
```

**Impacto:** Odoo marca la factura como "anulado" cuando DGII realmente la rechazó. El usuario cree que la anulación fue exitosa, pero la DGII aún la tiene como vigente.

**Fix:**
```python
    await self._callback_odoo(tenant, ecf_data, respuesta, estado_final)
```

---

### C2. Tests rotos — `_setup_tenant_auth` no tiene los campos que `get_tenant` requiere

**Archivo:** `tests/test_api.py` líneas ~140-150  
**Problema:** `_setup_tenant_auth` crea un `FakeRecord` con solo 7 campos, pero `get_tenant` en `main.py` consulta 11 columnas y accede a `tenant["estado"]`, `tenant["ecf_emitidos_mes"]`, `tenant["cert_vencimiento"]`, etc. Como `FakeRecord` es un `dict`, acceder a un key inexistente lanza `KeyError` → HTTP 500.

```python
# _setup_tenant_auth solo pone:
make_record(
    id=TENANT_ID, rnc="130000001", razon_social="Empresa Test SRL",
    schema_name=TENANT_SCHEMA, ambiente="certificacion",
    activo=True, max_ecf_mensual=1000,
)
# FALTA: estado, ecf_emitidos_mes, cert_vencimiento, odoo_webhook_url, odoo_webhook_secret
```

**Impacto:** **NINGÚN test que use autenticación de tenant puede pasar.** Todos fallan con 500 en vez del resultado esperado.

**Fix:**
```python
fake_pool.conn.fetchrow.return_value = make_record(
    id=TENANT_ID,
    rnc="130000001",
    razon_social="Empresa Test SRL",
    schema_name=TENANT_SCHEMA,
    ambiente="certificacion",
    estado="activo",
    ecf_emitidos_mes=0,
    max_ecf_mensual=1000,
    cert_vencimiento=None,
    odoo_webhook_url=None,
    odoo_webhook_secret=None,
)
```

---

### C3. Odoo `set_values` valida longitud 64 chars → rechaza toda API key válida

**Archivo:** `odoo_module/ecf_connector/models/models.py` línea ~91  
**Problema:** La validación exige 64 caracteres, pero las API keys generadas por `admin.py` tienen formato `sk_cert_` + 48 hex = **56 caracteres**. El hash SHA-256 es 64 hex, pero los usuarios configuran la key **raw**, no el hash.

```python
def set_values(self):
    super().set_values()
    api_key = self.company_id.ecf_api_key
    if api_key and len(api_key) != 64:
        raise ValidationError(_('La API Key debe tener exactamente 64 caracteres'))
```

**Impacto:** **Ningún usuario puede guardar la configuración del módulo Odoo.** El módulo es inutilizable.

**Fix:** Eliminar la validación de longitud fija o validarla como `>= 20`:
```python
def set_values(self):
    super().set_values()
    api_key = self.company_id.ecf_api_key
    if api_key and len(api_key) < 20:
        raise ValidationError(_('La API Key parece inválida (muy corta)'))
```

---

### C4. Wizard anulación: mensaje chatter dice "anulado" cuando estado es "anulación pendiente"

**Archivo:** `odoo_module/ecf_connector/wizard/ecf_anular_wizard.py` línea ~76  
**Problema:** El `message_post` dice "e-CF anulado" inmediatamente, pero el estado es `anulacion_pendiente` (el resultado real viene por webhook después).

```python
move.sudo().write({'ecf_estado': 'anulacion_pendiente'})
# ...
move.message_post(
    body=_("e-CF anulado. NCF: %s. Motivo: %s", ...),  # ← DICE "anulado"
```

**Impacto:** El usuario lee "anulado" en el chatter pero el comprobante sigue vigente ante la DGII hasta que el webhook confirme.

**Fix:**
```python
move.message_post(
    body=_(
        "Solicitud de anulación enviada al SaaS. NCF: %s. Motivo: %s. "
        "El estado se actualizará cuando la DGII responda.",
        move.ecf_ncf,
        dict(self._fields['motivo'].selection).get(self.motivo, self.motivo),
    ),
)
```

---

## 🟠 IMPORTANT — Bugs de lógica, rendimiento o calidad

### I1. Admin DLQ/Stats endpoints: nueva conexión Redis por request

**Archivo:** `api_gateway/admin.py` líneas ~530, 555, 580, 620  
**Problema:** Cada llamada a `/dlq`, `/dlq/{index}`, `/dlq/{index}/retry` y `/stats` crea una nueva conexión Redis con `aioredis.from_url(...)` + `aclose()`. En producción con múltiples administradores, esto genera churn de conexiones.

**Fix:** Pasar la referencia de Redis al igual que se hace con DB:
```python
_redis_ref = None

def set_redis_ref(r):
    global _redis_ref
    _redis_ref = r
```
Y en `main.py`, durante lifespan:
```python
admin.set_redis_ref(app.state.redis)
```

---

### I2. Operaciones DLQ no son atómicas (race condition)

**Archivo:** `api_gateway/admin.py` líneas ~555-600  
**Problema:** `remove_dlq_message` y `retry_dlq_message` hacen `lrange` → `lset` → `lrem` sin transacción Redis. Si dos admins operan la DLQ simultáneamente, se puede eliminar/reintentar el mensaje equivocado.

**Fix:** Usar un script Lua atómico o `MULTI/EXEC`:
```python
# Ejemplo con Lua
lua_script = """
local msg = redis.call('LINDEX', KEYS[1], ARGV[1])
if not msg then return nil end
redis.call('LSET', KEYS[1], ARGV[1], '__REMOVED__')
redis.call('LREM', KEYS[1], 0, '__REMOVED__')
return msg
"""
```

---

### I3. Scheduler: SMTP bloqueante dentro de async event loop

**Archivo:** `ecf_core/scheduler.py` líneas ~70-85  
**Problema:** `smtplib.SMTP` es I/O síncrono. Ejecutarlo dentro de una función async bloquea el event loop durante toda la operación SMTP (DNS, handshake TLS, auth, envío). Si hay muchos tenants con certs por vencer, el scheduler se congela.

**Fix:** Usar `asyncio.to_thread()`:
```python
await asyncio.to_thread(_send_email_sync, smtp_host, smtp_port, smtp_user, smtp_pass, msg)
```
O migrar a `aiosmtplib`.

---

### I4. Schema `api_key_hash` nunca usa bcrypt — columna muerta

**Archivo:** `db/001_schema.sql` línea 22, `api_gateway/admin.py` línea ~194  
**Problema:** La columna `api_key_hash VARCHAR(128)` tiene comentario `-- bcrypt del api_key`, pero el código almacena el **mismo SHA-256 hash** en ambas columnas (`api_key` y `api_key_hash`). Ningún código consulta `api_key_hash`.

```sql
api_key             VARCHAR(64) NOT NULL UNIQUE,     -- SHA-256 hex
api_key_hash        VARCHAR(128) NOT NULL,           -- bcrypt del api_key  ← NUNCA IMPLEMENTADO
```

```python
api_key_hash,       # ← SHA-256
api_key_hash,       # api_key_hash column (same as api_key for now)
```

**Impacto:** Columna desperdiciada + comentario engañoso en el schema. Si alguien futuro confía en que `api_key_hash` es bcrypt, introducirá bugs.

**Fix:** Eliminar la columna `api_key_hash` o implementar bcrypt. Si se elimina, limpiar la referencia en admin.py INSERT.

---

### I5. `VAULT_MASTER_KEY` en tests con valor inválido

**Archivo:** `tests/test_api.py` línea 24  
**Problema:** `os.environ.setdefault("VAULT_MASTER_KEY", "a" * 64)` — al decodificar base64, `"a" * 64` produce 48 bytes, no los 32 que CertVault espera. Si algún test inicializa CertVault, crasheará.

**Fix:**
```python
import base64, os as _os
os.environ.setdefault("VAULT_MASTER_KEY", base64.b64encode(_os.urandom(32)).decode())
```

---

### I6. `FakeRedis` no implementa `set`, `rpush` usados en `emitir_ecf`

**Archivo:** `tests/test_api.py` clase `FakeRedis`  
**Problema:** `FakeRedis` tiene `lpush`, `zadd`, `incr`, `expire`, `get`, `setex`, `aclose`, `ttl` — pero le faltan `set` (usado para idempotencia) y `rpush` (usado para encolar). Tests que lleguen a `emitir_ecf` fallarán con `AttributeError`.

**Fix:** Agregar:
```python
async def set(self, key, value, ex=None):
    self._store[key] = value

async def rpush(self, key, *values):
    if key not in self._store:
        self._store[key] = []
    self._store[key].extend(values)
```

---

### I7. XSD directory vacía — validación siempre falla

**Archivo:** `xsd/README.md` (único archivo en `xsd/`)  
**Problema:** El directorio `xsd/` solo contiene un README. `ECFValidator` intenta cargar archivos `.xsd` y lanza `ValueError` si no los encuentra. Todo envío que pase por validación XSD fallará.

**Impacto:** La validación XSD es obligatoria (fix de sesión 3, item #28). Sin los archivos `.xsd` descargados de DGII, **ningún e-CF pasa el proceso completo**.

**Fix:** Documentar claramente en README y en `.env.example` que los schemas XSD deben descargarse manualmente de la DGII y copiarse a `xsd/`. Considerar un script de setup.

---

### I8. `_actualizar_ecf` sobreescribe `approved_at` con NULL en actualizaciones no-aprobadas

**Archivo:** `ecf_core/queue_worker.py` línea ~435  
**Problema:**
```sql
approved_at = CASE WHEN $1 = 'aprobado' THEN NOW() ELSE NULL END,
```
Si un e-CF fue aprobado y luego se actualiza por otro motivo, `approved_at` se pone a NULL.

**Fix:**
```sql
approved_at = CASE WHEN $1 = 'aprobado' THEN NOW() ELSE approved_at END,
```

---

## 🟡 MINOR — Cleanup, consistencia, mejoras menores

### M1. CORS no permite métodos DELETE/PATCH del Admin API

**Archivo:** `api_gateway/main.py` línea ~92  
```python
allow_methods=["POST", "GET"],
```
El Admin API usa `DELETE` y `PATCH` — si se accede desde browser (portal futuro), CORS los bloqueará.

**Fix:** `allow_methods=["GET", "POST", "PATCH", "DELETE"]`

---

### M2. `python-jose` no se usa en ningún archivo

**Archivo:** `requirements.txt` línea 24  
`python-jose[cryptography]==3.3.0` está listado pero no se importa en ningún módulo del proyecto. Dependencia muerta.

---

### M3. `bcrypt` no se usa en ningún archivo

**Archivo:** `requirements.txt` línea 23  
`bcrypt==4.1.3` está listado pero nunca se importa (ver I4 — bcrypt nunca se implementó para api_key_hash). Dependencia muerta.

---

### M4. `_safe_schema()` duplicado en 2 archivos

**Archivos:** `api_gateway/main.py` y `ecf_core/queue_worker.py`  
La misma función y regex se definen en dos lugares. Riesgo de divergencia.

**Fix:** Extraer a un módulo compartido, ej. `ecf_core/utils.py`.

---

### M5. `crear_tenant.py` imprime "Próximos pasos" truncado

**Archivo:** `scripts/crear_tenant.py` línea ~82  
El script se trunca al final con `print("    2. Configurar en Odoo:")` y no hay más. Falta el texto completo.

---

### M6. Landing form usa `mailto:` fallback

**Archivo:** `landing/index.html` sección CTA (~línea 1640)  
El formulario de contacto usa `window.location.href = 'mailto:...'` que depende del cliente de correo del usuario. En producción, debería enviar a un endpoint backend o servicio de formularios.

---

### M7. `COPY xsd/ ./xsd/` en Dockerfile sin wildcard guard

**Archivo:** `Dockerfile.api` línea 29  
```dockerfile
RUN mkdir -p /app/xsd
COPY xsd/ ./xsd/
```
Aunque `mkdir -p` previene error si el directorio no existe, Docker `COPY` fallará si el directorio `xsd/` no existe en el build context. El `RUN mkdir -p` crea el directorio EN el container, no protege el `COPY`.

**Fix:** Usar un `.dockerignore`-safe approach, o copiar condicionalmente:
```dockerfile
COPY xsd/README.md ./xsd/
# XSD files deben copiarse manualmente al build context antes de build
```

---

### M8. Footer dice "© 2026" — puede quedar desactualizado

**Archivo:** `landing/index.html` footer  
Año hardcodeado. Considerar JavaScript dinámico.

---

## 📋 RESUMEN

| Severidad | Cantidad | Estado |
|-----------|----------|--------|
| CRITICAL  | 4        | Pendiente |
| IMPORTANT | 8        | Pendiente |
| MINOR     | 8        | Pendiente |
| **Total** | **20**   | |

### Top 3 acciones inmediatas antes de producción:

1. **Fix C1** — Callback anulación envía estado correcto (`estado_final` en vez de `"anulado"`)
2. **Fix C3** — Quitar validación de longitud 64 en Odoo `set_values`
3. **Fix C2 + I6** — Corregir tests para que realmente pasen

### Issues ya corregidos en sesiones previas (no repetidos aquí):
- 66 fixes documentados en `audit-fixes-completed.md` (sesiones 1-5)
- Incluyen: UUID serialization, Pydantic v2, XML builder, mTLS, anti-replay webhook, estados anulación, multi-company Odoo, COALESCE en updates, y más.
