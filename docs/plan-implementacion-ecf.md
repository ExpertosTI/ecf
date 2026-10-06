# Plan de implementación e-CF

Orden único para certificar a Renace, conectar Odoo con la recepción y ver qué punto de venta emitió cada factura. Cada fase se cierra con su criterio de salida. No se abre la siguiente si el criterio anterior falla.

Versión de certificación: **Odoo 18** (`ecf_connector` 18.0.5.0). Odoo 19 se iguala al final, cuando el flujo de la 18 ya pasó el set de la DGII. Certificar las dos a la vez parte el esfuerzo y deja la 19 sin permisos de acceso ni el diálogo de tipo e-CF del POS.

Norma de fondo: Ley 32-23, Reglamento 587-24, Norma General 01-2020 y Norma General 10-2021. El trámite ante la DGII está detallado en `docs/dgii-facultad-proveedor.md`. Este plan es el orden de construcción.

## Lo que hoy no cumple el estándar

| Hueco | Dónde está | Efecto |
| --- | --- | --- |
| El Excel oficial del portal (25 e-CF y 4 resúmenes, 5.215 columnas) no se lee | `scripts/dgii_homologation_set.py` manda 3 facturas inventadas | El paso 2 sigue en 0/21 y 0/4 desde el 13 de julio de 2026. Un XML mal armado reinicia el set. |
| La postulación del 13 de julio llegó sin firma | Mensaje del portal: «no se encontró ninguna firma en el documento» | La firma del representante no la sustituye el gateway. |
| `validacioncertificado` no verifica la firma X.509 | `api_gateway/main.py` | La prueba de comunicación de la DGII valida certificado y firma. |
| El e-CF no guarda el punto de venta | Tabla `tenant_*.ecf` y el payload de `_emitir_ecf` | No se puede ver qué caja mandó qué NCF a la DGII. |
| La recepción existe en dos puntas que no se ven juntas | Gateway `/fe/recepcion/api/ecf` y webhook Odoo `/ecf/webhook/recibida` | Compras recibidas no quedan atadas al mismo tablero que las ventas. |
| Odoo 19 no está a la par | Sin `ir.model.access.csv` y sin diálogo de tipo e-CF en el POS | No se usa para certificar. |

## Fase 0 — Cerrar el expediente de emisor de Renace

Postulación abierta: RNC 132842316, estado «Pruebas de datos e-CF», representante Adderly Marte, tipos 31, 32, 33, 34, 41, 43, 44, 45, 46 y 47.

1. Confirmar RNC al día, alta de NCF (ya completada el 24 de abril de 2026) y certificado de procedimiento tributario vigente del representante.
2. Dejar las URL públicas en HTTPS, las mismas que irán a producción al cierre:
   - Autenticación: `GET /fe/autenticacion/api/semilla` y `POST /fe/autenticacion/api/validacioncertificado`
   - Recepción: `POST /fe/recepcion/api/ecf`
   - Aprobación comercial: `POST /fe/aprobacioncomercial/api/ecf`
3. Verificar la firma X.509 en `validacioncertificado` antes de la prueba de comunicación. Hoy solo se comprueba que la semilla salió de este servidor.

Criterio de salida: el portal sigue en el paso 2 y las tres URL responden en HTTPS.

## Fase 1 — Lector del set oficial, sin enviarlo todavía

El archivo `132842316-….xlsx` tiene hoja `ECF` (25 filas) y hoja `RFCE` (4 filas). El contador del portal es 21 comprobantes aceptados, 4 resúmenes aceptados y 4 facturas de consumo menores de DOP 250.000 cargadas en la pantalla. El lector tiene que clasificar cada fila en uno de esos tres destinos, no mandar las 25 al mismo servicio.

Reglas del estándar, todas obligatorias:

- Generar el XML con los mismos campos y en el mismo orden que la fila del Excel. Un rechazo reinicia el set.
- Firmar con el `.p12` del emisor de esa postulación, no con un certificado de prueba.
- Enviar el e-CF a `https://ecf.dgii.gov.do/CerteCF/Recepcion`.
- Enviar cada resumen de consumo menor de DOP 250.000 a `https://fc.dgii.gov.do/CerteCF/RecepcionFC` y, solo si ese resumen queda aceptado, cargar el XML íntegro en la pantalla «Facturas de consumo < 250Mil».
- Consultar el TrackId en `https://ecf.dgii.gov.do/CerteCF/ConsultaResultado` hasta Aceptado, Rechazado, Aceptado condicional o En proceso.
- Guardar por fila: caso de prueba, e-NCF, destino, TrackId, estado y el XML firmado. Si una fila rechaza, el informe dice cuál y no sigue mandando el resto a ciegas.
- Validar el XML contra el XSD del tipo (`xsd/ECF-31.xsd` y los demás) antes de salir a la red.

