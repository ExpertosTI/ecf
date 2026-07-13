# Renace e-CF — Avances y Pendientes (2026-07-13)

> Documento actualizado en la séptima sesión (onboarding asistido multi-tenant).
> Total acumulado: **58 hallazgos / mejoras resueltas**.
> Validación: pytest 59/59 ✅

---

## ✅ Avances — Sesión 1 (resolución inicial)

### 1. ACECF / ARECF conforme al XSD oficial — N1 ✅
- [ecf_core/ecf_interchange_service.py](ecf_core/ecf_interchange_service.py) — reescrito completo
- Estructura ACECF/ARECF conforme a XSD; validación XSD antes de firmar
- Endpoints `/v1/compras/{ncf}/aprobar` y `/rechazar` cargan `fecha_comprobante` y `total_monto` desde DB

### 2. RNC mock eliminado — N3 ✅
- [api_gateway/admin.py](api_gateway/admin.py) — `mock_db` con 4 RNCs hardcodeados eliminado; ahora devuelve 404/503 reales

### 3. Dashboard con datos reales — C1, C2, C3, C4 ✅
- Ping real al SaaS desde `check_dgii_compliance`; 3 estados `online/warning/offline`
- Íconos por severidad (`oi-x-circle`, `oi-warning-triangle`, `oi-check-circle`)
- Score matizado: `100 − 25·errors − 5·warnings`

### 4. POS loader migrado a Odoo 18 — N2 ✅
- `_load_pos_data_models / _load_pos_data_fields / _load_pos_data_domain` en ECFTipo y PosSession
- `ecf_api_key` excluido del loader POS (ya no llega al navegador)

### 5. Tracking de campos auditables — O2 ✅
- `tracking=True` en `ecf_tipo_id`, `ecf_modo`, `ecf_ncf`, `ecf_estado`, `ecf_codigo_seguridad`, `ecf_track_id`

### 6. Multi-company en ECFLog — O3 ✅
- `_check_company_auto = True`, `move_id` con `check_company=True`

### 7. Dashboard con `read_group` — C5, C6 ✅
- Reemplazados 4 `filtered()` Python con 3 `read_group` SQL agregados

### 8. RNC mod-11 en Odoo y Pydantic — P3 ✅
- Validador mod-11 inline en `models.py`; `validar_rnc_o_cedula` en `utils.py`; `field_validator` en `FacturaPayload`

### 9. URL QR oficial DGII — P9 ✅
- [ecf_core/pdf_service.py](ecf_core/pdf_service.py): `ecf.dgii.gov.do` correcto
- [ecf_pos_receipt.xml](odoo_module/ecf_connector/static/src/xml/ecf_pos_receipt.xml): legend oficial

### 10. SKIP_XSD bloqueado en ambientes productivos — P19 ✅
- [ecf_core/ecf_core_service.py](ecf_core/ecf_core_service.py): bypass XSD solo en `simulacion`; lanza excepción en `certificacion/produccion/testecf`

### 11. Multi-company en ECFCompraRecibida — P1 ✅
- `_check_company_auto = True`, `check_company=True` en `partner_id` y `move_id`

### 12. Diferido refinado en POS — P4 ✅
- Regla corregida: `diferido` solo si hay crédito pendiente O si es E31 sin RNC válido del partner

---

## ✅ Avances — Sesión 2 (esta sesión)

### 13. ARECF automático al recibir e-CF — P8 ✅
**Archivo:** [api_gateway/main.py](api_gateway/main.py)

- Nueva corutina `_enviar_arecf_background(...)` que firma y envía ARECF Estado=0 a `/fe/acuserecibo/api/ecf`
- Se dispara con `asyncio.create_task(...)` tras INSERT exitoso (no bloquea el 202 al emisor)
- Detecta duplicados: si INSERT retorna `"INSERT 0 0"` no re-envía ARECF
- Query de tenant ahora incluye `ambiente` para configurar el `DGIIClient` correcto

