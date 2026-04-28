# AUDITORÍA GLOBAL — RENACE e-CF (DGII República Dominicana)

> **Fecha:** 2026-04-28  
> **Alcance:** Workspace completo (`api_gateway/`, `ecf_core/`, `odoo_module/`, `db/`, `xsd/`, `portal_admin/`, `scripts/`, `tests/`)  
> **Estado actual:** En producción — con **bugs críticos activos** que rompen funcionalidades clave.  
> **Versión Odoo módulo:** 18.0.3.5 (`ecf_connector`) y 19.0.1.0 (`ecf_connector_v19`)  
> **Marca:** Renace.tech  
> **Ámbito DGII:** Comprobantes Fiscales Electrónicos (e-CF) — 10 tipos (E31, E32, E33, E34, E41, E43, E44, E45, E46, E47)

---

## 0 · TL;DR — lo más urgente

1. **🔴 El endpoint público registrado en DGII para recibir e-CF está roto** (`POST /fe/recepcion/api/ecf` — `etree` no está importado). Cada e-CF que un emisor externo te envíe responde HTTP 500. Esto **te impide certificar el paso 9 ("Recepción e-CF")** y, si ya certificaste, **bloquea operación**.
2. **🔴 Los endpoints de Aceptación Comercial (ACECF/ARECF) y la sincronización de Compras Recibidas están rotos** — usan `CertVault()` no importado y un método `obtener_certificado(...)` que **no existe** (el método real se llama `obtener`).
3. **🔴 La anulación de e-CF nunca llega al estándar DGII** — el `DGIIClient.anular_ecf()` envía un XML `<AnulacionRango>` fabricado manualmente, sin firma XAdES y sin ajustarse al esquema oficial **ANECF.xsd** (que exige `Encabezado/RncEmisor/CantidadeNCFAnulados/FechaHoraAnulacioneNCF` + `DetalleAnulacion/Anulacion/...`).
4. **🟠 El "CUFE" implementado no existe en DGII RD.** La DGII no usa CUFE (eso es Colombia). Lo que la DGII utiliza es el **CódigoSeguridad** (6 alfanuméricos del `SignatureValue`) — ya implementado en `dgii_client.py:generar_security_code`. La columna `cufe` y el `CUFEGenerator` deben renombrarse o eliminarse.
5. **🟠 La firma XAdES-BES tiene riesgo de canonicalización** — el `DigestValue` de `SignedProperties` se calcula sobre el fragmento `xades_xml` antes de insertarlo en el árbol; tras la inserción los namespaces heredados del `<ds:Signature>` cambian la canonicalización c14n y la firma puede invalidarse en validación DGII.
6. **🟢 Hay dos módulos Odoo casi idénticos** (`ecf_connector` y `ecf_connector_v19`) con 90 % de código duplicado — riesgo alto de divergencia en producción.

Si el sistema está activo en producción **se debe priorizar**:  
1) Fix de los tres bugs `NameError`/`AttributeError` (sección 2.1, 2.2, 2.3).  
2) Reemplazar la implementación de anulación.  
3) Renombrar "CUFE → CodigoSeguridad" y limpiar el campo en DB.  
4) Validación XSD obligatoria (ya hay XSDs en `xsd/`, dejar de permitir `SKIP_XSD_VALIDATION` en producción).

---

## 1 · Nombre del módulo (depuración de marca)

Estado actual:
- Módulo Odoo: `ecf_connector` / `ecf_connector_v19` (técnico, descriptivo, no comercial).
- Manifiesto: `ECF Connector — DGII e-CF República Dominicana` (largo, no memorable).
- Marca corporativa: `Renace.tech`.
- Portal admin: `Renace ECF — Panel Admin`.
- PDF service: encabezado `RENACE TECH`.
- Stack Docker: `saas_ecf` (genérico).
- API Gateway título: `SaaS ECF DGII`.

**Nomenclatura recomendada (única, en todo el stack):**

| Capa | Nombre actual | Nombre propuesto |
|------|---------------|------------------|
| Producto comercial | varios | **Renace e-CF** |
| Módulo Odoo (carpeta) | `ecf_connector` | `renace_ecf` |
| Módulo Odoo (manifest `name`) | `ECF Connector — DGII e-CF` | `Renace e-CF — Facturación Electrónica DGII` |
| Stack/proyecto raíz | `ecf` / `saas_ecf` | `renace-ecf` |
| FastAPI title | `SaaS ECF DGII` | `Renace e-CF — DGII Gateway` |
| Portal admin | `Renace ECF — Panel Admin` | `Renace e-CF — Consola` |
| Imagen Docker | `ecf-api` | `renace-ecf-gateway`, `renace-ecf-worker` |

Beneficios:
- "Renace e-CF" es corto, evocador, contiene la marca y la categoría DGII (`e-CF`).
- Permite registrar `pip` packages con namespace propio: `renace_ecf_core`, `renace_ecf_dgii_client`.
- Evita la cacofonía actual ("ECF Connector", "SaaS ECF DGII", "Renace ECF") que hoy varía entre archivos.

---

## 2 · Bugs CRÍTICOS (rompen producción ahora mismo)

### 2.1 🔴 `etree` no importado en `api_gateway/main.py` → endpoint público DGII roto