El comando es por tenant: Excel de esa postulación, certificado de ese RNC, ambiente `certificacion`. No se dispara contra la postulación viva hasta que un ensayo local deje las 25 filas válidas contra el XSD.

Criterio de salida: informe local con 25 XML válidos y 4 resúmenes válidos, cero envíos rechazados en un ensayo, y recién ahí el envío real al paso 2.

## Fase 2 — Resto del set de emisor, en el orden del portal

El portal no deja saltar pasos. El orden es el de julio 2025:

1. Pruebas de datos de aprobación o rechazo comercial, después de que el paso 2 esté en 21/21 y 4/4.
2. Simulación: e-CF con datos de la operación real, más la representación impresa en PDF (máximo 10 MB) con NCF, código de seguridad de 6 caracteres, QR a `ecf.dgii.gov.do` y la marca del ambiente.
3. Pruebas de comunicación: la DGII envía e-CF y aprobaciones a nuestras URL. Hay que responder acuse de recibo y el estado comercial. Aquí entra la firma X.509 de la fase 0.
4. Declarar las URL de producción.
5. Declaración jurada en XML, firmada por el representante de la postulación. El gateway prepara el archivo. La firma la hace el representante.
6. Verificación final del RNC: al día, Oficina Virtual, alta de NCF y representante registrado.

Criterio de salida: la Oficina Virtual muestra el menú de facturación electrónica (contingencia, delegación, emitidos, recibidos, anulados, directorio). Renace es emisor electrónico. Hasta ese menú no se presenta el FI-GDF-017.

## Fase 3 — Tres clientes piloto

Cada piloto es un contribuyente distinto, con su RNC, su Oficina Virtual, su alta de NCF y su certificado. Crear el tenant no los certifica.

Por cada uno, en este orden:

1. El cliente presenta el FI-GDF-016. Tipo de software: proveedor externo. Datos del proveedor: RNC 132842316, aunque Renace todavía no esté en el listado de proveedores.
2. Nosotros creamos `tenant_{rnc}`, subimos su `.p12`, dejamos el ambiente en `certificacion` y las secuencias de los 10 tipos.
3. Corre el mismo lector de la fase 1 con el Excel de **su** postulación.
4. Cierran simulación, comunicación, URL de producción y declaración jurada.
5. En su Odoo 18: URL del gateway, API key y secreto de webhook. Emisión automática apagada hasta que el primer e-CF de prueba vuelva Aceptado.

Los tres pueden ir en paralelo. El FI-GDF-017 exige los tres ya autorizados como emisores.

Criterio de salida: tres RNC distintos con menú de facturación electrónica activo en su Oficina Virtual.

## Fase 4 — Facultad de proveedor

Recién aquí va el FI-GDF-017, firmado y sellado, a `facturacionelectronica@dgii.gov.do` o en oficina. Plazo del catálogo: 10 días laborables para la respuesta.

Anexos, con el nombre que pide la DGII:

| Anexo | Qué tiene que existir antes de enviarlo |
| --- | --- |
| 1.1 Política de seguridad | Documento formal vigente |
| 1.2 Política de contingencia | `docs/contingencia.md` pasado a política, no solo runbook |
| 1.3 Protección de datos | URL pública, Ley 172-13, colgada en la web |
| 1.4 Asistencia | Canales y tiempos de respuesta |
| 1.5 Contacto DGII | Nombre, cargo, correo y teléfono directo |
| 1.6 | Solo si hay ISO 27001 u otra certificación |

Modelo a declarar: software como servicio. El certificado del cliente se custodia en nuestra infraestructura.

Después del enlace del buzón: postulación «Proveedor de Servicios», pruebas del portal y declaración jurada.

Criterio de salida: autorización de proveedor. A partir de ahí un cliente nuevo no repite las 25 filas. Su portal solo abre simulación, URL de producción y declaración jurada.

## Fase 5 — Alta de un cliente ya con proveedor certificado

Un solo alta, en este orden. Lo que es juramento del cliente no se automatiza.

1. El cliente presenta el FI-GDF-016 y firma la postulación.
2. Comando de alta: tenant, `.p12`, secuencias, ambiente `certificacion`, compañía de Odoo con URL, API key y webhook.
3. Comando de simulación: los e-CF de prueba que pida esa postulación, firmados con el certificado de ese tenant, con informe de TrackId.
4. El cliente firma la declaración jurada.
5. Se cambian las URL a producción y el ambiente del tenant a `produccion`.
6. Primera factura real. Se confirma en el portal de la DGII y en la factura de Odoo.

Criterio de salida: un e-CF de producción Aceptado, visible en Odoo con NCF, código de seguridad y QR.

## Fase 6 — De qué punto de venta sale cada factura

Hoy `pos.order` guarda `ecf_tipo_id`, `ecf_ncf`, código de seguridad y QR, y al facturar llama a `_emitir_ecf`. El payload solo manda `odoo_move_id` y `odoo_move_name`. La tabla `ecf` no tiene caja ni sesión. Por eso no se puede filtrar «qué mandó la caja 2».