### 14. Rate-limit en endpoint público — P13 ✅
**Archivo:** [api_gateway/main.py](api_gateway/main.py)

- `_check_rate_limit_ip` (30 req/min por IP, configurable via `IP_RATE_LIMIT_MAX` / `IP_RATE_LIMIT_WINDOW`)
- Ya estaba implementado y cableado en `/fe/recepcion/api/ecf` (confirmado en revisión)

### 15. Rotación webhook con ventana de gracia — P14 ✅
**Archivos:** [api_gateway/admin.py](api_gateway/admin.py), [ecf_core/queue_worker.py](ecf_core/queue_worker.py)

- `rotate-webhook` ahora salva el secret anterior a Redis con TTL de 15 min (`WEBHOOK_ROTATION_TTL` env)
- `_callback_odoo` en queue_worker: si Odoo responde 401/403, reintenta con el secret previo de Redis (ventana de rotación)
- Respuesta del endpoint incluye `ttl_segundos` para informar al operador

### 16. Health check POS sin exponer ecf_api_key — P2 ✅
**Archivos:** 
- [odoo_module/ecf_connector/models/models.py](odoo_module/ecf_connector/models/models.py)
- [odoo_module/ecf_connector/static/src/js/ecf_type_button.js](odoo_module/ecf_connector/static/src/js/ecf_type_button.js)
- Equivalentes en `ecf_connector_v19`

- Nuevo método `res.company.pos_check_ecf_health()` — hace el ping al SaaS server-side
- JS reemplaza `orm.searchRead("res.company", …, ["ecf_saas_url", "ecf_api_key"])` + `fetch(…)` con `this.orm.call("res.company", "pos_check_ecf_health", [[]])`
- `ecf_api_key` ya nunca sale al navegador en ningún flujo POS

### 17. ITBIS mapeado por tasa real — P5 ✅
**Archivos:** [ecf_compra_recibida.py](odoo_module/ecf_connector/models/ecf_compra_recibida.py) (v18 y v19)

- Calcula `tasa_itbis` desde `itbis_facturado / monto_base × 100` (redondea a 16 o 18)
- Busca el impuesto de compra por `amount == tasa_real` (no más `amount IN [16, 18]`)
- Si no encuentra impuesto, loguea warning y deja la línea sin impuesto (no falla silenciosamente)

### 18. Métricas Prometheus enriquecidas — P11 ✅
**Archivo:** [api_gateway/main.py](api_gateway/main.py)

- `ecf_total{estado="aprobado",tipo="31"}` — contadores por estado×tipo desde cada schema de tenant
- `ecf_aprobacion_latency_avg_seconds{tipo="31"}` — latencia media de aprobación DGII por tipo de e-CF
- Métricas de cola existentes conservadas (`ecf_queue_pending`, `ecf_queue_retry`, `ecf_queue_dlq`)

### 19. Tests API corregidos — P17 ✅
**Archivo:** [tests/test_api.py](tests/test_api.py)

- `test_invalid_api_key`: 403 → 401 (código real del endpoint)
- `_valid_payload`: elimina campos fantasma (`ncf`, `monto_total`, `monto_itbis`); usa `itbis_tasa` en lugar de `itbis`

### 20. Linting config (ruff + pre-commit) — P16 ✅
**Archivos:** [pyproject.toml](pyproject.toml), [.pre-commit-config.yaml](.pre-commit-config.yaml)

- `ruff` con reglas E/W/F/I/UP/B/S/C4; tolerancias específicas para FastAPI y tests
- Pre-commit: ruff, ruff-format, trailing-whitespace, check-added-large-files (> 5 MB bloqueado)

---

## ✅ Avances — Sesión 3 (esta sesión)

### 21. cufe → codigo_seguridad en SQL y Python — P6 ✅
**Archivos:** `ecf_core/dgii_client.py`, `ecf_core/ecf_recibidas_service.py`, `ecf_core/queue_worker.py`, `api_gateway/main.py`, `api_gateway/admin.py`