[api_gateway/main.py:802](api_gateway/main.py:802) usa `etree.fromstring(xml_bytes)` pero `from lxml import etree` **no aparece** en los imports (líneas 1-37).

```python
@app.post("/fe/recepcion/api/ecf", include_in_schema=False)
async def recibir_ecf_externo(request: Request, db: asyncpg.Pool = Depends(get_db)):
    ...
    try:
        root = etree.fromstring(xml_bytes)   # NameError: name 'etree' is not defined
```

**Impacto:** este es el endpoint **público** que se registra en DGII como "URL de Recepción". Cuando otro contribuyente te envía un e-CF, el primer `POST` aborta con HTTP 500. La **DGII bloquea la certificación del paso 9 ("Recepción e-CF")** porque esa URL tiene que responder 202 con el RECF.

**Fix:** añadir `from lxml import etree` y validar el `RNCReceptor` con `_safe_schema(tenant["schema_name"])` (que tampoco se llama — ver 2.4).

---

### 2.2 🔴 `CertVault` no importado en `api_gateway/main.py`

[api_gateway/main.py:628, 711, 761](api_gateway/main.py:628) llaman a `CertVault()`. El import sólo trae `CertVaultRepository`:

```python
from ecf_core.cert_vault import CertVaultRepository  # ← falta CertVault
...
vault = CertVault()                                   # NameError
cert_repo = CertVaultRepository(db, vault)
```

**Impacto:** los tres endpoints siguientes lanzan `NameError` al primer request:
- `POST /v1/compras/sincronizar` — sincronización de e-CF Recibidas.
- `POST /v1/compras/{ncf}/aprobar` — Aceptación Comercial (ACECF).
- `POST /v1/compras/{ncf}/rechazar` — Rechazo Comercial (ARECF).

**Fix:**
```python
from ecf_core.cert_vault import CertVault, CertVaultRepository
```

---

### 2.3 🔴 Método `obtener_certificado` inexistente en `CertVaultRepository`

[ecf_core/cert_vault.py:155](ecf_core/cert_vault.py:155) define `async def obtener(self, tenant_id) -> bytes` (devuelve los bytes desencriptados del .p12).

Pero múltiples sitios llaman `obtener_certificado(...)` y esperan un **dict** con `cert_data` y `cert_password`:

| Llamador | Línea | Método llamado | Existe? |
|---|---|---|---|
| `api_gateway/main.py` aprobar | 712 | `vault.obtener_certificado(...)` | ❌ |
| `api_gateway/main.py` rechazar | 762 | `vault.obtener_certificado(...)` | ❌ |
| `ecf_core/ecf_recibidas_service.py` | 176 | `self.cert_repo.obtener_certificado(...)` | ❌ |

**Impacto:** los flujos de Aceptación Comercial **y** la sincronización completa de e-CF Recibidas explotan al primer call con `AttributeError`.

**Fix:** agregar a `CertVaultRepository`:
```python
async def obtener_certificado(self, tenant_id: str) -> dict:
    """Devuelve cert_data (bytes desencriptados) + cert_password (descifrada)."""
    p12 = await self.obtener(tenant_id)
    async with self.db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT cert_password FROM public.tenants WHERE id = $1",
            uuid.UUID(tenant_id),
        )
    enc = (row["cert_password"] or "") if row else ""
    return {"cert_data": p12, "cert_password": self.vault.descifrar_campo(enc)}
```

---

### 2.4 🔴 SQL injection latente en endpoint público

[api_gateway/main.py:816-822](api_gateway/main.py:816) construye SQL con `tenant["schema_name"]` **sin** llamar a `_safe_schema()` antes de interpolar:

```python
schema = tenant["schema_name"]
async with db.acquire() as conn:
    await conn.execute(f"""
        INSERT INTO {schema}.compras (ncf, rnc_proveedor, xml_original, estado_odoo)
        VALUES ($1, $2, $3, 'nueva')
        ...
```

Aunque hoy `tenant["schema_name"]` proviene de la DB y debería ser confiable, este es un **endpoint sin autenticación** (`auth=none` en su caso de uso), expuesto a Internet, y si por una migración o un error administrativo entra un valor malicioso, queda como SQLi. Política: cualquier interpolación de schema en SQL pasa por `_safe_schema`.

**Fix:** `schema = _safe_schema(tenant["schema_name"])`.

---

### 2.5 🔴 Anulación e-CF: no cumple esquema DGII oficial

**Problema 1 — XML inventado:** [ecf_core/dgii_client.py:415-420](ecf_core/dgii_client.py:415) genera:

```xml
<AnulacionRango xmlns="http://www.dgii.gov.do/ecf">
    <RNCEmisor>...</RNCEmisor>
    <CantidadDesde>...</CantidadDesde>
    <CantidadHasta>...</CantidadHasta>
</AnulacionRango>
```

El esquema oficial DGII (`xsd/ANECF.xsd`) exige:
```
ANECF
└── Encabezado (Version, RncEmisor, CantidadeNCFAnulados, FechaHoraAnulacioneNCF)
└── DetalleAnulacion
    └── Anulacion (NoLinea, TipoeCF, TablaRangoSecuenciasAnuladaseNCF/Secuencias[SecuenciaeNCFDesde, SecuenciaeNCFHasta], CantidadeNCFAnulados)
└── Signature  (firma XAdES-BES — `<xs:any>`)
```