Datos que tienen que viajar juntos, de la orden POS a la factura, al gateway y de vuelta por el webhook:

| Dato | Origen |
| --- | --- |
| Punto de venta | `pos.config` (nombre y id) |
| Sesión | `pos.session` |
| Orden | `pos.order` |
| Factura | `account.move` |
| NCF, TrackId, estado DGII | Respuesta del gateway |

En Odoo 18:

- Lista de facturas y lista de órdenes POS con columnas NCF, estado, punto de venta y sesión.
- Filtro y agrupación por punto de venta y por estado (aprobado, rechazado, condicionado, pendiente).
- En el ticket, el nombre del punto de venta junto al NCF y al QR.
- Tablero: conteo del día por caja (emitidas, aceptadas, rechazadas, monto).

En el panel admin (`portal_admin/index.html`):

- Por tenant, la misma tabla: caja, sesión, NCF, tipo, estado, hora de envío, TrackId.
- Un tenant no ve las cajas de otro. El esquema sigue siendo `tenant_{rnc}`.

La factura hecha en Contabilidad, sin POS, queda con origen «Facturación», no con una caja inventada.

Criterio de salida: desde el tablero se abre una caja y se ven solo sus NCF, y el mismo NCF aparece en la orden, en la factura y en el gateway.

## Fase 7 — Recepción de facturas de proveedores

El gateway ya recibe en `/fe/recepcion/api/ecf` y Odoo ya tiene `ecf.compra.recibida` más el webhook `/ecf/webhook/recibida`. Hay que cerrar el circuito para que una compra no se quede solo en un lado.

1. Al recibir un e-CF, el gateway lo guarda en el esquema del tenant receptor, responde el acuse de recibo en el formato de la DGII y avisa a Odoo por el webhook, con HMAC y el RNC del tenant.
2. Odoo crea `ecf.compra.recibida` una sola vez (si el e-NCF ya existe, no duplica) y deja la factura de proveedor en borrador para confirmar.
3. La pantalla de compras muestra estado del acuse, estado comercial (aprobado o rechazado) y el enlace a la factura de proveedor.
4. Aprobar o rechazar en Odoo arma el ACECF, lo firma y lo manda a la URL de aprobación del emisor. El gateway registra ese envío.
5. El tablero de la fase 6 tiene una segunda franja: recibidas por día, pendientes de acuse y pendientes de aprobación comercial. No se mezclan con las ventas de la caja.

Criterio de salida: un e-CF de prueba recibido por la URL pública aparece en Odoo en menos de un minuto, con acuse enviado, y la aprobación comercial hecha en Odoo queda registrada en el gateway.

## Fase 8 — Aspecto visual

Se toca la pantalla cuando el dato de las fases 6 y 7 ya existe. Si no, el diseño muestra ceros.

Panel admin:

- Inicio del tenant: cuatro cifras (aceptadas hoy, rechazadas, pendientes, recibidas sin acuse) y la lista de cajas.
- Lista de e-CF con estado en color fijo: aceptado, rechazado, condicionado, pendiente. Sin gradientes nuevos. La paleta que ya usa el panel se queda.
- Ficha del e-CF: XML no se muestra entero por defecto. Se muestran NCF, caja, sesión, TrackId, código de seguridad y el botón del PDF.
- Alta de cliente: un asistente de los seis pasos de la fase 5, con el paso en el que va cada tenant. El paso de la firma del cliente queda marcado «pendiente del representante», no como si lo pudiéramos cerrar nosotros.

Odoo 18:

- Las listas de facturas, POS y compras usan el mismo nombre de estado.
- El formulario de factura deja el banner de estado que ya tiene y suma origen (caja o facturación) y TrackId.
- El POS muestra el estado del último e-CF de esa orden antes de imprimir el ticket. Si la DGII rechazó, el cajero lo ve ahí, no en un log.

Odoo 19, solo después de que la 18 cierre la fase 6 y la 7:

- Copiar permisos (`ir.model.access.csv`), el diálogo de tipo e-CF y las columnas de caja.
- No se certifica un cliente en la 19 hasta que esas pantallas muestren el mismo NCF que la 18.

## Orden de construcción

```text
0  URL públicas y firma X.509 de la semilla
1  Lector del Excel, validado en local, luego envío real del paso 2
2  Resto del set de Renace hasta ser emisor
3  Tres pilotos con el mismo lector
4  FI-GDF-017 y autorización de proveedor
5  Alta corta del cliente siguiente
6  Punto de venta en el e-CF, en Odoo y en el panel
7  Recepción conectada al webhook y al tablero
8  Pantallas encima de esos datos
9  Igualar Odoo 19
```

No se redibuja el panel antes de la fase 6. No se manda el Excel a la DGII antes de que el XSD local pase. No se abre el FI-GDF-017 antes de que Renace y los tres pilotos tengan el menú de facturación electrónica.