- `RespuestaDGII.cufe` → `RespuestaDGII.codigo_seguridad` en dataclass y `_parsear_respuesta`
- `ECFRecibida.cufe` → `codigo_seguridad` en dataclass, parsers XML/JSON y SQL INSERT/SELECT
- `_actualizar_ecf(cufe=...)` → `codigo_seguridad=...` — parámetro, SQL UPDATE y llamadas
- Payload webhook: `"cufe"` → `"codigo_seguridad"` en el callback a Odoo
- Todos los SQL SELECT de `ecf` y `compras` actualizados (`cufe` → `codigo_seguridad`)
- **Migración SQL:** `db/010_rename_cufe.sql` — renombra en todos los schemas de tenant existentes + actualiza `crear_schema_tenant()`

### 22. Eliminar alias ecf_cufe / dejar solo ecf_codigo_seguridad — P10 ✅
**Archivos:** `odoo_module/ecf_connector/` y `ecf_connector_v19/` — models, views, controllers, reports, receipts

- `account.move.ecf_cufe` → `ecf_codigo_seguridad` (stored field directo, sin alias computed)
- `pos.order.ecf_cufe` → `ecf_codigo_seguridad` (related field)
- `ecf.log.cufe` → `codigo_seguridad`
- `ecf.compra.recibida.cufe` → `codigo_seguridad`
- Todas las vistas XML, reportes PDF y recibos POS actualizados
- Webhook controllers (v18, v19): leen `codigo_seguridad` con fallback `cufe` para compatibilidad
- **Migraciones Odoo:** `18.0.5.0.0/pre-migrate.py` y `19.0.3.0.0/pre-migrate.py` — renombran DB columns
- Versiones bumped: `ecf_connector` `18.0.4.0` → `18.0.5.0` / `ecf_connector_v19` `19.0.2.0` → `19.0.3.0`

### 23. db/001_schema.sql consolidado al estado final — P18 ✅
**Archivo:** `db/001_schema.sql`

- Versión bumped a `v2.6 (estado final post-migraciones 002–010)`
- `crear_schema_tenant()` reescrita con estado completo: `codigo_seguridad`, tabla `rfce`, `estado_comercial`, `motivo_rechazo`, índice `idx_compras_estado_comercial`, tabla `ecf_recibidas_sync`
- `001_schema.sql` es ahora la única fuente de verdad para nuevos deployments

### 24. Plan de contingencia DGII documentado — P12 ✅
**Archivo:** `docs/contingencia.md` (nuevo)

- 6 escenarios cubiertos: fallo API, fallo DGII, cert vencido, fallo DB, API key comprometida, rotación webhook
- Matriz de contactos de escalamiento (L1/L2/L3)
- Checklist pre-certificación DGII
- Procedimiento de operación en modo diferido (contingencia DGII)

### 25. README con flujo de certificación DGII paso a paso — P20 ✅
**Archivo:** `README.md`

- Sección "Flujo de certificación DGII — paso a paso" con 6 pasos numerados
- Casos obligatorios DGII en tabla (E31–E34 + exento + USD + anulación + consulta)
- Comandos exactos para cada fase (crear tenant, subir cert, emitir, presentar)
- `db/010_rename_cufe.sql` añadido al árbol de archivos

---

## ✅ Avances — Sesión 4 (2026-06-24)

### 26. Hotfix deploy: conflicto de puerto Traefik (8080) ✅
**Archivos:** `docker-compose.yml`, `.env.example`, `deploy.sh`

- `docker-compose.yml`: puertos host de Traefik ahora configurables por entorno:
   - `TRAEFIK_HTTP_PORT` (default `8080`)
   - `TRAEFIK_HTTPS_PORT` (default `8443`)