El XML actual **será rechazado por la DGII** apenas se valide contra el XSD oficial.

**Problema 2 — sin firma:** el XML enviado a `EP_ANULACION_RANGO` se manda como `content=payload_xml.encode("utf-8")` sin pasar por `ECFSigner.firmar(...)`. La DGII rechaza por falta de firma del PSFE.

**Problema 3 — motivo se pierde:** el wizard recoge motivo y nota ([ecf_anular_wizard.py:17-28](odoo_module/ecf_connector/wizard/ecf_anular_wizard.py:17)) con códigos `01`–`09`, los manda al gateway → al worker → pero `dgii.anular_ecf(rnc, ncf_desde, ncf_hasta)` no recibe motivo ni nota. La información se descarta.

**Problema 4 — códigos de motivo incorrectos:** los 9 códigos del wizard mezclan motivos de NCF preimpresos con e-CF. La DGII para e-CF utiliza `CodigoModificacion` 1-4 (Descuento / Devolución / Anulación / Otro). Para anulaciones, además, el código sólo necesita rango de secuencias.

**Fix integral:**
1. Crear `ecf_core/ecf_anulacion_service.py` que genere XML conforme a `ANECF.xsd` y lo firme con `ECFSigner`.
2. Cambiar `DGIIClient.anular_ecf` para enviar el XML firmado.
3. Reducir el wizard a "rango de NCF + tipo + cantidad" + un campo libre de observación interna (que NO viaja a DGII en el XML, va al chatter).
4. Persistir XML firmado de la anulación en DB para auditoría 10 años (igual que el e-CF).

---

### 2.6 🔴 Firma XAdES-BES probablemente inválida ante DGII

[ecf_core/ecf_core_service.py:329-457](ecf_core/ecf_core_service.py:329) construye `xades:QualifyingProperties` como **fragmento string** y calcula su digest **antes** de insertarlo en el árbol firmado:

```python
signed_props_digest = self._sha256_b64(self._canonicalizar_string(xades_xml))   # ← canonicalización del fragmento
...
obj_node.append(etree.fromstring(xades_xml))  # ← inserción posterior
```

La canonicalización c14n exclusiva (`http://www.w3.org/2001/10/xml-exc-c14n#`) **incluye los namespaces heredados del contexto** del nodo. Cuando `<xades:SignedProperties>` está dentro de `<ds:Object>` que está dentro de `<ds:Signature xmlns:ds="...">`, los `xmlns:ds` declarados en el fragmento se vuelven redundantes y c14n los **omite**. El digest calculado sobre el fragmento aislado **no coincide** con el digest que la DGII calcula al validar.

**Síntoma esperado:** la DGII responde "Firma inválida" en validación.

**Fix:** insertar primero `<xades:QualifyingProperties>` en el árbol del documento, **luego** localizar el nodo `xades:SignedProperties` dentro del árbol y aplicar `etree.tostring(..., method="c14n", exclusive=True, with_comments=False)` sobre el nodo ya inserto. Verificar contra implementaciones de referencia (la del repositorio `victors1681/dgii-ecf` que ya cita `dgii_client.py`).

---

## 3 · Bugs IMPORTANTES (degradan la operación)

### 3.1 "CUFE" no existe en DGII RD — concepto y código a renombrar

Toda la base de código (`db/001_schema.sql:144`, `ecf_core_service.py:CUFEGenerator`, columnas `cufe` en compras/ecf, `tenant.cufe_secret`, PDF templates con "CUFE: …", QR URLs con `&CUFE=…`) está modelada como si la DGII RD usara CUFE. **No es así.** En DGII RD lo que existe es:

- **`CodigoSeguridadeCF`**: 6 caracteres alfanuméricos extraídos del `SignatureValue`. Ya implementado en [dgii_client.py:470](ecf_core/dgii_client.py:470).
- **`TrackId`**: identificador devuelto por la DGII al recibir el e-CF.
- **URL de consulta de timbre**: contiene `RncEmisor`, `ENCF`, `MontoTotal`, `FechaFirma`, `CodigoSeguridad`, **no CUFE**. (Ver `pdf_service.py:148` → la URL `?CUFE=` que hoy genera no es la oficial DGII.)

**Trabajo de renombrado (no breaking si se migra cuidado):**
- `db`: añadir columnas `codigo_seguridad`, `track_id` (ya existe `track_id` ✅) y dejar `cufe` como deprecated; en una migración futura, drop.
- `ecf_core_service.CUFEGenerator` → eliminar; ya no se usa el `clave_secreta` (`tenant.cufe_secret`).
- `pdf_service.HTML_TEMPLATE`: cambiar bloque "CUFE:" por "Código de Seguridad: XXXXXX" + "TrackId: …".
- `pdf_service.generar_pdf_html`: la URL del QR debe ser la real (`{base}/consultatimbre?...`) — esa lógica ya existe correctamente en `dgii_client.generar_qr_url`. Reutilizar, no duplicar.
- Odoo: el campo `ecf_cufe` en `account.move` y `pos.order` debería renombrarse a `ecf_codigo_seguridad` (con migración Odoo). Como mínimo, el `string` y `help` del campo deben corregirse.

### 3.2 Pydantic regex bloquea valores DGII válidos

[api_gateway/main.py:269-270](api_gateway/main.py:269):

