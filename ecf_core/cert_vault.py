"""
Cert Vault — Almacenamiento seguro de certificados .p12
Cifrado AES-256-GCM con llave maestra desde variable de entorno.
La DGII auditará que los certificados estén protegidos en reposo.
"""

from __future__ import annotations

import base64
import os
import uuid
from datetime import date
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import pkcs12
import asyncpg


class CertVaultError(Exception):
    pass


class CertVault:
    """
    Cifra y descifra los certificados .p12 de cada tenant usando AES-256-GCM.
    La llave maestra (32 bytes) vive SOLO en la variable de entorno VAULT_MASTER_KEY.
    Nunca se almacena en base de datos.
    """

    def __init__(self):
        key_b64 = os.environ.get("VAULT_MASTER_KEY")
        if not key_b64:
            raise CertVaultError("VAULT_MASTER_KEY no configurada")
        key_bytes = base64.b64decode(key_b64)
        if len(key_bytes) != 32:
            raise CertVaultError("VAULT_MASTER_KEY debe ser exactamente 32 bytes (256 bits)")
        self._aesgcm = AESGCM(key_bytes)

    def cifrar(self, p12_data: bytes) -> tuple[bytes, bytes, bytes]:
        """
        Cifra el .p12 con AES-256-GCM.
        Retorna: (ciphertext, iv, tag)
        El tag de autenticación protege contra manipulación.
        """
        iv = os.urandom(12)  # 96 bits — recomendado para GCM
        # AESGCM.encrypt retorna ciphertext + tag (últimos 16 bytes)
        ct_with_tag = self._aesgcm.encrypt(iv, p12_data, None)
        ciphertext = ct_with_tag[:-16]
        tag        = ct_with_tag[-16:]
        return ciphertext, iv, tag

    def descifrar(self, ciphertext: bytes, iv: bytes, tag: bytes) -> bytes:
        """
        Descifra y verifica autenticidad del .p12.
        Lanza InvalidTag si los datos fueron manipulados.
        """
        ct_with_tag = ciphertext + tag
        return self._aesgcm.decrypt(iv, ct_with_tag, None)

    # --- Cifrado de campos sensibles (cert_password, cufe_secret, webhook_secret) ---

    def cifrar_campo(self, plaintext: str) -> str:
        """
        Cifra un campo de texto corto con AES-256-GCM.
        Retorna base64(iv || ciphertext || tag) para almacenar en VARCHAR.
        """
        if not plaintext:
            return ""
        iv = os.urandom(12)
        ct_with_tag = self._aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)
        return base64.b64encode(iv + ct_with_tag).decode("ascii")

    def descifrar_campo(self, encrypted_b64: str) -> str:
        """
        Descifra un campo previamente cifrado con cifrar_campo().
        Retorna el texto plano. Lanza CertVaultError si el dato es inválido.
        """
        if not encrypted_b64:
            return ""
        try:
            raw = base64.b64decode(encrypted_b64)
            if len(raw) < 29:  # 12 iv + 16 tag + al menos 1 byte ciphertext
                raise CertVaultError("Dato cifrado demasiado corto")
            iv = raw[:12]
            ct_with_tag = raw[12:]
            return self._aesgcm.decrypt(iv, ct_with_tag, None).decode("utf-8")
        except CertVaultError:
            raise
        except Exception as e:
            raise CertVaultError(f"Error descifrando campo: {e}") from e

    def extraer_metadatos(self, p12_data: bytes, password: bytes) -> dict:
        """Extrae metadatos del certificado para almacenar en DB."""
        try:
            private_key, cert, chain = pkcs12.load_key_and_certificates(p12_data, password)
        except Exception as e:
            raise CertVaultError(f"Error extrayendo metadatos del certificado .p12: {e}") from e
        return {
            "serial":     str(cert.serial_number),
            "subject":    cert.subject.rfc4514_string(),
            "valid_from": cert.not_valid_before_utc.date(),
            "valid_to":   cert.not_valid_after_utc.date(),
        }


class CertVaultRepository:
    """Persiste y recupera certificados cifrados desde PostgreSQL."""

    def __init__(self, db_pool: asyncpg.Pool, vault: CertVault):
        self.db    = db_pool
        self.vault = vault

    async def guardar(
        self,
        tenant_id: str,
        p12_data: bytes,
        p12_password: bytes,
    ) -> str:
        """Cifra y guarda el .p12. Desactiva certs anteriores."""
        metadatos    = self.vault.extraer_metadatos(p12_data, p12_password)
        ciphertext, iv, tag = self.vault.cifrar(p12_data)
        cert_id = str(uuid.uuid4())

        async with self.db.acquire() as conn:
            async with conn.transaction():
                # Desactivar cert anterior
                await conn.execute(
                    "UPDATE public.tenant_certs SET activo = FALSE WHERE tenant_id = $1",
                    uuid.UUID(tenant_id)
                )
                # Insertar nuevo
                await conn.execute("""
                    INSERT INTO public.tenant_certs
                        (id, tenant_id, cert_data, iv, tag,
                         cert_serial, cert_subject, valid_from, valid_to, activo)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE)
                """,
                    uuid.UUID(cert_id),
                    uuid.UUID(tenant_id),
                    ciphertext, iv, tag,
                    metadatos["serial"],
                    metadatos["subject"],
                    metadatos["valid_from"],
                    metadatos["valid_to"],
                )
                # Actualizar vencimiento en tabla tenants
                await conn.execute(
                    "UPDATE public.tenants SET cert_vencimiento = $1 WHERE id = $2",
                    metadatos["valid_to"], uuid.UUID(tenant_id)
                )

        return cert_id

    async def obtener(self, tenant_id: str) -> bytes:
        """Recupera y descifra el .p12 activo del tenant."""
        async with self.db.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT cert_data, iv, tag
                FROM public.tenant_certs
                WHERE tenant_id = $1 AND activo = TRUE
                ORDER BY created_at DESC
                LIMIT 1
            """, uuid.UUID(tenant_id))

        if not row:
            raise CertVaultError(f"Certificado no encontrado para tenant {tenant_id}")

        return self.vault.descifrar(
            bytes(row["cert_data"]),
            bytes(row["iv"]),
            bytes(row["tag"]),
        )

    async def verificar_vencimientos(self, db_pool: asyncpg.Pool) -> list[dict]:
        """
        Retorna lista de tenants con cert a punto de vencer (< 30 días).
        Para el job de alertas diario.
        """
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, rnc, razon_social, email, cert_vencimiento
                FROM public.tenants
                WHERE cert_vencimiento <= CURRENT_DATE + INTERVAL '30 days'
                  AND cert_alerta_enviada = FALSE
                  AND deleted_at IS NULL
                  AND estado = 'activo'
            """)
        return [dict(r) for r in rows]