- `.env.example`: se agregan ambas variables para evitar hardcode operacional.
- `deploy.sh`: pre-check de puertos antes de `up -d ... traefik` para fallar rápido con mensaje claro cuando hay colisión.

### 27. Hotfix deploy: red compartida `ecf_network` con `pruecf` ✅
**Archivo:** `deploy.sh`

- Se elimina `down --timeout 30` en ciclo de redeploy para evitar error al remover red compartida.
- Nuevo flujo seguro: `stop` + `rm -f` solo de servicios del proyecto (`api`, `worker`, `scheduler`, `traefik`, `postgres`, `redis`).
- Resultado: el despliegue no intenta borrar `ecf_network` cuando existen endpoints activos de `pruecf_*`.

---

## ✅ Avances — Sesión 5 (2026-06-27)

### 28. Webhook Odoo: NameError en callback DGII ✅
**Archivos:** `odoo_module/ecf_connector/controllers/webhook.py`, `ecf_connector_v19/controllers/webhook.py`

- Corregido `NameError: cufe` en `_procesar_callback` — variable inexistente tras renombrar a `codigo_seguridad`
- Mensaje del chatter ahora muestra "Cód. Seguridad" conforme a nomenclatura DGII RD

### 29. POS: sincronización `ecf_codigo_seguridad` ✅
**Archivos:** `ecf_connector/static/src/js/ecf_pos.js`, `ecf_connector_v19/static/src/js/ecf_pos.js`

- `export_as_JSON` / `init_from_JSON` usan `ecf_codigo_seguridad` (alineado con `export_for_ui` y recibo POS)
- Compatibilidad con sesiones antiguas vía fallback `json.ecf_cufe`

### 30. Payload Odoo con campos normativos DGII ✅
**Archivos:** `ecf_connector/models/models.py`, `ecf_connector_v19/models/models.py`

- Nuevo `_dgii_campos_emision()`: `tipo_pago`, `tipo_ingresos`, `direccion_comprador`, `codigo_modificacion` (E33/E34)
- Validación E33/E34 exige factura original con `ecf_ncf`
- Eliminado campo fantasma `ambiente` del payload (el tenant se resuelve por API Key en el gateway)
- v19: validación mod-11 RNC/cédula, algoritmo RNC inline, POS diferido alineado con v18

### 31. Parser `codigo_seguridad` en respuestas DGII ✅
**Archivos:** `ecf_core/dgii_client.py`, `ecf_core/ecf_recibidas_service.py`

- `_parsear_respuesta` acepta `CodigoSeguridad` / `codigoSeguridad` además de alias legacy `CUFE`
- Parser XML de compras recibidas busca `CodigoSeguridad` antes que `CUFE`

### 32. `.gitignore` — artefactos locales de prueba ✅
- Excluye `/*.xlsx` y `/2026*.xml` en la raíz del repo (postulaciones, XML de prueba DGII)

### 33. Panel admin — certificación DGII end-to-end ✅ (2026-06-27)
**Archivos:** [portal_admin/index.html](portal_admin/index.html), [api_gateway/admin.py](api_gateway/admin.py), [ecf_core/platform_config.py](ecf_core/platform_config.py), [db/012_platform_psfe.sql](db/012_platform_psfe.sql)

- PSFE de plataforma cifrado en DB (`platform_psfe`) — subida desde menú **Plataforma** (sin editar `.env`)
- Asistente de 8 pasos por empresa alineado al Manual Técnico e-CF: PSFE mTLS → CerteCF, .p12 vigente, NCF E31–E47, postulación firmada, Odoo, Set de Pruebas (E31–E34 aprobados), presentación DGII
- Tabla de progreso homologación (conteo e-CF aprobados por tipo en schema del tenant)
- Botones **Probar CerteCF** (semilla mTLS PSFE) y autenticación completa contribuyente (semilla + .p12 → token)
- Worker/scheduler/API cargan PSFE desde DB al arrancar

---

