# Renace ECF: despliegue en `ecf.renace.tech` y refactor de `ecf_connector`

**Fecha:** 2026-03-31

## Objetivo

Preparar el SaaS ECF para desplegarse en `ecf.renace.tech` usando **Portainer sobre Docker Swarm**, y definir el refactor del módulo Odoo existente `ecf_connector` para que cubra mejor el flujo e-CF de DGII sin crear un módulo separado.

## Decisiones aprobadas

- Se usará **Portainer + Docker Swarm** como entorno de despliegue.
- El dominio público principal del SaaS será **`ecf.renace.tech`**.
- Se preparará un **stack para Portainer** y un **script `sh` de despliegue/apoyo**.
- Se **refactorizará `ecf_connector`** en vez de crear un módulo nuevo.
- `portal_admin` queda **fuera de la primera fase**, porque no está presente en el repositorio actual.
- La configuración de integración en Odoo se moverá a **nivel compañía**, expuesta vía `res.config.settings`.

## Restricciones y supuestos

- Las credenciales reales de producción no deben quedar hardcodeadas en el repositorio.
- El despliegue debe ser compatible con Portainer y semántica de Swarm.
- El backend SaaS sigue siendo la pieza que habla con DGII; Odoo no debe integrarse directamente con DGII.
- El sistema actual tiene deuda técnica funcional en backend, worker, despliegue y módulo Odoo, por lo que esta spec prioriza una primera fase operable y mantenible.

## Diseño del despliegue Swarm/Portainer

## Artefactos

Se crearán o adaptarán estos artefactos:

- `docker-stack.portainer.yml`
  - stack compatible con Portainer/Swarm
  - sin `build:` locales como mecanismo principal de despliegue
  - usando imágenes publicadas en un registry
- `deploy_portainer.sh`
  - validación de variables y secretos
  - build/tag/push de imágenes
  - ayuda para despliegue y verificación
- `.env.portainer.example`
  - variables no sensibles y placeholders de configuración

## Servicios

### `api`

- expuesto públicamente vía Traefik
- rutas esperadas:
  - `/v1/*`
  - `/health`
- despliegue recomendado:
  - 2 réplicas

### `worker`

- no expuesto públicamente
- consume Redis y procesa emisión/consulta DGII
- despliegue recomendado:
  - 2 réplicas

### `scheduler`

- ejecuta tareas periódicas
- despliegue recomendado:
  - 1 réplica

### `postgres`

- 1 réplica
- volumen persistente
- preferible con placement controlado si el clúster tiene más de un nodo

### `redis`

- 1 réplica
- persistencia habilitada
- protegido por contraseña

### `portal_admin`

- fuera de fase 1
- no debe bloquear el despliegue inicial

## Redes

- `traefik-public`
  - red overlay externa compartida con Traefik si ya existe en la infraestructura Renace
- `ecf-private`
  - red overlay privada del stack

Solo `api` quedará expuesto. `worker`, `scheduler`, `postgres` y `redis` serán internos.

## Traefik

Se asume como opción preferida un **Traefik compartido** ya gestionado en la infraestructura Swarm. Si no existe, deberá haber un stack de borde separado.

Requisitos mínimos:

- TLS activo para `ecf.renace.tech`
- redirección HTTP a HTTPS
- headers de seguridad
- logs de acceso
- health routing para `/health`

## Secretos y configuración

Se usarán **Docker secrets** para credenciales sensibles. Variables sugeridas:

- `db_password`
- `redis_password`
- `vault_master_key`
- `psfe_cert_b64`
- `psfe_key_b64`
- `dgii_ca_b64`
- credenciales SMTP si aplican
- secretos webhook si aplican

La aplicación deberá aceptar el patrón `VAR` o `VAR_FILE` para poder leer secretos desde `/run/secrets/...`.

## Estrategia de imágenes

Para Swarm/Portainer, el stack debe consumir imágenes ya publicadas, por ejemplo:

- `registry/saas-ecf-api:<tag>`
- `registry/saas-ecf-worker:<tag>`
- `registry/saas-ecf-scheduler:<tag>`

Si se reutiliza una sola imagen con distintos comandos, también es aceptable, siempre que el stack no dependa de `build:` en tiempo de despliegue.

## Inicialización de base de datos

El montaje actual de `./db/001_schema.sql` es frágil para Swarm. Se define como dirección preferida:

- migración/bootstrap explícito en despliegue

Opciones válidas:

- job de bootstrap/migración
- imagen de Postgres con init embebido

Se recomienda el **job de bootstrap/migración** para desacoplar el esquema del contenedor de Postgres.

## Flujo operativo

1. Odoo envía factura al SaaS en `https://ecf.renace.tech/v1/ecf/emitir`
2. API valida, asigna NCF, persiste y encola
3. Worker genera XML, valida, firma y envía a DGII
4. Worker actualiza estado y notifica por webhook a Odoo
5. Odoo actualiza factura, trazabilidad y evidencias

## Operación mínima esperada

