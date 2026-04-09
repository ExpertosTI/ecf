"""
Cliente DGII — Envío de e-CF y consulta de estados
Implementa autenticación por semilla (seed), firma del seed, token Bearer,
y envío con los endpoints reales de la DGII.

Flujo de autenticación DGII:
1. GET  /fe/autenticacion/api/semilla         → XML seed
2. Firmar el XML seed con .p12 del tenant
3. POST /fe/autenticacion/api/validacioncertificado  → access token
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
from typing import Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from lxml import etree

logger = logging.getLogger(__name__)


class EstadoDGII(str, Enum):
    ACEPTADO     = "Aceptado"
    RECHAZADO    = "Rechazado"
    CONDICIONADO = "AceptadoCondicional"
    PROCESANDO   = "EnProceso"
    RECIBIDO     = "Recibido"


@dataclass
class RespuestaDGII:
    estado:         EstadoDGII
    track_id:       Optional[str]
    mensaje:        str
    cufe:           Optional[str]
    qr_code:        Optional[str]
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
    }

    # Endpoints de la API DGII
    EP_SEMILLA          = "/fe/autenticacion/api/semilla"
    EP_VALIDACION_CERT  = "/fe/autenticacion/api/validacioncertificado"
    EP_RECEPCION        = "/fe/recepcion/api/ecf"
    EP_CONSULTA_RESULT  = "/fe/recepcion/api/consultaresultado/{track_id}"
    EP_CONSULTA_TIMBRE  = "/fe/consultas/api/consultatimbre"
    EP_ANULACION_RANGO  = "/fe/anulacion/api/anulacionrangos"
    EP_DIR_RECEPTORES   = "/fe/consultas/api/directorioreceptores"

    # Token cache
    _access_token: Optional[str] = None
    _token_expires_at: float = 0

    def __init__(self, ambiente: str = "certificacion"):
        if ambiente not in self.URLS:
            raise ValueError(f"Ambiente inválido: {ambiente}. Válidos: {list(self.URLS)}")
        self.base_url = self.URLS[ambiente]
        self._client: Optional[httpx.AsyncClient] = None
        self._p12_data: Optional[bytes] = None
        self._p12_password: Optional[bytes] = None

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
                logger.warning("PSFE_CERT_B64/PSFE_KEY_B64 no configurados — mTLS deshabilitado")
        except Exception:
            self._cleanup_tmp_files()
            raise

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(60.0, connect=15.0),
            headers={"Accept": "application/json"},
            verify=ssl_context if ssl_context else True,
        )
        self._token_lock = asyncio.Lock()
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
            resp = await self._client.get(self.EP_SEMILLA)
            if resp.status_code != 200:
                raise DGIIClientError(f"Error obteniendo semilla DGII: HTTP {resp.status_code} — {resp.text}")

            seed_xml = resp.text

            # Paso 2: Firmar la semilla con el .p12
            signed_seed = self._sign_seed_xml(seed_xml)

            # Paso 3: Validar certificado con semilla firmada
            resp = await self._client.post(
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
        rnc_emisor: str,
        tipo_ecf: int,
        ncf: str,
    ) -> RespuestaDGII:
        """
        Envía el e-CF firmado a la DGII.
        Retorna un RespuestaDGII con trackId para consulta posterior.
        """
        await self._authenticate()

        last_error = None
        for intento in range(1, 4):
            try:
                logger.info("Enviando NCF %s a DGII — intento %d", ncf, intento)
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

    async def anular_ecf(self, rnc_emisor: str, ncf_desde: str, ncf_hasta: str) -> RespuestaDGII:
        """Solicita la anulación de un rango de e-CF."""
        await self._authenticate()

        payload_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<AnulacionRango xmlns="http://www.dgii.gov.do/ecf">
    <RNCEmisor>{rnc_emisor}</RNCEmisor>
    <CantidadDesde>{ncf_desde}</CantidadDesde>
    <CantidadHasta>{ncf_hasta}</CantidadHasta>
</AnulacionRango>"""

        resp = await self._client.post(
            self.EP_ANULACION_RANGO,
            content=payload_xml.encode("utf-8"),
            headers=self._auth_headers(),
        )
        if resp.status_code not in (200, 201, 202):
            raise DGIIClientError(f"Error en anulación: HTTP {resp.status_code} — {resp.text}")
        return self._parsear_respuesta(resp.json())

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
            cufe     = data.get("CUFE") or data.get("cufe"),
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