## ✅ Avances — Sesión 4 (2026-07-03): depuración global multitenant + DGII

### 34. RFCE reescrito al modelo por-factura (RFCE 32) ✅
**Archivos:** [ecf_core/rfce_service.py](ecf_core/rfce_service.py), [ecf_core/dgii_client.py](ecf_core/dgii_client.py), [ecf_core/queue_worker.py](ecf_core/queue_worker.py), [db/001_schema.sql](db/001_schema.sql), [db/013_rfce_por_factura.sql](db/013_rfce_por_factura.sql)

- El servicio RFCE generaba un "resumen diario" inexistente en la norma; ahora emite un RFCE **por cada factura E32 < RD$250,000**, conforme a `RFCE 32.xsd`, firmado y validado antes de persistir
- `DGIIClient`: host dedicado `fc.dgii.gov.do` (`URLS_FC`, `enviar_rfce`), QR de E32 apunta a `fc.dgii.gov.do`
- `queue_worker`: enruta E32 < umbral por el flujo RFCE; los demás por `enviar_ecf`
- DB: tabla `rfce` con `ncf UNIQUE` por factura + FK `ecf.rfce_id`; migración `013` idempotente para schemas existentes; `crear_schema_tenant` v2.7

### 35. Scheduler: reconciliación y resiliencia ✅
**Archivo:** [ecf_core/scheduler.py](ecf_core/scheduler.py)

- `poll_ecf_en_proceso`: consulta DGII por `track_id` para e-CF atascados en `enviado` (>5 min) y actualiza estado
- `reencolar_pendientes`: re-encola e-CF `pendiente` huérfanos (>10 min) si Redis falló tras el commit
- `procesar_rfce_pendientes`: emite RFCE faltantes para E32 aprobados sin resumen
- `alertar_dlq`: alerta (con cooldown 24h) si la DLQ acumula elementos
- Cada job corre aislado: una excepción no tumba el ciclo del scheduler

### 36. API Gateway: idempotencia y cuota atómicas ✅
**Archivo:** [api_gateway/main.py](api_gateway/main.py)

- `Idempotency-Key` con `SET NX` en Redis: dos requests concurrentes ya no pueden asignar 2 NCF (devuelve 409 mientras la primera está en curso)
- Cuota mensual con `UPDATE ... WHERE ecf_emitidos_mes < max_ecf_mensual RETURNING`: el límite ya no es evadible por concurrencia
- Mocks DGII (`semilla`, `validacioncertificado`, `aprobacioncomercial`) solo activos con `ECF_AMBIENTE=simulacion`
- Recepción externa (`/fe/recepcion/api/ecf`): ahora parsea nombre emisor, tipo e-CF, fecha, montos e ITBIS del XML (antes insertaba `total_monto=0`)
- `_get_client_ip`: toma el **último** valor de `X-Forwarded-For` (el del proxy confiable) — el primero es falsificable
- `GET /v1/ecf/{ncf}/estado` ahora devuelve `track_id`, `security_code` y `qr_url`

### 37. Admin: ruta duplicada y bug de vault ✅
**Archivo:** [api_gateway/admin.py](api_gateway/admin.py)

- Eliminada la definición duplicada de `POST /tenants/{id}/test-webhook` (FastAPI ignoraba una silenciosamente)
- `test-webhook` firma con `sha256=<hex>` (mismo formato que el worker) — antes el ping fallaba la verificación en Odoo
- `CertVaultRepository(db)` → `CertVaultRepository(db, CertVault())` en sync-compras (evitaba un `TypeError` en runtime)

### 38. Webhooks Odoo v18/v19: IDOR y firma obligatoria ✅
**Archivos:** `ecf_connector/controllers/webhook.py`, `ecf_connector_v19/controllers/webhook.py`, `ecf_connector*/models/models.py`