```python
tipo_pago:     str = Field(default="1", pattern=r"^[123]$")     # DGII permite 1..9
tipo_ingresos: str = Field(default="01", pattern=r"^0[1-5]$")   # DGII permite 01..06 (Otros Ingresos)
```

Cuando un cliente Odoo envía un payload con `tipo_pago='4'` (Permuta) o `tipo_ingresos='06'` (Otros Ingresos), el Gateway responde 422 antes de llegar al worker. **La factura nunca se emite.**

**Fix:** `pattern=r"^[1-9]$"` y `pattern=r"^0[1-6]$"`.

### 3.3 `xml_original` nunca se persiste

`db/001_schema.sql:153` define `xml_original BYTEA` pero `_actualizar_ecf` ([queue_worker.py:438-457](ecf_core/queue_worker.py:438)) sólo guarda `xml_firmado`. El XML pre-firma se descarta.

Para auditoría DGII (retención 10 años) y debugging de firmas inválidas, **se debe** guardar también `xml_original`. Fix: incluir `xml_original` en el UPDATE.

### 3.4 `MontoTotalTransaccionado` no se incluye en `Resumen` (sólo en `Encabezado`)

[ecf_core_service.py:270-272](ecf_core/ecf_core_service.py:270): se añade el monto en moneda extranjera **sólo** en el bloque `<Totales>` del Encabezado. El esquema DGII también pide ese valor en el `<Resumen>` cuando la moneda no es DOP. Si el e-CF se emite en USD/EUR, el XML se valida XSD con error.

### 3.5 `FechaHoraFirma` se genera demasiado pronto

`_build_resumen` ([ecf_core_service.py:314-315](ecf_core/ecf_core_service.py:314)) escribe `FechaHoraFirma` con `datetime.now(utc)` durante la generación del XML — pero la firma real ocurre después en `ECFSigner.firmar()`. Si entre ambos pasos hay segundos (validación XSD, latencia), el `SigningTime` de XAdES y el `FechaHoraFirma` divergen. La DGII rechaza si la diferencia es notoria.

**Fix:** mover `FechaHoraFirma` a la fase de firma; o, lo más simple, escribirla en `ECFSigner.firmar` con el mismo `signing_time` que va al XAdES.

### 3.6 `PaginaActual` siempre `"1"` aunque hubiese más páginas

[ecf_core_service.py:294-298](ecf_core/ecf_core_service.py:294):

```python
def _build_paginacion(self, f):
    pag = etree.Element(...)
    self._e(pag, "PaginaActual", "1")
    ...
```

Pero `total_paginas` se calcula correctamente (1 cada 50 items). Para e-CF con > 50 ítems la DGII espera múltiples bloques `<Paginacion>` o un `PaginaActual` por hoja. Hoy: emisión incorrecta para facturas largas. Si nunca se han emitido > 50 ítems, el bug es latente.

### 3.7 Secuencia NCF — chequeo de gap no implementado

`public.next_ncf` es atómica (✅), pero no hay ningún reporte/cron que detecte:
- secuencias agotadas próximamente (90 % consumido),
- saltos por NCF fallidos cuyo número se "perdió" (estado `rechazado` después de transacción exitosa de `next_ncf`),
- duplicados (no debería pasar pero conviene auditar).

La DGII audita continuidad. Recomendación: crear `cron_alertar_ncf_secuencias` en `scheduler.py` que avise cuando faltan < 1000 NCFs disponibles para un tipo activo.

### 3.8 Doble emisión potencial: `action_post` + `action_pos_order_invoice`

[odoo_module/.../models.py:463-497](odoo_module/ecf_connector/models/models.py:463) emite e-CF si `ecf_emision_automatica` está ON y modo no es diferido.

[odoo_module/.../models.py:842-860](odoo_module/ecf_connector/models/models.py:842) (`PosOrder.action_pos_order_invoice`) **siempre** emite si `ecf_modo == 'inmediato'`, **ignorando** `ecf_emision_automatica`. 

Resultado: si la empresa apaga `ecf_emision_automatica` para reducir riesgo, el POS sigue emitiendo. Es una violación silenciosa de la "regla de oro" que el propio docstring del módulo declara.

**Fix:** respetar `move.company_id.ecf_emision_automatica` también en el flujo POS. O bien definir explícitamente que el POS siempre emite cuando NO es diferido — pero entonces hay que documentarlo.

### 3.9 `check_dgii_compliance` retorna datos falsos

[odoo_module/.../models.py:362-364](odoo_module/ecf_connector/models/models.py:362):

```python
issues.append({'type': 'info', 'msg': 'Certificado Digital: Activo (Expira en 180 días)'})
```

Esto es **siempre** lo mismo, hardcoded. El UI muestra al usuario "todo bien" sin consultar realmente el SaaS. En producción, induce a error grave si el cert expiró. **Solución:** llamar a `GET /v1/health` (que ya devuelve `cert_vencimiento`) o crear un endpoint específico `/v1/cert/info`.

### 3.10 `action_export_excel` no exporta nada

[odoo_module/.../models.py:378-388](odoo_module/ecf_connector/models/models.py:378): comentario "demo premium" — devuelve URL ficticia. El usuario hace clic y obtiene 404. Sacarlo o implementarlo de verdad con `xlsxwriter`.

### 3.11 Búsqueda imprecisa de impuesto en factura recibida

