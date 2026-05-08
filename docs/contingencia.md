# Plan de Contingencia — Renace e-CF

> Requerido por DGII RD para la fase 1 de certificación (Norma General 06-2018 y sus modificaciones).
> Versión: 1.0 — 2026-04-30

---

## 1. Objetivo

Garantizar la continuidad de la facturación electrónica ante fallos del sistema Renace e-CF o del portal DGII, minimizando el impacto operativo en el contribuyente.

---

## 2. Escenarios de contingencia

### 2.1 Fallo del servicio Renace e-CF (API Gateway / Worker)

**Síntoma:** Los e-CF no se generan, los endpoints devuelven 5xx o no responden.

**Procedimiento:**

1. **Detección automática** — Prometheus alerta en < 2 min si `up{job="ecf-api"}` = 0 o si la tasa de errores 5xx supera el 10% en 5 min.
2. **Modo diferido en Odoo** — Configurar `ecf_modo = 'diferido'` en la compañía de Odoo. Los documentos de venta se generan sin NCF y quedan en cola.
3. **Verificar logs**: `docker compose logs --tail=200 ecf-api`
4. **Reiniciar servicio**: `docker compose restart ecf-api ecf-worker`
5. **Si el fallo persiste > 15 min**: escalar al equipo técnico de Renace e-CF (ver §6).
6. **Recuperación**: Una vez restaurado el servicio, los e-CF diferidos se procesan automáticamente por el scheduler (`ecf_core/scheduler.py`) en el próximo ciclo (máx. 5 min).

**Tiempo máximo de recuperación (RTO):** 30 minutos.

---

### 2.2 Fallo de conectividad con la DGII

**Síntoma:** El worker devuelve errores de red al intentar enviar a `ecf.dgii.gov.do`.

**Procedimiento:**

1. Verificar status oficial DGII: `https://ecf.dgii.gov.do/` o el canal de comunicaciones DGII.
2. Los e-CF en estado `pendiente` se reintentarán automáticamente cada 5 minutos (máx. 3 intentos antes de pasar a DLQ).
3. Ampliar el período de reintentos si la DGII anuncia mantenimiento prolongado:
   ```
   # docker-compose.yml → ECF_RETRY_DELAY_SECONDS=300
   ```
4. Documentar el período de indisponibilidad (fecha/hora inicio y fin) para presentar a la DGII si se requiere justificación de NCFs emitidos fuera de plazo.
5. Una vez restaurada la conectividad, vaciar el DLQ manualmente:
   ```
   curl -X POST http://localhost:8000/v1/admin/queue/retry-dlq \
     -H "Authorization: Bearer $ADMIN_API_KEY"
   ```

**Tiempo máximo de recuperación:** Depende del tiempo de restauración del servicio DGII (exógeno). El sistema reanuda automáticamente.

---

### 2.3 Vencimiento del certificado digital (.p12)

**Síntoma:** Los e-CF se rechazan con error de firma; alerta de cert próximo a vencer (30 días antes).

**Procedimiento:**

1. **Renovación anticipada** — Solicitar nuevo certificado a la DGII 30 días antes del vencimiento. El campo `cert_vencimiento` en la tabla `tenants` y el email de alerta automático (`cert_alerta_enviada`) facilitan el seguimiento.
2. Cargar el nuevo `.p12` via API:
   ```
   POST /v1/admin/tenants/{id}/certificate
   Content-Type: multipart/form-data
   ```
3. Validar el nuevo certificado en ambiente `certificacion` antes de usarlo en `produccion`.
4. En caso de vencimiento imprevisto, los e-CF quedarán en estado `rechazado` con el error de firma. Corregir el certificado y reenviar desde el DLQ.

---

### 2.4 Fallo de base de datos PostgreSQL

**Síntoma:** Errores de conexión a la DB; los endpoints devuelven 503.

**Procedimiento:**

1. Verificar estado: `docker compose ps db`
2. Revisar logs de PG: `docker compose logs db | tail -50`
3. Intentar reinicio controlado: `docker compose restart db`
4. Si hay corrupción de datos, restaurar desde el último backup:
   ```
   pg_restore -Fc -d renace_ecf backup_YYYYMMDD.dump
   ```
5. **Frecuencia de backups recomendada:** diaria (automática via `scripts/backup.sh`) + antes de cada migración.

**Punto de recuperación (RPO):** < 24 horas (backup diario). Para RPO < 1 hora, configurar WAL streaming o un réplica en standby.

---

### 2.5 Compromiso de la API Key de un tenant

**Síntoma:** Uso anómalo detectado en métricas (`ecf_total` con picos inusuales) o reporte del tenant.

**Procedimiento:**

1. Rotar la API Key inmediatamente:
   ```
   POST /v1/admin/tenants/{id}/rotate-api-key
   Authorization: Bearer $ADMIN_API_KEY
   ```
2. Notificar al tenant para que actualice su integración Odoo.
3. Revisar logs del período comprometido en `public.system_audit_log`.
4. Si hay e-CF fraudulentos emitidos, notificar a la DGII según el procedimiento de anulación masiva.

---

### 2.6 Rotación de webhook secret durante ventana de mantenimiento

El sistema implementa una ventana de gracia de 15 minutos (configurable via `WEBHOOK_ROTATION_TTL`):

```
POST /v1/admin/tenants/{id}/rotate-webhook
→ Devuelve nuevo secret + ttl_segundos
→ Durante ttl_segundos, el sistema acepta tanto el secret anterior como el nuevo
```

Actualizar el secret en Odoo (`Configuración → e-CF → Webhook Secret`) dentro del TTL.

---

## 3. Modo de operación sin conectividad DGII (Contingencia DGII)

Según la normativa DGII, cuando el portal DGII no esté disponible:

1. Emitir documentos en **modo diferido** (`indicador_envio_diferido = 1`).
2. Conservar los XML firmados localmente.
3. Enviar a la DGII dentro de las **72 horas** siguientes a la restauración del servicio.
4. El scheduler de Renace e-CF procesará automáticamente los pendientes cuando se restaure la conectividad.

**Nota:** El modo diferido está implementado en el campo `indicador_envio_diferido` de la tabla `ecf` y en el campo `ecf_modo` de `account.move` en Odoo.

---

## 4. Matriz de contactos de escalamiento

| Nivel | Responsable | Medio | SLA respuesta |
|-------|-------------|-------|---------------|
| L1 | Soporte Renace e-CF | support@renace.do | 4 h hábiles |
| L2 | Equipo técnico Renace | tech@renace.do | 2 h hábiles |
| L3 | DGII Centro de Atención | 809-689-3444 | Según DGII |

---

## 5. Checklist pre-certificación DGII

- [ ] Certificado digital válido cargado en ambiente `certificacion`
- [ ] Al menos 1 e-CF de prueba por cada tipo (31, 32, 33, 41, 44, 45, 46) aprobado en TesteCF
- [ ] Métricas Prometheus configuradas y monitoreadas
- [ ] Backup diario configurado y probado (restauración de prueba)
- [ ] Alertas de vencimiento de certificado activas
- [ ] Webhook configurado y probado en Odoo
- [ ] Procedimiento de anulación probado en TesteCF
- [ ] Este plan de contingencia aprobado por el responsable técnico del contribuyente

---

## 6. Historial de revisiones

| Versión | Fecha | Cambios |
|---------|-------|---------|
| 1.0 | 2026-04-30 | Documento inicial para fase 1 de certificación |