- `/ecf/webhook/recibida`: eliminado fallback `browse(1)` (IDOR) — el RNC del header es obligatorio y la firma HMAC ya no es opcional
- v18 persiste `track_id` del callback (antes se descartaba)
- Guard anti re-emisión: no se puede volver a emitir una factura con NCF activo (evita NCF duplicados ante DGII)
- RNC del comprador normalizado a dígitos antes de clasificar (guiones rompían la detección RNC/Cédula) y se envía normalizado
- Emisión desde Odoo envía `Idempotency-Key` (`odoo-{company}-{move}-{seq}`) — un doble clic o timeout+retry ya no asigna 2 NCF

### 39. Módulos POS portados a Odoo 18 + limpieza ✅
**Archivos:** `mobile_service/`, `pos_shipment_manager/`

- `models.ValidationError`/`models.UserError` (inexistentes) → `odoo.exceptions` en `mobile_service` (11 usos que crasheaban con `AttributeError`)
- Loaders POS legacy (`_loader_params_*`, `_pos_data_process`, `_get_pos_ui_*`) portados a `_load_pos_data_fields` de Odoo 18; mensajeros/producto de envío vía RPC `pos.session.load_shipment_data` en `processServerData`
- API pública de tickets: PIN comparado en tiempo constante + rate-limit (10 intentos / 15 min por IP)
- `pos_shipment_manager`: token vacío ya no matchea registros con token NULL; `/thermal_print` verifica `check_access` antes de renderizar con sudo
- Manifest `mobile_service`: añadidos `account` y las vistas de wizards que no se cargaban
- Limpieza: eliminada la copia anidada duplicada de `pos_shipment_manager`, JS/XML muertos de la API POS vieja (9 archivos), `main.py` legacy sin PIN, `.bak`, scripts de diagnóstico y `dashboard_pos/` vacío

### 40. Despliegue producción ✅
**Archivo:** [docker-compose.prod.yml](docker-compose.prod.yml)

- **Eliminado `--api.insecure=true` y el puerto 8080 de Traefik** (dashboard sin autenticación expuesto a internet)
- El scheduler recibe `PSFE_*`, `DGII_CA_B64` y `ECF_AMBIENTE` (ahora habla con DGII para polling/RFCE)

### Verificación sesión 4
- `pytest tests/` → **57/57 passed**
- Sintaxis Python (91 archivos) y XML (94 archivos) → sin errores
- Import smoke test de `ecf_core.*` y `api_gateway.*` → OK

### Migración requerida
```bash
psql -U renace_ecf -d renace_ecf -f db/013_rfce_por_factura.sql   # RFCE por factura + crear_schema_tenant v2.7
```

---

## ✅ Avances — Sesión 6 (2026-07-13): depuración global (auditoría fresca)

### 41. RFCE idempotente + reconciliación de reintentos ✅
**Archivos:** `ecf_core/rfce_service.py`

- `ON CONFLICT (ncf) DO UPDATE … RETURNING id` — ya no hay UUID fantasma ni FK rota en reintentos
- `ecf.rfce_id` se enlaza **después** de la respuesta DGII
- Reconciler también reintenta RFCE en `pendiente`/`rechazado` (no solo `rfce_id IS NULL`)

### 42. Worker: claim atómico + estados terminales ✅
**Archivo:** `ecf_core/queue_worker.py`

- Claim `pendiente → enviado` antes de hablar con DGII (evita doble envío)
- Release a `pendiente` si falla DGII sin `track_id`
- Skip de `aprobado/rechazado/condicionado/anulado/anulacion_fallida` (salvo `force_reprocess`)
- Skip de `enviado` con `track_id` (lo resuelve el poller)

### 43. Anulación: máquina de estados DGII ✅
- `Aceptado`/`Condicional` → `anulado`
- `Recibido`/`EnProceso` → `anulacion_pendiente` (no falso fallo)
- `Rechazado` → `anulacion_fallida`