[ecf_compra_recibida.py:156-161](odoo_module/ecf_connector/models/ecf_compra_recibida.py:156): `tax_account` toma el primer impuesto de compra con `amount in [16,18]`. En DGII RD existen ITBIS 18 %, ITBIS 16 % (servicios profesionales y publicidad), exento, retenido 100 %, retenido 30 %. Tomar el primero arbitrariamente puede producir asientos contables inexactos y desfases en 606. **Fix:** parametrizar por tasa real de la línea o pre-mapear taxes en una pantalla de configuración.

### 3.12 Imports duplicados en `models.py`

[odoo_module/.../models.py:18-32](odoo_module/ecf_connector/models/models.py:18): `import logging` y `import requests` aparecen dos veces. `from datetime import date` también. Cosmético pero indica falta de pasada de linter (`ruff`/`flake8`).

### 3.13 `get_dashboard_stats` ineficiente

[odoo_module/.../models.py:237-303](odoo_module/ecf_connector/models/models.py:237): hace 4 `logs.filtered(...)` lambdas en Python. Para tenants con 50 k logs/mes esto es O(n) cuatro veces. Cambiar por `read_group` agrupando por `estado` y por `tipo_ecf`.

### 3.14 Falta validación de RNC con dígito verificador

La DGII rechaza RNCs sin algoritmo válido (mod-11). Hoy se valida sólo formato dígitos / longitud. Implementar validador en `ecf_core/utils.py` y usar en payload Pydantic.

### 3.15 Anti-replay del webhook acepta SHA-256 en hex pero `_notificar_odoo` envía `sha256=<hex>`

[ecf_core/ecf_recibidas_service.py:476-477](ecf_core/ecf_recibidas_service.py:476): el header sale como `X-ECF-Signature: sha256=<hex>`. Pero el receptor `webhook.py:_verificar_firma` ([webhook.py:83-87](odoo_module/ecf_connector/controllers/webhook.py:83)) compara directamente el header con `expected = hex(...)` — sin remover el prefijo `sha256=`. Hay un `replace('sha256=', '')` en `ecf_recibida` ([webhook.py:200](odoo_module/ecf_connector/controllers/webhook.py:200)) pero **no en `ecf_callback`**. Resultado: las dos rutas no son consistentes — una funciona, la otra falla si el SaaS evoluciona el formato.

**Fix:** unificar en `_verificar_firma`: aceptar tanto `<hex>` como `sha256=<hex>` con `sig_header = sig_header.replace('sha256=', '', 1)`.

### 3.16 `ecf_connector` y `ecf_connector_v19` divergen sin gobernanza

Dos módulos casi idénticos. Manifest distinto, `data` files distintos (v19 usa `ecf_v19_master.xml` y agrega `res_partner_views.xml`, `pos_order_views.xml`). Sin un build/script que sincronice los dos, el día que se aplique un fix solo en uno → bug latente en el otro.

**Recomendación:** un solo módulo `renace_ecf` con tags de versión Odoo y rama por versión Odoo (18.0 / 19.0). Migrar v19 a partir del v18 con `bin/migrate_18_to_19.sh` y dejar de mantener dos copias.

### 3.17 `models.py:267-274` — query SQL crudo en dashboard

```python
self.env.cr.execute(daily_query, (date_limit, self.env.company.id))
```

Está parametrizado (✅) pero usa `ecf_log` literal en lugar del nombre real de la tabla. Si el módulo cambia `_table = 'ecf_log_log'`, rompe. Reescribir con `read_group` o usar `self._table`.

### 3.18 Wizard de anulación: mensaje de chatter desactualizado

