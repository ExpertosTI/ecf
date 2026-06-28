"""
Cliente DGII — Envío de e-CF y consulta de estados
Implementa autenticación por semilla (seed), firma del seed, token Bearer,
y envío con los endpoints reales de la DGII.

Flujo de autenticación DGII:
1. GET  semilla                               → XML seed
2. Firmar el XML seed con .p12 del tenant
3. POST semilla firmada                       → access token
4. Usar Authorization: Bearer {token} en los envíos posteriores

Referencia: https://github.com/victors1681/dgii-ecf
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from lxml import etree

logger = logging.getLogger(__name__)


class EstadoDGII(str, Enum):  # noqa: UP042 — usa str-Enum por compat con json.dumps
    ACEPTADO     = "Aceptado"
    RECHAZADO    = "Rechazado"
    CONDICIONADO = "AceptadoCondicional"
    PROCESANDO   = "EnProceso"
    RECIBIDO     = "Recibido"


@dataclass
class RespuestaDGII:
    estado:         EstadoDGII
    track_id:       str | None
    mensaje:        str
    codigo_seguridad: str | None
    qr_code:        str | None
    detalles:       list[dict]
    raw:            dict


class DGIIClientError(Exception):
    pass


class DGIIClient:
    """
    Cliente HTTP para la API de Facturación Electrónica de la DGII.
    Soporta ambientes: TesteCF, CerteCF, eCF (producción).

    Autenticación: Semilla firmada → Token Bearer
    """

    # Base URLs por ambiente
    URLS = {
        "TesteCF":       "https://ecf.dgii.gov.do/TesteCF",
        "CerteCF":       "https://ecf.dgii.gov.do/CerteCF",
        "eCF":           "https://ecf.dgii.gov.do/eCF",
        # Aliases para BackCompat
        "certificacion": "https://ecf.dgii.gov.do/CerteCF",
        "produccion":    "https://ecf.dgii.gov.do/eCF",
        "pruebas":       "https://ecf.dgii.gov.do/TesteCF",
        "simulacion":    "http://127.0.0.1:9999/mock_dgii", # Mock server local
    }

    # Endpoints de la API DGII
    EP_SEMILLA = (
        "/Autenticacion/api/Autenticacion/Semilla",
        "/fe/autenticacion/api/semilla",
    )
    EP_VALIDACION_CERT = (
        "/Autenticacion/api/Autenticacion/ValidarSemilla",
        "/fe/autenticacion/api/validacioncertificado",
    )
    EP_RECEPCION        = "/fe/recepcion/api/ecf"
    EP_CONSULTA_RESULT  = "/fe/recepcion/api/consultaresultado/{track_id}"
    EP_CONSULTA_TIMBRE  = "/fe/consultas/api/consultatimbre"
    EP_ANULACION_RANGO  = "/fe/anulacion/api/anulacionrangos"
    EP_DIR_RECEPTORES   = "/fe/consultas/api/directorioreceptores"

    # Token cache
    _access_token: str | None = None
    _token_expires_at: float = 0

    def __init__(self, ambiente: str = "certificacion"):
        if ambiente not in self.URLS:
            raise ValueError(f"Ambiente inválido: {ambiente}. Válidos: {list(self.URLS)}")
        self.base_url = self.URLS[ambiente]
        self._client: httpx.AsyncClient | None = None
        self._p12_data: bytes | None = None
        self._p12_password: bytes | None = None
        # Lock disponible incluso si el cliente se usa sin `async with` (tests).
        self._token_lock = asyncio.Lock()

    def set_certificate(self, p12_data: bytes, p12_password: bytes):
        """Configura el certificado .p12 para autenticación."""
        self._p12_data = p12_data
        self._p12_password = p12_password

    async def __aenter__(self):
        import os
        import ssl
        import tempfile

        self._tmp_files = []  # Track all temp files for guaranteed cleanup

        # --- mTLS: configurar certificado de cliente y CA de DGII ---
        ssl_context = None
        psfe_cert_b64 = os.environ.get("PSFE_CERT_B64", "")
        psfe_key_b64 = os.environ.get("PSFE_KEY_B64", "")
        dgii_ca_b64 = os.environ.get("DGII_CA_B64", "")

        try:
            from ecf_core.platform_config import get_psfe_credentials

            _psfe = get_psfe_credentials()
            if _psfe.configured:
                psfe_cert_b64, psfe_key_b64, dgii_ca_b64 = _psfe
        except ImportError:
            pass

        try:
            if psfe_cert_b64 and psfe_key_b64:
                ssl_context = ssl.create_default_context()

                # CA de DGII para validar el servidor
                if dgii_ca_b64:
                    ca_tmp = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
                    self._tmp_files.append(ca_tmp.name)
                    ca_tmp.write(base64.b64decode(dgii_ca_b64))
                    ca_tmp.close()
                    ssl_context.load_verify_locations(ca_tmp.name)

                # Certificado de cliente (PSFE) para mTLS
                cert_tmp = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
                self._tmp_files.append(cert_tmp.name)
                cert_tmp.write(base64.b64decode(psfe_cert_b64))
                cert_tmp.close()

                key_tmp = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
                self._tmp_files.append(key_tmp.name)
                key_tmp.write(base64.b64decode(psfe_key_b64))
                key_tmp.close()

                ssl_context.load_cert_chain(cert_tmp.name, key_tmp.name)
                logger.info("mTLS configurado con certificado PSFE para DGII")
            else:
                _sys_ambiente = os.environ.get("ECF_AMBIENTE", "").lower()
                if _sys_ambiente in {"ecf", "produccion"}:
                    raise RuntimeError(
                        "PSFE_CERT_B64/PSFE_KEY_B64 no configurados. "
                        "mTLS es obligatorio en producción (ECF_AMBIENTE=eCF)."
                    )
                logger.warning("PSFE_CERT_B64/PSFE_KEY_B64 no configurados — mTLS deshabilitado (solo válido en pruebas)")
        except Exception:
            self._cleanup_tmp_files()
            raise

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={"Accept": "application/json"},
            verify=ssl_context if ssl_context else True,
        )
        return self

    def _cleanup_tmp_files(self):
        """Elimina todos los archivos temporales de certificados."""
        import os
        for path in getattr(self, '_tmp_files', []):
            try:
                os.unlink(path)
            except OSError:
                pass
        self._tmp_files = []

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()
        self._access_token = None
        self._token_expires_at = 0
        self._cleanup_tmp_files()

    async def probar_conexion_mtls(self) -> dict:
        """GET semilla en CerteCF/eCF — valida mTLS PSFE (Manual Técnico DGII §Autenticación)."""
        if not self._client:
            raise DGIIClientError("Cliente HTTP no inicializado — use async with DGIIClient(...)")
        resp = await self._request_first_available("get", self.EP_SEMILLA)
        ok = resp.status_code == 200 and ("semilla" in resp.text.lower() or "<?xml" in resp.text.lower())
        return {
            "ok": ok,
            "status_code": resp.status_code,
            "base_url": self.base_url,
            "detalle": "Semilla DGII recibida" if ok else (resp.text[:200] if resp.text else "Sin respuesta"),
        }

    async def probar_autenticacion_contribuyente(self) -> dict:
        """Flujo completo semilla → firma → token (requiere .p12 del contribuyente)."""
        await self._authenticate()
        return {
            "ok": True,
            "base_url": self.base_url,
            "mensaje": "Autenticación DGII exitosa — token Bearer obtenido",
        }

    # -------------------------------------------------------
    # Autenticación DGII (Semilla)
    # -------------------------------------------------------

    async def _authenticate(self):
        """
        Flujo de autenticación según la DGII:
        1. GET semilla → recibir XML con valor de semilla
        2. Firmar el XML semilla con el .p12
        3. POST semilla firmada → recibir access token

        Usa asyncio.Lock para evitar race conditions cuando múltiples
        workers intentan refrescar el token simultáneamente.
        """
        if self._access_token and time.time() < self._token_expires_at:
            return  # Token aún válido

        async with self._token_lock:
            # Double-check después de adquirir el lock
            if self._access_token and time.time() < self._token_expires_at:
                return

            if not self._p12_data or not self._p12_password:
                raise DGIIClientError("Certificado .p12 no configurado. Llame set_certificate() primero.")

            logger.info("Autenticando con DGII (flujo semilla)...")

            # Paso 1: Obtener semilla
            resp = await self._request_first_available("get", self.EP_SEMILLA)
            if resp.status_code != 200:
                raise DGIIClientError(f"Error obteniendo semilla DGII: HTTP {resp.status_code} — {resp.text}")

            seed_xml = resp.text

            # Paso 2: Firmar la semilla con el .p12
            signed_seed = self._sign_seed_xml(seed_xml)

            # Paso 3: Validar certificado con semilla firmada
            resp = await self._request_first_available(
                "post",
                self.EP_VALIDACION_CERT,
                content=signed_seed,
                headers={"Content-Type": "application/xml"},
            )
            if resp.status_code != 200:
                raise DGIIClientError(
                    f"Error en validación certificado DGII: HTTP {resp.status_code} — {resp.text}"
                )

            data = resp.json()
            token = data.get("token") or data.get("accessToken") or data.get("access_token")
            if not token:
                raise DGIIClientError(f"No se obtuvo token de DGII. Respuesta: {data}")

            self._access_token = token
            # Tokens DGII típicamente duran 24h, usamos 23h para margen
            self._token_expires_at = time.time() + 82800
            logger.info("Autenticación DGII exitosa. Token válido por ~23h.")

    async def _request_first_available(self, method: str, paths: tuple[str, ...], **kwargs):
        """Try current DGII route variants, falling back when an older/newer path is missing."""
        request = getattr(self._client, method)
        last_response = None
        for path in paths:
            resp = await request(path, **kwargs)
            if resp.status_code != 404:
                return resp
            last_response = resp
            logger.debug("DGII endpoint no disponible para %s %s", method.upper(), path)
        return last_response

    def _sign_seed_xml(self, seed_xml: str) -> bytes:
        """Firma el XML de semilla con el certificado .p12 del tenant."""
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            self._p12_data, self._p12_password
        )

        # Parse el XML de semilla
        parser = etree.XMLParser(remove_blank_text=True)
        root = etree.fromstring(seed_xml.encode("utf-8") if isinstance(seed_xml, str) else seed_xml, parser)

        # Canonicalizar
        output = io.BytesIO()
        root.getroottree().write_c14n(output, exclusive=True)
        xml_c14n = output.getvalue()

        # Digest del documento
        digest = base64.b64encode(hashlib.sha256(xml_c14n).digest()).decode()

        # SignedInfo
        signed_info_xml = f"""<ds:SignedInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
  <ds:CanonicalizationMethod Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"/>
  <ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"/>
  <ds:Reference URI="">
    <ds:Transforms>
      <ds:Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/>
      <ds:Transform Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"/>
    </ds:Transforms>
    <ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>
    <ds:DigestValue>{digest}</ds:DigestValue>
  </ds:Reference>