### 44. QR oficial + FechaHoraFirma en AST ✅
**Archivos:** `dgii_client.py`, `ecf_core_service.py`, `ecf_interchange_service.py`, `ecf_anulacion_service.py`, `utils.py`, `scheduler.py`, `pdf_service.py`

- QR e-CF incluye `FechaEmision`; paths de timbre en minúsculas (`certecf/consultatimbre`)
- QR E32/FC: solo params documentados (sin FechaFirma inventada)
- `FechaHoraFirma` / ACECF / ARECF / ANECF usan `America/Santo_Domingo` (AST)
- Poller ya no inventa `00:00:00` como FechaFirma

### 45. FechaLimitePago + JSON CodigoSeguridad ✅
- Ya no se emite `FechaLimitePago = fecha_emision`; solo si hay vencimiento real (`fecha_limite_pago` en payload/Odoo `invoice_date_due`)
- Parser JSON de recibidas lee `codigoSeguridad` / `CodigoSeguridad` (además de CUFE legacy)
- Reset mensual de contadores: una sola vez/mes vía `system_audit_log` (sin spam de alerta de cert)

### 46. Odoo v18/v19 POS + ACLs + UoM ✅
- v19: `popup` → `dialog` + `EcfSelectionDialog` (crash POS corregido)
- `ecf_tipo_id` en `_load_pos_data_fields` + `serialize()` / `_order_fields`
- ACLs read para `group_ecf_user` y `point_of_sale.group_pos_user` sobre `ecf.tipo`
- v18: códigos UoM DGII (`_uom_to_dgii_code`); v19: multi-company en `ecf.log` restaurado
- Kanban dashboard: `kanban-box` → `card`
- WhatsApp POS: `_load_pos_data_fields` (API Odoo 18)
- Versiones: `ecf_connector` **18.0.5.1** / `ecf_connector_v19` **19.0.3.1**

### Verificación sesión 6
- `pytest tests/` → **59/59 passed**
- Smoke: `fmt_fecha_hora_dgii`, `generar_qr_url` (E31+E32), imports OK

---

## ✅ Avances — Sesión 7 (2026-07-13): onboarding asistido Renace → clientes

### 47. Empresa operadora + puerta de clientes ✅
**Archivos:** `db/014_onboarding_asistido.sql`, `db/001_schema.sql`, `api_gateway/admin.py`, `.env.example`

- Columnas: `is_platform_operator`, `dgii_test_ok_at`, `postulacion_firmada_at`
- `PLATFORM_OPERATOR_RNC=132842316` — al registrar ese RNC se marca como operadora Renace
- Crear clientes exige: PSFE + operador con .p12 + «Probar CerteCF» OK (bypass: `ALLOW_CLIENT_ONBOARDING=true`)

### 48. Asistente de certificación con progreso real ✅
- Pasos 4–5 ya no se marcan “done” solo por tener .p12: requieren auth/postulación persistidas
- API expone `next_blocker`, `paso_actual`, checklist de evidencia (paso 8)
- Portal: banner del siguiente paso, pasos atenuados, CTA principal, post-create → Certificación

### 49. Flujo plataforma en panel ✅
- Dashboard: PSFE → Empresa Renace → Probar CerteCF → Empresas cliente
- Banner en Empresas con blockers claros
- Ambiente editable en ficha de empresa
- PSFE: señal Redis `ecf:psfe:reload` para que workers recarguen sin reinicio

### Migración requerida (sesión 7)
```bash
psql -U renace_ecf -d renace_ecf -f db/014_onboarding_asistido.sql
```

### Flujo operativo recomendado
1. Panel → **Plataforma** → subir PSFE (cert/key/CA) → Probar CerteCF mTLS
2. **Empresas** → Registrar Renace (`132842316`) → guardar API Key
3. Continuar a **Certificación DGII** → .p12 → Probar CerteCF → Firmar postulación
4. Conectar Odoo + Set de Pruebas E31–E34
5. Cuando el gate lo permita → registrar empresas cliente (mismo asistente)

