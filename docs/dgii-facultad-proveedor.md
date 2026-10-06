# Facultad DGII: proveedor de software e-CF y registro de clientes

Documento operativo para que la empresa quede autorizada a comercializar facturación electrónica y, a partir de ahí, incorpore clientes.

Fuentes (publicaciones informativas DGII, sin validez legal por declaración de la propia DGII):

- Catálogo de servicios de Facturación: autorización de emisor y de proveedor.
- Guía básica para ser proveedor de servicios de FE (julio 2025).
- Documentaciones soporte de autorización de proveedores (julio 2025).
- Proceso de certificación para ser emisor electrónico (julio 2025).
- Proceso de certificación de emisor con proveedor ya certificado (mayo 2026).
- Respuesta de la comunidad DGII: los tres primeros clientes pueden certificarse con el software aunque la empresa todavía no sea proveedor certificado.

Norma de fondo: Ley 32-23, Reglamento 587-24, Norma General 01-2020 y Norma General 10-2021.

## Orden real

La DGII no faculta primero al proveedor y después deja registrar clientes. Un proveedor de servicios de facturación electrónica es un **emisor electrónico** al que Impuestos Internos autoriza a **comercializar** el servicio. Sin esa calidad de emisor, el formulario de proveedor no procede.

El camino que sí cierra el objetivo (vender el software y dar de alta clientes) es este:

1. La empresa se certifica como **emisor electrónico** con su propio software.
2. **Tres contribuyentes** se certifican como emisores usando ese software, mientras la empresa aún no es proveedor.
3. Con esos tres emisores ya certificados, la empresa presenta el **FI-GDF-017** y completa la postulación de proveedor.
4. Recién con la autorización de proveedor, los clientes siguientes usan el trámite corto.

```text
Empresa
  └─ FI-GDF-016  →  emisor electrónico (set completo)
        └─ 3 clientes piloto  →  emisores con el software (set completo)
              └─ FI-GDF-017 + anexos  →  proveedor autorizado
                    └─ clientes siguientes  →  emisor con proveedor certificado (solo simulación)
```

## Fase 1 — La empresa como emisor electrónico

Trámite: autorización para ser emisor electrónico. Formulario **FI-GDF-016**, en Oficina Virtual, menú Solicitudes, opción «Solicitud para ser Emisor Electrónico». Gratuito. La DGII responde en el buzón de la Oficina Virtual; el catálogo indica 1 día laborable para esa respuesta. Si es positiva, el mensaje trae el enlace al Portal de Certificación y la clave de ese portal.

Requisitos antes de enviar el FI-GDF-016:

- RNC activo, con obligaciones tributarias a cargo.
- Clave de Oficina Virtual del contribuyente y del representante.
- Alta de NCF (autorizado a emitir comprobantes fiscales, Norma General 06-2018).
- Certificado digital para procedimientos tributarios, emitido por una prestadora de confianza autorizada por INDOTEL (Ley 126-02). Tiene que ser de la persona que actúa como representante, y esa persona tiene que estar vinculada al RNC.
- Software capaz de elaborar, emitir y usar e-CF. En esta fase el tipo de software es **desarrollado de manera interna**.
- Estar al día en obligaciones tributarias.

En el portal, antes de postular, se puede usar el ambiente de **pre-certificación** para integrar. La postulación formal es «Crear Postulación» → «Emisor Electrónico».

Datos del software en el formulario de postulación:

| Campo | Qué indicar |
| --- | --- |
| Tipo de registro | Emisor electrónico |
| Tipo de software | Desarrollado de manera interna |
| Nombre y versión | Nombre comercial del software y la versión que se va a certificar |
| URL recepción | Servicio que recibe e-CF cuando la empresa actúa como receptor |
| URL aprobación | Servicio que recibe aprobaciones o rechazos comerciales |
| URL autenticación | Servicio de semilla y validación de certificado |

El portal descarga un XML. Hay que firmarlo (App Firma Digital de la DGII, u otra herramienta equivalente) y volver a cargarlo. Si la validación pasa, se habilita el set completo:

1. Pruebas de datos: Excel de e-CF, generar los XML en el mismo orden de campos y enviarlos a recepción de certificación. Un rechazo obliga a reiniciar el set.
2. Pruebas de datos de aprobación o rechazo comercial.
3. Pruebas de simulación: e-CF con datos de la operación real, más la representación impresa en PDF (máximo 10 MB).
4. Pruebas de comunicación: la DGII envía e-CF y aprobaciones comerciales a las URL declaradas. Hay que responder acuse de recibo y el estado comercial.
5. URL de producción (autenticación, recepción y aprobación comercial).
6. Declaración jurada en XML, firmada por el representante de la postulación.
7. Verificación final del RNC: al día, Oficina Virtual, alta de NCF y representante registrado.