</ds:SignedInfo>"""

        # Canonicalizar SignedInfo y firmar
        si_el = etree.fromstring(signed_info_xml.encode(), parser)
        si_output = io.BytesIO()
        si_el.getroottree().write_c14n(si_output, exclusive=True)
        si_c14n = si_output.getvalue()

        signature_value = base64.b64encode(
            private_key.sign(si_c14n, padding.PKCS1v15(), hashes.SHA256())
        ).decode()

        cert_b64 = base64.b64encode(
            certificate.public_bytes(serialization.Encoding.DER)
        ).decode()

        issuer = certificate.issuer.rfc4514_string()
        serial = str(certificate.serial_number)

        # Construir Signature completo
        sig_xml = f"""<ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
  {signed_info_xml}
  <ds:SignatureValue>{signature_value}</ds:SignatureValue>
  <ds:KeyInfo>
    <ds:X509Data>
      <ds:X509Certificate>{cert_b64}</ds:X509Certificate>
      <ds:X509IssuerSerial>
        <ds:X509IssuerName>{issuer}</ds:X509IssuerName>
        <ds:X509SerialNumber>{serial}</ds:X509SerialNumber>
      </ds:X509IssuerSerial>
    </ds:X509Data>
  </ds:KeyInfo>
</ds:Signature>"""

        # Insertar Signature en el XML de semilla
        sig_node = etree.fromstring(sig_xml.encode(), parser)
        root.append(sig_node)

        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")

    def _auth_headers(self) -> dict:
        """Headers con Bearer token para requests autenticados."""
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/xml",
            "Accept": "application/json",
        }

    # -------------------------------------------------------
    # Envío de e-CF
    # -------------------------------------------------------

    async def enviar_ecf(
        self,
        xml_firmado: bytes,
        rnc_emisor: str = "",
        tipo_ecf: int = 0,
        ncf: str = "",
    ) -> RespuestaDGII:
        """
        Envía el e-CF firmado a la DGII.
        Retorna un RespuestaDGII con trackId para consulta posterior.
        """
        await self._authenticate()

        last_error = None
        for intento in range(1, 4):
            try:
                if ncf:
                    logger.info("Enviando NCF %s a DGII — intento %d", ncf, intento)
                else:
                    logger.info("Enviando XML a DGII — intento %d", intento)
                resp = await self._client.post(
                    self.EP_RECEPCION,
                    content=xml_firmado,
                    headers=self._auth_headers(),
                )

                if resp.status_code in (200, 201, 202):
                    return self._parsear_respuesta(resp.json())

                if resp.status_code == 401:
                    # Token expirado, re-autenticar
                    self._access_token = None
                    self._token_expires_at = 0
                    await self._authenticate()
                    continue

                if resp.status_code in (429, 503):
                    espera = 2 ** intento
                    logger.warning("DGII respondió %d. Esperando %ds...", resp.status_code, espera)
                    await asyncio.sleep(espera)
                    last_error = f"HTTP {resp.status_code}"
                    continue

                logger.error("DGII rechazó la solicitud: %d %s", resp.status_code, resp.text)
                raise DGIIClientError(f"Error DGII {resp.status_code}: {resp.text}")

            except httpx.TimeoutException:
                espera = 2 ** intento
                logger.warning("Timeout enviando a DGII. Esperando %ds...", espera)
                await asyncio.sleep(espera)
                last_error = "Timeout"
            except httpx.ConnectError as e:
                raise DGIIClientError(f"No se pudo conectar a la DGII: {e}")

        raise DGIIClientError(
            f"Falló el envío a DGII después de 3 intentos. Último error: {last_error}"
        )

    # -------------------------------------------------------
    # Consultas
    # -------------------------------------------------------

    async def consultar_por_track_id(self, track_id: str) -> RespuestaDGII:
        """Consulta el estado de un e-CF por su trackId."""
        await self._authenticate()
        url = self.EP_CONSULTA_RESULT.format(track_id=track_id)
        resp = await self._client.get(url, headers=self._auth_headers())
        if resp.status_code != 200:
            raise DGIIClientError(f"Error consultando trackId {track_id}: HTTP {resp.status_code}")
        return self._parsear_respuesta(resp.json())

    async def consultar_timbre(self, rnc_emisor: str, ncf: str) -> RespuestaDGII:
        """Consulta el timbre/estado de un e-CF por RNC+NCF."""
        await self._authenticate()
        resp = await self._client.get(
            self.EP_CONSULTA_TIMBRE,
            params={"RncEmisor": rnc_emisor, "ENCF": ncf},
            headers=self._auth_headers(),
        )
        if resp.status_code != 200:
            raise DGIIClientError(f"Error consultando timbre: HTTP {resp.status_code}")
        return self._parsear_respuesta(resp.json())

    async def consultar_directorio_receptores(self, rnc: str) -> dict:
        """Verifica si un RNC es receptor habilitado en la DGII."""
        await self._authenticate()
        resp = await self._client.get(
            self.EP_DIR_RECEPTORES,
            params={"RncReceptor": rnc},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    # -------------------------------------------------------
    # Anulación
    # -------------------------------------------------------

    async def anular_ecf(
        self,
        rnc_emisor: str,
        ncf_desde: str,
        ncf_hasta: str,
        tipo_ecf: int = 31,
    ) -> RespuestaDGII:
        """Solicita la anulación de un rango de e-CF.

        Genera y firma el XML ANECF conforme a ``xsd/ANECF.xsd`` con el
        certificado .p12 cargado en este cliente.
        """
        await self._authenticate()

        if not (self._p12_data and self._p12_password):
            raise DGIIClientError("Certificado no configurado para firmar el ANECF")

        # Importación tardía para evitar ciclo (anulacion_service usa ECFSigner)
        from ecf_core.ecf_anulacion_service import ECFAnulacionService, RangoNCF

        cantidad = self._calcular_cantidad_rango(ncf_desde, ncf_hasta)
        servicio = ECFAnulacionService()
        xml_firmado = servicio.generar_y_firmar(
            rnc_emisor=rnc_emisor,
            rangos=[RangoNCF(
                tipo_ecf=tipo_ecf,
                desde=ncf_desde,
                hasta=ncf_hasta,
                cantidad=cantidad,
            )],
            p12_data=self._p12_data,
            p12_password=self._p12_password,
        )

        resp = await self._client.post(
            self.EP_ANULACION_RANGO,
            content=xml_firmado,
            headers={**self._auth_headers(), "Content-Type": "application/xml"},
        )
        if resp.status_code not in (200, 201, 202):
            raise DGIIClientError(f"Error en anulación: HTTP {resp.status_code} — {resp.text}")
        try:
            return self._parsear_respuesta(resp.json())
        except ValueError:
            return RespuestaDGII(
                estado=EstadoDGII.RECIBIDO,
                track_id=None,
                mensaje=resp.text[:500],
                codigo_seguridad=None,
                qr_code=None,
                detalles=[],
                raw={"raw_text": resp.text[:2000]},
            )

    @staticmethod
    def _calcular_cantidad_rango(desde: str, hasta: str) -> int:
        """Cuenta NCFs entre desde/hasta inclusive (asume mismo prefijo)."""
        try:
            n_desde = int(desde[3:])
            n_hasta = int(hasta[3:])
            return max(1, n_hasta - n_desde + 1)
        except (ValueError, IndexError):
            return 1

    # -------------------------------------------------------
    # Parser de respuestas
    # -------------------------------------------------------

    def _parsear_respuesta(self, data: dict) -> RespuestaDGII:
        """Normaliza la respuesta JSON de la DGII."""
        # La DGII puede retornar estado como texto o código
        raw_estado = data.get("estado") or data.get("status") or ""

        # Mapeo flexible
        estado_map = {
            "aceptado": EstadoDGII.ACEPTADO,
            "1": EstadoDGII.ACEPTADO,
            "rechazado": EstadoDGII.RECHAZADO,
            "2": EstadoDGII.RECHAZADO,
            "aceptadocondicional": EstadoDGII.CONDICIONADO,
            "3": EstadoDGII.CONDICIONADO,
            "enproceso": EstadoDGII.PROCESANDO,
            "procesando": EstadoDGII.PROCESANDO,
            "4": EstadoDGII.PROCESANDO,
            "recibido": EstadoDGII.RECIBIDO,
        }
        estado = estado_map.get(str(raw_estado).lower().strip(), EstadoDGII.RECHAZADO)

        track_id = data.get("trackId") or data.get("track_id") or data.get("TrackId")

        return RespuestaDGII(
            estado   = estado,
            track_id = track_id,
            mensaje  = data.get("mensaje") or data.get("message") or "",
            codigo_seguridad=(
                data.get("codigoSeguridad")
                or data.get("CodigoSeguridad")
                or data.get("codigo_seguridad")
                or data.get("CUFE")
                or data.get("cufe")
            ),
            qr_code  = data.get("codigoQR") or data.get("qr_url"),
            detalles = data.get("errores") or data.get("mensajes") or [],
            raw      = data,
        )


# Utilidades: SecurityCode y QR URL

def generar_security_code(xml_firmado: bytes) -> str:
    """
    Extrae los primeros 6 caracteres alfanuméricos del SignatureValue.
    Requerido para la representación impresa del e-CF.
    """
    try:
        root = etree.fromstring(xml_firmado)
        ns = {"ds": "http://www.w3.org/2000/09/xmldsig#"}
        sig_value = root.find(".//ds:SignatureValue", ns)
        if sig_value is not None and sig_value.text:
            # Limpiar whitespace y extraer primeros 6 alnum chars
            clean = re.sub(r"\s+", "", sig_value.text)
            alnum = "".join(c for c in clean if c.isalnum())
            return alnum[:6]
    except Exception as e:
        logger.warning("Error extrayendo security code: %s", e)
    return ""


def generar_qr_url(
    ambiente: str,
    rnc_emisor: str,
    ncf: str,
    total: str,
    fecha_firma: str,
    security_code: str,
    rnc_comprador: str = "",
    tipo_ecf: int = 31,
) -> str:
    """
    Genera la URL de consulta de timbre para el código QR.
    Formato según especificación DGII.
    """
    base = DGIIClient.URLS.get(ambiente, DGIIClient.URLS.get("certificacion"))

    # e-CF 32 (consumo) usa endpoint diferente sin RncComprador
    if tipo_ecf == 32:
        return (
            f"{base}/consultatimbrefc?"
            f"RncEmisor={rnc_emisor}&ENCF={ncf}"
            f"&MontoTotal={total}&FechaFirma={fecha_firma}"
            f"&CodigoSeguridad={security_code}"
        )

    return (
        f"{base}/consultatimbre?"
        f"RncEmisor={rnc_emisor}&RncComprador={rnc_comprador}"
        f"&ENCF={ncf}&MontoTotal={total}"
        f"&FechaFirma={fecha_firma}&CodigoSeguridad={security_code}"
    )