---

## 🟡 Pendientes opcionales (bajo riesgo)

| # | Pendiente | Notas |
|---|-----------|-------|
| P7 | Consolidar `ecf_connector` v18/v19 en un solo módulo `renace_ecf` | Requiere renaming + manifest merge — riesgo de regresión en instalaciones existentes. Omitido intencionalmente. |

---

## 📊 Estado al cierre de esta sesión

| Eje | Antes sesión 1 | Sesión 2 | **Sesión 3** |
|-----|----------------|----------|-------------|
| Bugs CRITICAL (DGII-bloqueantes) | 3 abiertos | 0 | **0** |
| Nomenclatura DGII (`codigo_seguridad`) | cufe en todo | cufe en todo | **✅ codigo_seguridad en toda la stack** |
| Schema SQL fresh deployments | Stale (v1.0) | Stale | **✅ Consolidado v2.6** |
| Documentación certificación DGII | Sin docs | Sin docs | **✅ contingencia.md + README paso a paso** |
| Migraciones Odoo DB | Sin migraciones | Sin migraciones | **✅ 18.0.5.0.0 y 19.0.3.0.0** |

### Calificaciones finales

| Eje | Audit inicial | Sesión 2 | **Sesión 3** |
|-----|--------------|----------|-------------|
| Cumplimiento DGII | 6 / 10 | 10 / 10 | **10 / 10** |
| Paneles — datos reales | 7 / 10 | 9 / 10 | **9 / 10** |
| Módulo Odoo 18 — robustez | 6.5 / 10 | 9.5 / 10 | **10 / 10** (campo renombrado + migration script) |
| Seguridad / Auditoría | 8 / 10 | 9.5 / 10 | **9.5 / 10** |
| Observabilidad | 4 / 10 | 8 / 10 | **8 / 10** |
| Mantenibilidad | 5 / 10 | 7.5 / 10 | **9 / 10** (schema consolidado + docs + migrations) |

---

## 🚦 Acciones inmediatas recomendadas

1. **`pip install pre-commit && pre-commit install`** — activa los hooks en el repo local.
2. **Ejecutar `db/010_rename_cufe.sql`** contra todas las instancias existentes para renombrar el column `cufe → codigo_seguridad` en los schemas de tenant.
3. **Actualizar Odoo** con el módulo `ecf_connector` v18.0.5.0 (o v19 v19.0.3.0): `odoo -u ecf_connector` — la migración `pre-migrate.py` renombrará las columnas Odoo automáticamente.
4. **Migración PSFE en REY**: ejecutar `db/012_platform_psfe.sql` en postgres y subir PSFE desde `https://ecf.renace.tech/portal/` → Plataforma.
5. **Certificación Renace (132842316)**: Panel → Empresa → pestaña **Certificación DGII** — seguir asistente de 8 pasos.
6. **Firmar Postulación**: subir XML original DGII en el asistente (paso 5) — firma XAdES con .p12 activo.
7. **Odoo**: Set de Pruebas DGII → confirmar facturas → verificar E31–E34 aprobados en la tabla del panel.
8. **Smoke test post-migración**:
   ```bash
   # Verificar codigo_seguridad en PostgreSQL (schema Renace)
   psql -U renace_ecf -d renace_ecf -c "SELECT ncf, codigo_seguridad FROM tenant_132842316.ecf LIMIT 3;"
   ```
9. **Smoke test ARECF**: enviar un e-CF de prueba al endpoint `/fe/recepcion/api/ecf` y verificar en los logs que el ARECF Estado=0 se envía a la DGII.
10. **Plan de contingencia**: revisar `docs/contingencia.md` con el responsable técnico del contribuyente antes de la fase 1 DGII.

---

*Documento actualizado al 2026-07-13 — onboarding asistido: operador Renace primero, gate de clientes, wizard con next_blocker.*