(Ya estaba en el AUDIT_REPORT.md previo como C4 — se ve resuelto en este código actual: [wizard:76-83](odoo_module/ecf_connector/wizard/ecf_anular_wizard.py:76) ahora dice "Anulación e-CF solicitada... Esperando confirmación DGII". ✅ resuelto.

### 3.19 Endpoint `/v1/ecf/anular` admite cualquier motivo de 1-2 chars

[main.py:879](api_gateway/main.py:879):
```python
motivo: str = Field(..., min_length=1, max_length=2)
```

Sin enum. Cualquier `"99"` pasa. Validar contra el set DGII (`{"01","02","03","04"}` para e-CF) — pero ojo, ver 2.5: la anulación misma no está implementada al estándar.

### 3.20 Idempotencia: la respuesta cacheada NO incluye errores

`emitir_ecf` cachea sólo respuestas `200`. Si dos requests con el mismo `Idempotency-Key` llegan y la primera falla con 422 (validación), la segunda llamada genera un nuevo NCF. Comportamiento aceptable (regenerar tras fallo) pero hay que documentarlo — hoy es ambiguo.

---

## 4 · Cumplimiento DGII (15 pasos del flujo de certificación)

Mapa de compatibilidad estado-actual ↔ requisitos DGII:

| # | Paso DGII | Componente del repo | Estado |
|---|-----------|---------------------|--------|
| 1 | **Registrado** | manual / fuera del sistema | ✅ |
| 2 | **Pruebas de Datos e-CF** (XML conformes) | `ecf_core_service.ECFXMLGenerator` + XSD en `xsd/` | 🟠 — generador funcional, **firma riesgosa (2.6)**, falta revisión schema-by-schema (sec. 5) |
| 3 | **Pruebas de Datos Aprobación Comercial** (ACECF) | `ecf_interchange_service.generar_aprobacion_comercial` | 🔴 — XML mínimo, no estructura completa (`Encabezado/DetalleAprobacionComercial` según `ACECF.xsd`); además los endpoints están rotos (2.2/2.3) |
| 4 | **Pruebas Simulación e-CF** | flujo completo + DGIIClient | 🟠 — funcional sólo si certificados y URLs reales están configurados; modo `simulacion` mock local existe |
| 5 | **Pruebas Simulación Representación Impresa** | `ecf_core/pdf_service.py` | 🟠 — sólo HTML, **no genera PDF real** ([pdf_service.py:153-161](ecf_core/pdf_service.py:153) "mock de PDF"). Necesita `weasyprint`/`wkhtmltopdf` para certificación |
| 6 | **Validación Representación Impresa** | mismo | 🔴 — el QR usa URL incorrecta `dgii.gov.do/verificaeCF?...&CUFE=...` (CUFE no existe). Plantilla menciona "CUFE" en lugar de "Código de Seguridad" |
| 7 | **URL Servicios Prueba** | `DGIIClient.URLS["TesteCF"]` y "CerteCF" | ✅ — endpoints `https://ecf.dgii.gov.do/CerteCF` configurados |
| 8 | **Inicio Prueba Recepción e-CF** | endpoint `/fe/recepcion/api/ecf` | 🔴 — **bug 2.1** (etree no importado) lo deja inoperante |
| 9 | **Recepción e-CF** | mismo | 🔴 — bloqueado por (8) |
| 10 | **Inicio Prueba Recepción Aprobación Comercial** | controller `_procesar_compras_recibidas` + `ECFRecibidasService` | 🔴 — **bug 2.3** (`obtener_certificado` no existe) |
| 11 | **Recepción Aprobación Comercial** | endpoints `aprobar` / `rechazar` | 🔴 — bugs 2.2 y 2.3 |
| 12 | **URL Servicios Producción** | `DGIIClient.URLS["eCF"]` | ✅ |
| 13 | **Declaración Jurada** | manual / DGII | ⚪ — fuera de scope técnico |
| 14 | **Verificación Estatus** | `consultar_por_track_id`, `/v1/ecf/{ncf}/estado` | ✅ |
| 15 | **Finalizado** | — | ⚪ |

**Conclusión:** sin los fixes 2.1, 2.2, 2.3 y 2.5, **no se completa la certificación** (pasos 8-11) y, si ya está completada, los flujos están rotos en producción.

### Otros incumplimientos formales

- `xsd/SKIP_XSD_VALIDATION=true` — debe **prohibirse** cuando `ambiente == 'eCF'` (producción). Hoy es global. Cambio sugerido en `ECFValidator.validar`:

  ```python
  if schema is None:
      if _SKIP_XSD_VALIDATION and ambiente in ("CerteCF", "TesteCF", "simulacion"):
          ...
      raise ValueError(...)
  ```

- Retención de 10 años: schema OK (`xml_firmado` BYTEA), pero falta política de backup/lifecycle documentada y `pgcrypto` activado para el at-rest.
- Ausencia de **Plan de Contingencia** documentado (la DGII lo audita en fase 1). Sólo hay reintentos + DLQ. Se recomienda documento `docs/contingencia.md` con: `MODO_OFFLINE` (emisión local con NCF preasignado, envío diferido), procedimiento si DGII no responde > 24h, política de reembolso al cliente.
- **Telemetría DGII**: Prometheus metrics existen (`/metrics`), pero faltan métricas por estado DGII (`ecf_aceptado_total{tipo="31"}`), latencia p50/p95/p99 por endpoint DGII.

---

## 5 · XSD: archivos presentes vs uso real

`xsd/` contiene 16 archivos:

```
ACECF.xsd  ANECF.xsd  ARECF.xsd
ECF-31.xsd ECF-32.xsd ECF-33.xsd ECF-34.xsd
ECF-41.xsd ECF-43.xsd ECF-44.xsd ECF-45.xsd ECF-46.xsd ECF-47.xsd
RFCE-32.xsd  Semilla.xsd
```

`ECFValidator._get_schema` ([ecf_core_service.py:519](ecf_core/ecf_core_service.py:519)) sólo busca `ECF-{tipo}.xsd`. **No valida** `ACECF`, `ANECF`, `ARECF`, `RFCE-32`, ni `Semilla`. Recomendación:

- Extender `ECFValidator` para validar también los tipos de evento (`ACECF/ANECF/ARECF`).
- Antes de enviar la semilla firmada, validar contra `Semilla.xsd`.
- Validar el RFCE para tipo 32 (RFCE-32.xsd existe — útil para retornos de consumo).

---

## 6 · Funciones contables — qué hay y qué falta

### Lo que ya existe ✅
- Reportes 606 (Compras), 607 (Ventas), 608 (Anulaciones) en JSON, TXT (formato DGII), XLSX, PDF.
- Sincronización de e-CF Recibidos desde DGII (cuando los bugs 2.x se arreglen).
- Aceptación / Rechazo Comercial (cuando 2.x se arreglen).
- Dashboard básico Odoo con conteo por estado / tipo / volumen.
- POS con e-CF E32/E31 y modo diferido para créditos.
- Detección automática de pago conciliado para emitir e-CF en POS diferido.
- Cron `_cron_detectar_ecf_listos` para POS diferidos.

### Lo que el README promete y NO está
- Tabla `retenciones` definida en schema (`{schema}.retenciones`) **sin endpoints** que la consuman, **sin reporte IR-17**, **sin lógica de cálculo de retención automática**.
- "Funciones contables superútiles" del manifest no están: las de POS y dashboard son cosmética.

### Lo que se debería añadir para ser un módulo "premium contable" (lo que pide el usuario)

#### A. Reportes fiscales y operativos
- **IT-1 (Declaración Mensual de ITBIS)** — agrupar 606 + 607 + ITBIS adelantado vs causado por mes y producir el formulario IT-1 listo para subir a OFV.
- **IR-17 (Retenciones a Terceros)** — endpoint y reporte usando la tabla `retenciones`.
- **606 con clasificación automática** de `tipo_bienes` (1=bien / 2=servicio / 3=arrendamiento / 4=publicidad / 5=…) según producto Odoo o NLP de descripción.
- **Reporte de NCFs en limbo** — e-CF >24h en `pendiente`/`enviado` sin respuesta DGII.
- **Reporte de continuidad de secuencias NCF** — detecta saltos por tipo y consumo proyectado vs `secuencia_max`.
- **Anexo "Costos y Gastos"** — exigido por DGII para ciertos contribuyentes.

#### B. Conciliación contable
- **Asiento contable automático al aprobar e-CF**: hoy Odoo crea el asiento al `action_post`, pero el ITBIS adelantado/causado debería marcarse en una analítica `ECF=APROBADO` para conciliación con DGII.
- **Conciliación 606 ↔ DGII**: comparar el 606 propio con el "Reporte de Compras" que DGII expone a cada contribuyente. Detectar facturas que el proveedor reportó a DGII pero no entraron a Odoo.
- **Conciliación 607 ↔ pagos bancarios**: enriquecer cada e-CF aprobado con `payment_state`/`bank_match`.

#### C. Cálculo automático
- **Retención del 30 %/100 %** del ITBIS al pagar a personas físicas — generar líneas de retención al validar pago.
- **Retención 10 %** del ISR a profesionales liberales — auto-cálculo a partir del partner type.
- **Distribución de gastos comunes** (luz/agua) entre cuentas analíticas a partir de e-CF tipo 41.

#### D. Alertas y gobierno
- Alerta cuando el certificado .p12 vence < 30 días (ya existe en `scheduler.py` ✅).
- Alerta cuando NCF disponible < 1000 por tipo activo (no existe).
- Alerta cuando hay e-CF en DLQ (no existe).
- Alerta cuando hay e-CF rechazado en últimas 24h.

#### E. Dashboard ejecutivo (CFO view)
- KPIs: ITBIS facturado/causado mes vs mes anterior, top 10 clientes por monto, top 10 proveedores, % aprobación DGII (KPI de calidad técnica), tiempo promedio aprobación.

#### F. Compliance interno
- Log inalterable de toda anulación con quién, cuándo, motivo (la DB ya tiene `ecf_estado_log`; falta UI completa en Odoo).

---

## 7 · Estructura, DevOps y calidad de código

### 7.1 Imports duplicados / sucios
- `ecf_connector/models/models.py:18-32` — `import logging`, `import requests`, `from datetime import date` duplicados.
- `api_gateway/main.py:9` y línea 43 — `import re` duplicado.

### 7.2 Funciones duplicadas (DRY)
- `_safe_schema` y `_SAFE_SCHEMA_RE` están en `api_gateway/main.py` y `ecf_core/queue_worker.py`. Mover a `ecf_core/utils.py`.

### 7.3 Tests
- Hay `tests/test_homologacion.py` y `test_api.py` pero la auditoría previa (AUDIT_REPORT.md C2/I5/I6) detectó tests **rotos**. Sin verificación reciente, asumimos siguen rotos. Antes de cualquier deploy, `pytest -q` debe pasar — hoy probablemente no.

### 7.4 Dependencias muertas (del audit previo)
- `python-jose[cryptography]==3.3.0` — no se usa.
- `bcrypt==4.1.3` — no se usa (la columna `api_key_hash` nunca implementó bcrypt; usa SHA-256 igual que `api_key`).

### 7.5 Migraciones SQL acumuladas sin orden
`db/001_*.sql` … `db/006_*.sql`: existen 005 y 006 que son fixes a checks de `001`. El `001` original no se actualizó — un re-deploy desde cero usa el check anterior. Idealmente: `001` debe reflejar el estado final, y `005/006` quedar como "ya aplicadas" para entornos existentes. O bien: usar Alembic.

### 7.6 Docker
- `Dockerfile.api` (1 archivo). No hay `Dockerfile.worker`, `Dockerfile.scheduler`. Si el `docker-compose.yml` los referencia, hay dependencia frágil (verificar).
- `RUN mkdir -p /app/xsd && COPY xsd/ ./xsd/`: el `mkdir -p` crea en el container, no protege el `COPY` durante el build (si `xsd/` no existe en el contexto el build falla).

### 7.7 `.gitignore`
- `RNC_Contribuyentes_Actualizado_25_Abr_2026.csv` (113 MB) está commiteado al repo (visible en `ls`). Esto **no debería** estar en git: ralentiza clones, expone datos. Recomendación: `git rm --cached`, agregar a `.gitignore`, dejar instructivo para descargar de DGII.

### 7.8 `scratch/` está en disk pero no en `.gitignore`
Posibles datos personales en scratch. Verificar.

---

## 8 · Seguridad — recordatorio + nuevos hallazgos

| # | Item | Estado |
|---|------|--------|
| S1 | `api_key` SHA-256 vs bcrypt | 🟠 — la columna `api_key_hash` declara bcrypt en comment pero almacena SHA-256. Decidir uno y consolidar (rendimiento favorece SHA-256 con prefijo aleatorio si el ataque es timing+rainbow; bcrypt sólo necesario si el hash filtra) |
| S2 | mTLS con DGII | ✅ — implementado en `dgii_client.__aenter__` |
| S3 | HMAC-SHA256 webhook con anti-replay | ✅ — `_verificar_timestamp` en webhook.py |
| S4 | Webhook controller no rota secret | 🟠 — agregar `ecf_webhook_secret_previous` para rotación zero-downtime |
| S5 | XAdES con `_canonicalizar_string` | 🔴 — ver 2.6 |
| S6 | Endpoint público `/fe/recepcion/api/ecf` sin rate limit | 🟠 — sin auth y sin límite. Riesgo DDoS / spam. Añadir limitador por IP o por RNC receptor |
| S7 | Schema interpolation sin `_safe_schema` | 🔴 — ver 2.4 |
| S8 | Logs en `_callback_odoo` pueden incluir el body | 🟢 — actualmente sólo logea `move_id`. OK |
| S9 | `cert_password` se almacena en `tenants.cert_password` cifrada con `cifrar_campo` | ✅ — cifrado AES-GCM |
| S10 | `cufe_secret` en DB cifrado | ✅ — pero ya no es necesario tras renombrar 3.1 |

---

## 9 · Plan de acción recomendado (orden de ejecución)

### Sprint 0 — hot-fix de producción (1 día)
1. Importar `etree` y `CertVault` en `api_gateway/main.py`.
2. Añadir `_safe_schema` en endpoint `/fe/recepcion/api/ecf`.
3. Añadir método `obtener_certificado` a `CertVaultRepository`.
4. Relajar regex Pydantic `tipo_pago`/`tipo_ingresos`.

### Sprint 1 — DGII compliance estricto (1-2 semanas)
5. Reescribir flujo de Anulación → ANECF.xsd correcto, firmado con XAdES.
6. Auditar firma XAdES contra implementación de referencia (victors1681/dgii-ecf) y tests con un certificado real de pruebas.
7. Renombrar CUFE → CodigoSeguridad / TrackId en código y UI; mantener columna `cufe` deprecated en DB con migración.
8. Activar validación XSD obligatoria en producción + extender a ACECF/ANECF/ARECF/Semilla.
9. PDF real (weasyprint) + URL QR oficial.
10. Persistir `xml_original`.

### Sprint 2 — funciones contables premium (2-3 semanas)
11. IT-1 (mensual ITBIS) con conciliación 606+607+retenciones.
12. IR-17 (retenciones).
13. Reporte de NCFs en limbo + alertas DLQ.
14. Asientos contables con marca analítica `ECF=APROBADO`.
15. Wizard de retención automática al pagar (10 % ISR profesional, 30 % ITBIS persona física, 100 % a no-cooperantes).

### Sprint 3 — depuración de marca y código (1 semana)
16. Renombrar todo a `Renace e-CF` (módulos, manifest, README, Docker images).
17. Consolidar `ecf_connector` y `ecf_connector_v19` en un solo árbol con tags por versión Odoo.
18. Limpiar imports duplicados, `ruff` + `pre-commit`.
19. Reorganizar migraciones SQL (todas reflejadas en `001`, las posteriores como históricas).
20. `git rm --cached` del CSV de RNC y agregarlo al `.gitignore`.

### Sprint 4 — observabilidad + seguridad (1 semana)
21. Métricas Prometheus por estado DGII y latencia.
22. Plan de Contingencia documentado (`docs/contingencia.md`).
23. Rotación zero-downtime de webhook secret.
24. Rate-limit en endpoint público.
25. Tests pasando en CI antes de cada merge.

---

## 10 · Resumen ejecutivo

| Categoría | Hallazgos | Severidad mediana |
|-----------|-----------|-------------------|
| Bugs CRITICAL | 6 | 🔴 |
| Bugs IMPORTANT | 20 | 🟠 |
| Compliance DGII (15 pasos) | 4 pasos rotos por bugs critical | 🔴 |
| Funciones contables faltantes | ~10 capacidades | 🟠 |
| Marca/Naming | dispersión multi-archivo | 🟢 |
| Tests | rotos previamente, sin verificación | 🟠 |
| Seguridad | 3 hallazgos nuevos | 🟠 |

**Recomendación operativa:** congelar `main` para tráfico nuevo de tenants hasta que Sprint 0 esté en producción. Los tenants existentes que ya emiten e-CF (E31/E32) tipo 1 inmediato siguen operando — los flujos rotos son anulación, recepción, aceptación comercial.

**Recomendación estratégica:** un solo nombre, un solo módulo, un solo set de tests, validación XSD siempre. La promesa "premium contable" del manifest se cumplirá tras Sprint 2 — hoy todavía es un emisor + sincronizador con cosmética.