- despliegue repetible desde Portainer
- secretos fuera del repo
- API detrás de TLS
- PostgreSQL y Redis persistentes
- backups externos o programados
- logs de acceso y de aplicación disponibles para auditoría

## Diseño del refactor de `ecf_connector`

## Objetivos funcionales

- mantener el módulo actual como base
- corregir roturas estructurales
- mejorar onboarding, trazabilidad y operación
- soportar mejor el flujo real SaaS -> DGII -> Odoo

## Saneamiento obligatorio

### Estructura del módulo

- agregar/alinear `__init__.py` necesarios
- asegurar carga correcta de `models`, `controllers`, `wizard`, `data`, `security`
- reconciliar `__manifest__.py` con los archivos reales del módulo

### Acciones y referencias rotas

- corregir referencias a modelos o vistas inexistentes
- implementar el wizard real de anulación si la acción sigue apuntando a `ecf.anular.wizard`
- asegurar que menús, acciones y vistas existen y cargan

### Seguridad base

- ACL y reglas mínimas para modelos del módulo
- data semilla para tipos e-CF, menús y acciones

## Configuración por compañía

La configuración dejará de vivir solo en `ir.config_parameter` global y pasará a `res.company`, expuesta mediante `res.config.settings`.

Campos esperados:

- URL del SaaS
- API key
- webhook secret
- ambiente
- emisión automática
- consulta automática de estado
- intervalo de consulta

### Validaciones

- no asumir que la API key tenga 64 caracteres
- validar presencia, formato y coherencia de campos críticos
- validar RNC de la compañía cuando aplique

## Onboarding y configuración

### Wizard de primera configuración

Debe guiar al usuario para:

- validar RNC de compañía
- registrar URL del SaaS
- registrar API key
- registrar webhook secret
- activar emisión automática si procede
- verificar conectividad con el SaaS

### Prueba de conexión

Debe existir un botón o acción para probar conexión autenticada con el SaaS.

## Flujo de emisión

### Prevalidación antes de enviar

Antes del envío al SaaS, el módulo debe validar:

- tipo de e-CF
- RNC/cédula del cliente cuando aplique
- líneas válidas
- impuestos coherentes
- moneda y tasa de cambio
- NCF de referencia en notas de crédito/débito

### Emisión automática y manual

Se conservarán ambos modos:

- automática al confirmar factura
- manual desde botón

### Sugerencia de tipo e-CF

El módulo podrá sugerir un tipo por defecto según contexto, permitiendo override manual.

## Estado, callbacks y evidencias

### Consulta de estado

La consulta manual debe actualizar:

- estado en factura
- log asociado
- mensaje útil para usuario

### Webhook

El callback debe endurecerse con:

- idempotencia
- manejo coherente de estados
- registro mínimo del evento recibido
- tratamiento de callbacks huérfanos o inconsistentes

### Evidencias

La factura debe exponer o facilitar acceso a:

- NCF
- CUFE
- QR
- error DGII si existe
- XML firmado descargable desde el SaaS

## Trazabilidad

El modelo `ecf.log` se reforzará para registrar, al menos:

- operación
- estado
- timestamp
- usuario si aplica
- NCF
- CUFE
- error
- intento
- referencia al documento origen
- resumen de respuesta

Se mejorarán vistas, filtros y búsqueda por:

- estado
- tipo e-CF
- fecha
- NCF
- error

## Automatización en Odoo

### Cron de consulta

Se añadirá tarea programada para refrescar documentos en estados no finales.

### Limpieza de logs

Se añadirá retención configurable de logs.

### Dashboard básico

Se implementará un dashboard simple con vistas estándar Odoo para mostrar:

- enviados
- aprobados
- rechazados
- pendientes
- tasa de éxito
- distribución por tipo e-CF

## Contratos que el SaaS debe sostener

Para que el módulo Odoo quede completo, el backend debe exponer o estabilizar:

- endpoint autenticado para prueba de conexión
- endpoint confiable de consulta de estado
- endpoint para descarga de XML firmado
- endpoint de anulación end-to-end
- webhook con firma HMAC consistente e idempotencia lógica

## Fuera de alcance en fase 1

- `portal_admin`
- UI avanzada tipo portal/demo del módulo de referencia
- integración directa Odoo -> DGII
- BI avanzada o frontend pesado

## Riesgos y preguntas abiertas

- confirmar registry donde vivirán las imágenes del stack
- confirmar si Traefik ya existe como stack compartido en Swarm
- confirmar estrategia real de backups y restauración
- validar qué cambios backend son obligatorios antes de considerar listo el refactor Odoo
- revisar brechas actuales del worker y del esquema de datos para asegurar que el contrato con Odoo sea estable

## Criterios de aceptación del diseño

Se considerará cumplida esta fase de diseño cuando exista:

- spec aprobada por el usuario
- ruta clara para stack Portainer/Swarm de `ecf.renace.tech`
- alcance aprobado del refactor de `ecf_connector`
- lista de dependencias del módulo Odoo respecto al backend SaaS
- base suficiente para pasar a un plan de implementación por fases