Servicios del ambiente de certificación que usa ese set:

- Recepción: `https://ecf.dgii.gov.do/CerteCF/Recepcion`
- Consulta de resultado (TrackId): `https://ecf.dgii.gov.do/CerteCF/ConsultaResultado`
- Estados posibles del e-CF: Aceptado, Rechazado, Aceptado condicional, En proceso.

Al cerrar, la Oficina Virtual habilita el menú de facturación electrónica (contingencia, delegación, consulta de emitidos, recibidos y anulados, directorio). Ahí quedan las URL productivas.

## Fase 2 — Tres clientes piloto, antes de ser proveedor

La DGII exige **al menos tres contribuyentes certificados como emisores electrónicos** para autorizar al proveedor. Esos tres pueden certificarse con este software **aunque la empresa todavía no sea proveedor**. Lo confirmó la comunidad de ayuda de la DGII: el cliente entra a Oficina Virtual → Solicitudes → «Solicitud para ser Emisor Electrónico», completa el FI-GDF-016 y certifica el sistema en el portal.

Como el proveedor aún no está en el listado de certificados, el cliente no usa el trámite corto. Usa el **proceso completo de emisor** (el de julio 2025): pruebas de datos, simulación, comunicación, representación impresa y declaración jurada. En el formulario:

- Tipo de software: adquirido a un proveedor externo.
- Nombre y versión: los del software de esta empresa.
- Datos del proveedor: RNC, razón social y contacto de esta empresa, aunque todavía no figure como proveedor certificado.

Cada cliente piloto necesita, por su cuenta: RNC activo, Oficina Virtual, alta de NCF, certificado digital del representante vinculado a su RNC, y estar al día.

Estos tres no son un trámite interno de «dar de alta un tenant». Cada uno tiene que quedar **emisor electrónico autorizado** en la DGII. Sin esos tres cierres, el FI-GDF-017 no cumple el requisito 3 del catálogo.

## Fase 3 — Facultad como proveedor de servicios

Trámite: autorización para ser proveedor de servicios de facturación electrónica. Lo pide quien **ya es emisor electrónico** y quiere comercializar el servicio. Formulario **FI-GDF-017** (versión B en el catálogo de formularios de solicitudes de la DGII). Persona jurídica: firmado y sellado. Gratuito. Plazo de respuesta del catálogo: 10 días laborables. Área: Gerencia de Facturación.

Vía: Centro de Asistencia Presencial de la sede central, o el área de información de la administración local. También por correo, con la documentación escaneada, a `facturacionelectronica@dgii.gov.do`.

Requisitos del catálogo, además de ser emisor y tener los tres clientes certificados:

- RNC con actividad económica de venta y/o desarrollo de aplicaciones informáticas. El listado de actividades aceptadas está en el proceso de certificación de proveedores, en el portal de la DGII.
- Al día en obligaciones formales y sustantivas.
- Bitácora de eventos (logs) con trazabilidad y control de cambios.
- Respaldo y recuperación de la información.
- Página web institucional activa.
- Certificado digital de persona física para procedimientos tributarios (el mismo tipo exigido al emisor).

Anexos, con el nombre que pide la DGII:

| Anexo | Contenido |
| --- | --- |
| 1.1 Política de seguridad | Documento formal vigente de controles de seguridad de la información |
| 1.2 Política de contingencia | Continuidad ante eventos disruptivos. Base interna: `docs/contingencia.md`; hay que presentarlo como política, no solo como runbook técnico |
| 1.3 Protección de datos | URL pública de la política, Ley 172-13. Tiene que estar colgada en la web, no solo adjunta |
| 1.4 Asistencia | Canales de mesa de ayuda y tiempos de respuesta comprometidos |
| 1.5 Contacto DGII | Nombre, cargo, nivel de responsabilidad, correo y teléfono directo de quien atiende requerimientos de la DGII |
| 1.6 Certificación de seguridad | Opcional. ISO 27001 u otra, si existe |

Si el expediente pasa, el buzón de Oficina Virtual trae el enlace al Portal de Certificación. Ahí la postulación es «Crear Postulación» → «Proveedor de Servicios». Se repiten pruebas en pre-certificación y certificación (la Norma 10-2021, art. 9, pide comprobar que el software emite, recibe y almacena e-CF, acuse de recibo y aprobación comercial). Al terminar, declaración jurada firmada. Con eso la DGII entrega la autorización para comercializar.

Hay que mantener anexos, contacto y URL actualizados. La propia hoja de anexos advierte sanciones si esa información queda desactualizada.

### Modelo que corresponde declarar

La guía de julio 2025 distingue dos modelos:

- El certificado y la firma viven en la infraestructura del dueño del certificado.
- El software se ofrece como servicio y la custodia del certificado y de la firma vive en la infraestructura del proveedor.

Este repositorio es el segundo: el `.p12` del contribuyente se sube al portal admin y el gateway firma y envía. Hay que declararlo así. En ese modelo el proveedor debe dar al contribuyente la forma de firmar el e-CF con el certificado de procedimiento tributario del contribuyente, guardar datos fiscales y certificados con mecanismos seguros, y responder por el uso indebido de la información según la Ley 32-23.

### URL que expone este software

El gateway publica los servicios que la postulación pide (rutas en `api_gateway/main.py`):

| Campo del portal | Ruta |
| --- | --- |
| Autenticación | `GET /fe/autenticacion/api/semilla` y `POST /fe/autenticacion/api/validacioncertificado` |
| Recepción | `POST /fe/recepcion/api/ecf` |
| Aprobación comercial | `POST /fe/aprobacioncomercial/api/ecf` |

Tienen que ser HTTPS público, el mismo host en certificación y, al cierre, las URL de producción. El ambiente de certificación de la DGII en este proyecto es `https://ecf.dgii.gov.do/CerteCF` (`ECF_AMBIENTE=certificacion`). El host `ecf-cert.dgii.gov.do` no es el de este cliente.

Antes de la postulación de proveedor conviene cerrar este hueco: `validacioncertificado` comprueba que la semilla salió de este servidor, y no verifica la firma X.509 del XML. La prueba de comunicación de la DGII valida certificado y firma. Sin esa verificación, el endpoint no demuestra la identidad del contribuyente.

## Fase 4 — Clientes siguientes, ya con proveedor certificado

A partir de la autorización, el cliente nuevo no repite el set completo. Sigue el proceso de mayo 2026, «Emisor electrónico con proveedor de servicios de FE certificado»:

1. El cliente cumple RNC, Oficina Virtual (contribuyente y representante), alta de NCF, certificado digital del representante y software contratado.
2. FI-GDF-016 en Oficina Virtual → Solicitudes. Si pasa, recibe usuario y clave del portal.
3. Pre-certificación, incluyendo el resumen de facturas de consumo electrónicas menores de DOP 250,000 (servicio B2C de certificación: `https://fc.dgii.gov.do/certecf/recepcionfc/`).
4. «Crear Postulación» → «Emisor Electrónico», con estos datos:
   - Tipo de registro: emisor electrónico.
   - Tipo de software: proveedor externo.
   - Nombre del software: el contratado con el proveedor certificado.
   - URL de recepción, aprobación y autenticación (las del servicio que opera para ese cliente, o las del proveedor si el modelo es SaaS y el directorio apunta al proveedor).
   - Versión.
   - Datos del proveedor: obligatorios, y tienen que ser los del proveedor **ya certificado**.
5. Generar XML, firmarlo y enviarlo. La DGII valida RNC activo y al día, que el representante coincida con el registro y con el certificado, y que la firma sea íntegra.
6. Si eso pasa, solo se habilitan **pruebas de simulación** (envío de e-CF al servicio de recepción de certificación y, si aplica, el XML íntegro de la factura de consumo menor de DOP 250,000). No se repiten el set de datos ni el set de comunicación: eso ya lo acreditó el proveedor.
7. URL de producción, declaración jurada firmada y verificación de estatus.
8. Autorización de emisor. En Oficina Virtual queda el menú de facturación electrónica y el directorio con las URL productivas.

Dar de alta al cliente en este software (tenant, API key, `.p12`, secuencias NCF) es un paso interno. No sustituye el FI-GDF-016 ni la postulación del cliente. Sin esa postulación, el cliente no puede emitir en producción.

## Checklist de salida

- [ ] RNC de la empresa con actividad de software y al día.
- [ ] Representante vinculado, con Oficina Virtual y certificado de procedimiento tributario vigente.
- [ ] Alta de NCF de la empresa.
- [ ] FI-GDF-016 de la empresa aprobado y set completo cerrado. La empresa es emisor electrónico.
- [ ] Tres clientes piloto emisores electrónicos, certificados con este software.
- [ ] Web activa, política de datos publicada (Ley 172-13), mesa de ayuda y contacto DGII.
- [ ] Anexos 1.1 a 1.5 listos; 1.6 si existe.
- [ ] FI-GDF-017 firmado y sellado, depositado o enviado a `facturacionelectronica@dgii.gov.do`.
- [ ] Postulación «Proveedor de Servicios» cerrada, con declaración jurada.
- [ ] Autorización de proveedor recibida.
- [ ] A partir de ahí, cada cliente nuevo: FI-GDF-016 + postulación corta (solo simulación) + alta interna del tenant.
