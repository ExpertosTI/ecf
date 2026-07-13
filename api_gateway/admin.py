# Admin API — Tenant management and certificate upload
# Protected by ADMIN_API_KEY (separate from tenant API keys)

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import uuid
from datetime import date, datetime, timezone
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator, AliasChoices

from ecf_core.cert_vault import CertVault, CertVaultError, CertVaultRepository
from ecf_core.dgii_client import DGIIClient, DGIIClientError
from ecf_core.utils import normalize_odoo_webhook_url, safe_schema as _safe_schema

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"])

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
PLATFORM_OPERATOR_RNC = os.environ.get("PLATFORM_OPERATOR_RNC", "132842316").strip()
ALLOW_CLIENT_ONBOARDING = os.environ.get("ALLOW_CLIENT_ONBOARDING", "false").lower() in {
    "1", "true", "yes", "on",
}


def _operator_rnc() -> str:
    return "".join(c for c in PLATFORM_OPERATOR_RNC if c.isdigit()) or "132842316"


async def _evaluate_onboarding_gate(conn, psfe_ok: bool) -> dict:
    """Evalúa si la plataforma puede onboardear empresas cliente."""
    try:
        operator = await conn.fetchrow(
            """
            SELECT id, rnc, razon_social, dgii_test_ok_at, postulacion_firmada_at, estado
            FROM public.tenants
            WHERE is_platform_operator = TRUE AND deleted_at IS NULL
            LIMIT 1
            """
        )
    except asyncpg.UndefinedColumnError:
        # Migración 014 pendiente: permitir crear (compat) pero avisar
        return {
            "can_onboard_clients": True,
            "psfe_ok": psfe_ok,
            "operator": None,
            "operator_rnc_esperado": _operator_rnc(),
            "blockers": [
                "Ejecuta db/014_onboarding_asistido.sql para habilitar el onboarding asistido."
            ],
            "allow_bypass": True,
            "migration_pending": True,
        }

    operator_cert = False
    if operator:
        operator_cert = bool(await conn.fetchval(
            """
            SELECT 1 FROM public.tenant_certs
            WHERE tenant_id = $1 AND activo = TRUE AND valid_to >= CURRENT_DATE
            LIMIT 1
            """,
            operator["id"],
        ))

    operator_auth_ok = bool(operator and operator["dgii_test_ok_at"])
    can_onboard = bool(
        ALLOW_CLIENT_ONBOARDING
        or (psfe_ok and operator and operator_cert and operator_auth_ok)
    )

    blockers = []
    if not psfe_ok:
        blockers.append("Sube el certificado PSFE en Plataforma (mTLS Renace → DGII).")
    if not operator:
        blockers.append(
            f"Registra primero la empresa operadora Renace (RNC {_operator_rnc()})."
        )
    elif not operator_cert:
        blockers.append("Sube el .p12 vigente de la empresa operadora.")
    elif not operator_auth_ok:
        blockers.append("Ejecuta «Probar CerteCF» en la empresa operadora hasta que pase.")

    return {
        "can_onboard_clients": can_onboard,
        "psfe_ok": psfe_ok,
        "operator": (
            {
                "id": str(operator["id"]),
                "rnc": operator["rnc"],
                "razon_social": operator["razon_social"],
                "cert_ok": operator_cert,
                "dgii_auth_ok": operator_auth_ok,
                "postulacion_ok": bool(operator.get("postulacion_firmada_at")),
            }
            if operator
            else None
        ),
        "operator_rnc_esperado": _operator_rnc(),
        "blockers": blockers,
        "allow_bypass": ALLOW_CLIENT_ONBOARDING,
        "migration_pending": False,
    }


async def require_admin(
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    """Validates the admin bearer token."""
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="Admin API not configured")
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header requerido")
    expected = f"Bearer {ADMIN_API_KEY}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TenantCreate(BaseModel):
    rnc: str = Field(..., min_length=9, max_length=11)
    razon_social: str = Field(..., min_length=1, max_length=255)
    nombre_comercial: Optional[str] = Field(None, max_length=255)
    direccion: Optional[str] = None
    telefono: Optional[str] = Field(None, max_length=20)
    email: str = Field(..., max_length=255)
    plan: str = Field(default="basico")
    ambiente: str = Field(default="certificacion")
    odoo_webhook_url: Optional[str] = Field(None, validation_alias=AliasChoices("webhook_url", "odoo_webhook_url"))
    max_ecf_mensual: int = Field(default=1000, ge=100, le=1000000)

    @field_validator("rnc")
    @classmethod
    def validate_rnc(cls, v):
        if not v.isdigit():
            raise ValueError("RNC debe contener solo dígitos")
        return v

    @field_validator("plan")
    @classmethod
    def validate_plan(cls, v):
        valid_plans = ("basico", "profesional", "enterprise", "pyme", "standard", "empresarial")
        if v not in valid_plans:
            raise ValueError(f"Plan inválido. Opciones: {', '.join(valid_plans)}")
        return v

    @field_validator("ambiente")
    @classmethod
    def validate_ambiente(cls, v):
        if v not in ("simulacion", "certificacion", "produccion"):
            raise ValueError("Ambiente inválido. Opciones: simulacion, certificacion, produccion")
        return v


class TenantUpdate(BaseModel):
    razon_social: Optional[str] = Field(None, max_length=255)
    nombre_comercial: Optional[str] = Field(None, max_length=255)
    direccion: Optional[str] = None
    telefono: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=255)
    plan: Optional[str] = None
    estado: Optional[str] = None
    ambiente: Optional[str] = None
    odoo_webhook_url: Optional[str] = Field(None, validation_alias=AliasChoices("webhook_url", "odoo_webhook_url"))
    max_ecf_mensual: Optional[int] = Field(None, ge=100, le=1000000)

    @field_validator("plan")
    @classmethod
    def validate_plan(cls, v):
        valid_plans = ("basico", "profesional", "enterprise", "pyme", "standard", "empresarial")
        if v is not None and v not in valid_plans:
            raise ValueError("Plan inválido")
        return v

    @field_validator("estado")
    @classmethod
    def validate_estado(cls, v):
        if v is not None and v not in ("pendiente", "activo", "suspendido", "cancelado"):
            raise ValueError("Estado inválido")
        return v

    @field_validator("ambiente")
    @classmethod
    def validate_ambiente(cls, v):
        if v is not None and v not in ("simulacion", "certificacion", "produccion"):
            raise ValueError("Ambiente inválido")
        return v


class NCFSequenceCreate(BaseModel):
    tipo_ecf: int
    secuencia_max: int = Field(default=9999999999, ge=1)

    @field_validator("tipo_ecf")
    @classmethod
    def validate_tipo(cls, v):
        if v not in (31, 32, 33, 34, 41, 43, 44, 45, 46, 47):
            raise ValueError("Tipo e-CF inválido")
        return v


# ---------------------------------------------------------------------------
# Tenant CRUD
# ---------------------------------------------------------------------------

_db_pool_ref = None
_redis_ref = None


def set_db_pool(pool):
    global _db_pool_ref
    _db_pool_ref = pool


def set_redis(redis_client):
    global _redis_ref
    _redis_ref = redis_client


def _get_pool():
    if _db_pool_ref is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    return _db_pool_ref


def _get_redis():
    if _redis_ref is None:
        raise HTTPException(status_code=503, detail="Redis not initialized")
    return _redis_ref


@router.post("/tenants", status_code=201)
async def create_tenant(
    payload: TenantCreate,
    _: None = Depends(require_admin),
):
    """Create a new tenant with schema, NCF sequences, and API key.

    Flujo asistido:
    1) PSFE plataforma
    2) Empresa operadora (PLATFORM_OPERATOR_RNC)
    3) Auth CerteCF del operador
    4) Empresas cliente
    """
    db = _get_pool()
    from ecf_core.platform_config import psfe_status

    rnc = "".join(c for c in payload.rnc if c.isdigit())
    is_operator_candidate = rnc == _operator_rnc()

    # Generate API key (raw) and its SHA-256 hash for storage
    raw_api_key = f"sk_{payload.ambiente[:4]}_{secrets.token_hex(24)}"
    api_key_hash = hashlib.sha256(raw_api_key.encode()).hexdigest()

    # Generate webhook secret
    webhook_secret = secrets.token_hex(32)

    # Schema name from RNC
    schema_name = f"tenant_{rnc}"

    tenant_id = str(uuid.uuid4())

    # Cifrar webhook_secret con vault (obligatorio en producción)
    _sistema_ambiente = os.environ.get("ECF_AMBIENTE", "").lower()
    _es_produccion = _sistema_ambiente in {"ecf", "produccion"}
    encrypted_webhook_secret = webhook_secret
    try:
        vault = CertVault()
        encrypted_webhook_secret = vault.cifrar_campo(webhook_secret)
    except CertVaultError:
        if _es_produccion:
            raise HTTPException(
                status_code=500,
                detail="VAULT_MASTER_KEY no configurada. En producción todos los secrets deben cifrarse en reposo.",
            )
        logger.warning(
            "VAULT_MASTER_KEY no disponible — webhook_secret en texto plano (solo aceptable en pruebas)"
        )

    try:
        async with db.acquire() as conn:
            psfe = await psfe_status(db)
            gate = await _evaluate_onboarding_gate(conn, psfe["configured"])

            existing_operator = gate["operator"] is not None
            mark_as_operator = is_operator_candidate and not existing_operator

            if not mark_as_operator and not gate["can_onboard_clients"]:
                detail = " ".join(gate["blockers"]) or (
                    "Completa el onboarding de la plataforma antes de registrar clientes."
                )
                raise HTTPException(
                    status_code=422,
                    detail={
                        "mensaje": detail,
                        "blockers": gate["blockers"],
                        "operator_rnc_esperado": gate["operator_rnc_esperado"],
                        "siguiente": (
                            "Plataforma → PSFE, luego Empresas → registrar "
                            f"RNC {gate['operator_rnc_esperado']} y Probar CerteCF."
                        ),
                    },
                )

            if is_operator_candidate and existing_operator and existing_operator["rnc"] != rnc:
                # RNC operador ya asignado a otra fila — no debería pasar por UNIQUE rnc
                pass

            async with conn.transaction():
                # Insert tenant
                try:
                    await conn.execute(
                        """
                        INSERT INTO public.tenants
                            (id, rnc, razon_social, nombre_comercial, direccion,
                             telefono, email, api_key,
                             plan, estado, schema_name, ambiente,
                             odoo_webhook_url, odoo_webhook_secret,
                             max_ecf_mensual, is_platform_operator, onboarding_started_at)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'activo',$10,$11,$12,$13,$14,$15, NOW())
                    """,
                        uuid.UUID(tenant_id),
                        rnc,
                        payload.razon_social,
                        payload.nombre_comercial,
                        payload.direccion,
                        payload.telefono,
                        payload.email,
                        api_key_hash,
                        payload.plan,
                        schema_name,
                        payload.ambiente,
                        payload.odoo_webhook_url,
                        encrypted_webhook_secret,
                        payload.max_ecf_mensual,
                        mark_as_operator,
                    )
                except asyncpg.UndefinedColumnError:
                    # Migración 014 aún no aplicada — crear sin columnas nuevas
                    await conn.execute(
                        """
                        INSERT INTO public.tenants
                            (id, rnc, razon_social, nombre_comercial, direccion,
                             telefono, email, api_key,
                             plan, estado, schema_name, ambiente,
                             odoo_webhook_url, odoo_webhook_secret,
                             max_ecf_mensual)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'activo',$10,$11,$12,$13,$14)
                    """,
                        uuid.UUID(tenant_id),
                        rnc,
                        payload.razon_social,
                        payload.nombre_comercial,
                        payload.direccion,
                        payload.telefono,
                        payload.email,
                        api_key_hash,
                        payload.plan,
                        schema_name,
                        payload.ambiente,
                        payload.odoo_webhook_url,
                        encrypted_webhook_secret,
                        payload.max_ecf_mensual,
                    )
                    mark_as_operator = False

                # Create tenant schema with all tables
                await conn.execute(
                    "SELECT public.crear_schema_tenant($1)",
                    schema_name,
                )

                # Create NCF sequences for all e-CF types
                for tipo_ecf in (31, 32, 33, 34, 41, 43, 44, 45, 46, 47):
                    prefijo = f"E{tipo_ecf}"
                    await conn.execute(
                        """
                        INSERT INTO public.ncf_sequences
                            (tenant_id, tipo_ecf, prefijo, secuencia_actual, secuencia_max, activo)
                        VALUES ($1, $2, $3, 0, 9999999999, TRUE)
                    """,
                        uuid.UUID(tenant_id),
                        tipo_ecf,
                        prefijo,
                    )

        logger.info(
            "Tenant creado: %s (RNC: %s) operador=%s",
            tenant_id, rnc, mark_as_operator,
        )

    except HTTPException:
        raise
    except asyncpg.UniqueViolationError as e:
        detail = str(e)
        if "rnc" in detail:
            raise HTTPException(status_code=409, detail=f"RNC {rnc} ya está registrado")
        raise HTTPException(status_code=409, detail="Tenant ya existe")
    except asyncpg.CheckViolationError:
        raise HTTPException(
            status_code=422, detail="Valor no permitido. Verifique ambiente, plan y estado del tenant."
        )
    except Exception as e:
        logger.error("Error creando tenant %s: %s", rnc, e, exc_info=True)
        raise HTTPException(
            status_code=500, detail="Error interno al crear empresa. Revise los logs del servidor."
        )

    next_steps = [
        "Guarda API Key y Webhook Secret (no se recuperan).",
        "Abre Certificación DGII y sigue el paso resaltado.",
        "Sube el .p12 del contribuyente y ejecuta Probar CerteCF.",
    ]
    if mark_as_operator:
        next_steps.insert(
            0,
            "Empresa marcada como operadora Renace. Complétala antes de registrar clientes.",
        )

    return {
        "tenant_id": tenant_id,
        "rnc": rnc,
        "razon_social": payload.razon_social,
        "schema_name": schema_name,
        "ambiente": payload.ambiente,
        "api_key": raw_api_key,
        "webhook_secret": webhook_secret,
        "estado": "activo",
        "is_platform_operator": mark_as_operator,
        "siguiente_paso": "certificacion",
        "next_steps": next_steps,
        "mensaje": (
            "Empresa operadora creada. Continúa en Certificación DGII."
            if mark_as_operator
            else "Empresa cliente creada. Guarda el api_key y webhook_secret — no se pueden recuperar."
        ),
    }


@router.get("/tenants")
async def list_tenants(
    estado: Optional[str] = None,
    _: None = Depends(require_admin),
):
    """List all tenants with summary info."""
    try:
        db = _get_pool()
        async with db.acquire() as conn:
            if estado:
                rows = await conn.fetch(
                    """
                    SELECT id, rnc, razon_social, plan, estado, ambiente,
                           ecf_emitidos_mes, max_ecf_mensual, cert_vencimiento,
                           created_at
                    FROM public.tenants
                    WHERE deleted_at IS NULL AND estado = $1
                    ORDER BY created_at DESC
                """,
                    estado,
                )
            else:
                rows = await conn.fetch("""
                    SELECT id, rnc, razon_social, plan, estado, ambiente,
                           ecf_emitidos_mes, max_ecf_mensual, cert_vencimiento,
                           created_at
                    FROM public.tenants
                    WHERE deleted_at IS NULL
                    ORDER BY created_at DESC
                """)
        return {"tenants": [dict(r) for r in rows]}
    except Exception as e:
        logger.error("Error listando tenants: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Error al listar empresas. Revise los logs del servidor.")


@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: str,
    _: None = Depends(require_admin),
):
    """Get tenant details."""
    db = _get_pool()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, rnc, razon_social, nombre_comercial, direccion,
                   telefono, email, plan, estado, schema_name, ambiente,
                   odoo_webhook_url, ecf_emitidos_mes, max_ecf_mensual,
                   cert_vencimiento, created_at, updated_at
            FROM public.tenants
            WHERE id = $1 AND deleted_at IS NULL
        """,
            uuid.UUID(tenant_id),
        )
    if not row:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    res = dict(row)
    res["webhook_url"] = res.get("odoo_webhook_url")
    return res


@router.patch("/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    payload: TenantUpdate,
    _: None = Depends(require_admin),
):
    """Update tenant fields."""
    db = _get_pool()
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=422, detail="No fields to update")

    # Build dynamic SET clause safely
    set_parts = []
    values = []
    idx = 2  # $1 is tenant_id
    for key, val in updates.items():
        set_parts.append(f"{key} = ${idx}")
        values.append(val)
        idx += 1

    set_clause = ", ".join(set_parts)
    query = f"UPDATE public.tenants SET {set_clause}, updated_at = NOW() WHERE id = $1 AND deleted_at IS NULL RETURNING id"

    async with db.acquire() as conn:
        result = await conn.fetchval(query, uuid.UUID(tenant_id), *values)

    if not result:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    return {"tenant_id": tenant_id, "updated": list(updates.keys())}


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(
    tenant_id: str,
    _: None = Depends(require_admin),
):
    """Soft-delete a tenant."""
    db = _get_pool()
    async with db.acquire() as conn:
        result = await conn.fetchval(
            "UPDATE public.tenants SET deleted_at = NOW(), estado = 'cancelado' "
            "WHERE id = $1 AND deleted_at IS NULL RETURNING id",
            uuid.UUID(tenant_id),
        )
    if not result:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    return {"tenant_id": tenant_id, "estado": "cancelado"}


@router.post("/tenants/{tenant_id}/rotate-key")
async def rotate_api_key(
    tenant_id: str,
    _: None = Depends(require_admin),
):
    """Generate a new API key for a tenant (invalidates the old one)."""
    db = _get_pool()

    # Verify tenant exists
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ambiente FROM public.tenants WHERE id = $1 AND deleted_at IS NULL",
            uuid.UUID(tenant_id),
        )
    if not row:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    new_api_key = f"sk_{row['ambiente'][:4]}_{secrets.token_hex(24)}"
    new_hash = hashlib.sha256(new_api_key.encode()).hexdigest()

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE public.tenants SET api_key = $1, updated_at = NOW() WHERE id = $2",
            new_hash,
            uuid.UUID(tenant_id),
        )

    return {
        "tenant_id": tenant_id,
        "api_key": new_api_key,
        "mensaje": "Guarda la nueva API key — no se puede recuperar.",
    }


_WEBHOOK_PREV_TTL = int(os.environ.get("WEBHOOK_ROTATION_TTL", "900"))  # 15 min default


@router.post("/tenants/{tenant_id}/rotate-webhook")
async def rotate_webhook_secret(
    tenant_id: str,
    _: None = Depends(require_admin),
):
    """Rota el webhook secret con ventana de gracia de 15 min (configurable via WEBHOOK_ROTATION_TTL).

    Durante el TTL, el worker acepta también el secret anterior para que los
    callbacks en vuelo no fallen mientras Odoo es actualizado con el nuevo valor.
    """
    db = _get_pool()
    redis = _get_redis()

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, odoo_webhook_secret FROM public.tenants WHERE id = $1 AND deleted_at IS NULL",
            uuid.UUID(tenant_id),
        )
    if not row:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    # Guardar secret actual (descifrado) en Redis con TTL para fallback durante rotación
    old_encrypted = row["odoo_webhook_secret"]
    if old_encrypted:
        try:
            vault = CertVault()
            old_plain = vault.descifrar_campo(old_encrypted)
        except Exception:
            old_plain = old_encrypted  # ya estaba en texto plano
        if old_plain:
            await redis.set(
                f"whk:prev:{tenant_id}",
                old_plain,
                ex=_WEBHOOK_PREV_TTL,
            )

    new_secret = secrets.token_hex(32)
    encrypted = new_secret
    try:
        vault = CertVault()
        encrypted = vault.cifrar_campo(new_secret)
    except CertVaultError:
        logger.warning("VAULT_MASTER_KEY no disponible — webhook secret en texto plano")

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE public.tenants SET odoo_webhook_secret = $1, updated_at = NOW() WHERE id = $2",
            encrypted,
            uuid.UUID(tenant_id),
        )

    return {
        "tenant_id": tenant_id,
        "webhook_secret": new_secret,
        "ttl_segundos": _WEBHOOK_PREV_TTL,
        "mensaje": (
            f"Actualiza el Webhook Secret en Odoo dentro de los próximos "
            f"{_WEBHOOK_PREV_TTL // 60} minutos. "
            "Durante ese período el sistema acepta ambos secrets automáticamente."
        ),
    }


# NOTA: el endpoint POST /tenants/{tenant_id}/test-webhook está definido más
# abajo (sección RNC/tools). Aquí existía una definición duplicada que FastAPI
# ignoraba silenciosamente — eliminada para evitar comportamiento impredecible.


# ---------------------------------------------------------------------------
# Certificate management
# ---------------------------------------------------------------------------


@router.post("/tenants/{tenant_id}/certs", status_code=201)
async def upload_certificate(
    tenant_id: str,
    cert_password: str = Form(...),
    cert_file: UploadFile = File(...),
    _: None = Depends(require_admin),
):
    """Upload a .p12 certificate for a tenant."""
    db = _get_pool()

    # Verify tenant exists
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM public.tenants WHERE id = $1 AND deleted_at IS NULL",
            uuid.UUID(tenant_id),
        )
    if not row:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    # Read and validate the .p12 file
    p12_data = await cert_file.read()
    if len(p12_data) > 50_000:  # 50KB max for a .p12
        raise HTTPException(status_code=422, detail="Archivo demasiado grande (max 50KB)")
    if len(p12_data) < 100:
        raise HTTPException(status_code=422, detail="Archivo demasiado pequeño para ser un .p12 válido")

    try:
        vault = CertVault()
        cert_repo = CertVaultRepository(db, vault)

        # Store encrypted cert_password in tenant record
        encrypted_password = vault.cifrar_campo(cert_password)
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE public.tenants SET cert_password = $1, updated_at = NOW() WHERE id = $2",
                encrypted_password,
                uuid.UUID(tenant_id),
            )

        cert_id = await cert_repo.guardar(tenant_id, p12_data, cert_password.encode("utf-8"))

        # Get cert metadata for response
        metadatos = vault.extraer_metadatos(p12_data, cert_password.encode("utf-8"))

        logger.info(
            "Certificado subido para tenant %s: serial=%s, vence=%s",
            tenant_id,
            metadatos["serial"],
            metadatos["valid_to"],
        )

        return {
            "cert_id": cert_id,
            "tenant_id": tenant_id,
            "serial": metadatos["serial"],
            "subject": metadatos["subject"],
            "valid_from": str(metadatos["valid_from"]),
            "valid_to": str(metadatos["valid_to"]),
            "mensaje": "Certificado cifrado y almacenado correctamente.",
        }

    except CertVaultError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/tenants/{tenant_id}/certs")
async def list_certificates(
    tenant_id: str,
    _: None = Depends(require_admin),
):
    """List certificates for a tenant."""
    db = _get_pool()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, cert_serial, cert_subject, valid_from, valid_to, activo, created_at
            FROM public.tenant_certs
            WHERE tenant_id = $1
            ORDER BY created_at DESC
        """,
            uuid.UUID(tenant_id),
        )
    return {"certificates": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# NCF Sequence management
# ---------------------------------------------------------------------------


@router.post("/tenants/{tenant_id}/ncf-sequences", status_code=201)
async def create_ncf_sequence(
    tenant_id: str,
    payload: NCFSequenceCreate,
    _: None = Depends(require_admin),
):
    """Create or reset an NCF sequence for a tenant."""
    db = _get_pool()
    prefijo = f"E{payload.tipo_ecf}"

    async with db.acquire() as conn:
        # Check tenant exists
        exists = await conn.fetchval(
            "SELECT 1 FROM public.tenants WHERE id = $1 AND deleted_at IS NULL",
            uuid.UUID(tenant_id),
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")

        # Upsert sequence
        await conn.execute(
            """
            INSERT INTO public.ncf_sequences
                (tenant_id, tipo_ecf, prefijo, secuencia_actual, secuencia_max, activo)
            VALUES ($1, $2, $3, 0, $4, TRUE)
            ON CONFLICT (tenant_id, tipo_ecf)
            DO UPDATE SET secuencia_max = $4, activo = TRUE, updated_at = NOW()
        """,
            uuid.UUID(tenant_id),
            payload.tipo_ecf,
            prefijo,
            payload.secuencia_max,
        )

    return {
        "tenant_id": tenant_id,
        "tipo_ecf": payload.tipo_ecf,
        "prefijo": prefijo,
        "secuencia_max": payload.secuencia_max,
    }


@router.get("/tenants/{tenant_id}/ncf-sequences")
async def list_ncf_sequences(
    tenant_id: str,
    _: None = Depends(require_admin),
):
    """List NCF sequences for a tenant."""
    db = _get_pool()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tipo_ecf, prefijo, secuencia_actual, secuencia_max, activo, updated_at
            FROM public.ncf_sequences
            WHERE tenant_id = $1
            ORDER BY tipo_ecf
        """,
            uuid.UUID(tenant_id),
        )
    return {"sequences": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# DLQ Management
# ---------------------------------------------------------------------------


@router.get("/dlq")
async def list_dlq(
    limit: int = 50,
    _: None = Depends(require_admin),
):
    """List messages in the Dead Letter Queue."""
    import json

    redis_client = _get_redis()

    messages = await redis_client.lrange("ecf:dlq", 0, limit - 1)
    total = await redis_client.llen("ecf:dlq")
    parsed = []
    for msg in messages:
        try:
            item = json.loads(msg)
            if item.get("dlq_error") and not item.get("error"):
                item["error"] = item["dlq_error"]
            parsed.append(item)
        except json.JSONDecodeError:
            parsed.append({"raw": msg})
    return {"total": total, "messages": parsed}


@router.delete("/dlq/{index}")
async def remove_dlq_message(
    index: int,
    _: None = Depends(require_admin),
):
    """Remove a specific message from the DLQ by replaying or discarding."""
    redis_client = _get_redis()
    sentinel = f"__REMOVED__:{uuid.uuid4().hex}"
    lua_script = """
    local key = KEYS[1]
    local index = tonumber(ARGV[1])
    local sentinel = ARGV[2]
    local msg = redis.call('LINDEX', key, index)
    if not msg then return nil end
    redis.call('LSET', key, index, sentinel)
    redis.call('LREM', key, 1, sentinel)
    return msg
    """
    msg = await redis_client.eval(lua_script, 1, "ecf:dlq", index, sentinel)
    if msg is None:
        raise HTTPException(status_code=404, detail="Índice fuera de rango")
    return {"removed": True}


@router.post("/dlq/{index}/retry")
async def retry_dlq_message(
    index: int,
    _: None = Depends(require_admin),
):
    """Move a DLQ message back to the pending queue for reprocessing."""
    import json

    redis_client = _get_redis()
    sentinel = f"__REMOVED__:{uuid.uuid4().hex}"
    lua_script = """
    local key = KEYS[1]
    local index = tonumber(ARGV[1])
    local sentinel = ARGV[2]
    local msg = redis.call('LINDEX', key, index)
    if not msg then return nil end
    redis.call('LSET', key, index, sentinel)
    redis.call('LREM', key, 1, sentinel)
    return msg
    """
    msg = await redis_client.eval(lua_script, 1, "ecf:dlq", index, sentinel)
    if msg is None:
        raise HTTPException(status_code=404, detail="Índice fuera de rango")

    try:
        parsed = json.loads(msg)
        parsed["intento"] = 1
        parsed["retried_from_dlq"] = True
        msg = json.dumps(parsed)
    except json.JSONDecodeError:
        pass

    await redis_client.rpush("ecf:pending", msg)

    return {"retried": True, "mensaje": "Mensaje movido a cola pendiente"}


# ---------------------------------------------------------------------------
# System stats
# ---------------------------------------------------------------------------


@router.get("/stats")
async def system_stats(
    _: None = Depends(require_admin),
):
    """System-wide statistics."""
    try:
        db = _get_pool()
        redis_client = _get_redis()

        async with db.acquire() as conn:
            tenants_total = await conn.fetchval(
                "SELECT COUNT(*) FROM public.tenants WHERE deleted_at IS NULL"
            )
            tenants_activos = await conn.fetchval(
                "SELECT COUNT(*) FROM public.tenants WHERE estado = 'activo' AND deleted_at IS NULL"
            )
            certs_por_vencer = await conn.fetchval(
                "SELECT COUNT(*) FROM public.tenants "
                "WHERE cert_vencimiento <= CURRENT_DATE + INTERVAL '30 days' "
                "AND deleted_at IS NULL AND estado = 'activo'"
            )

        pending = await redis_client.llen("ecf:pending")
        retry = await redis_client.zcard("ecf:retry")
        dlq = await redis_client.llen("ecf:dlq")

        return {
            "tenants": {
                "total": tenants_total,
                "activos": tenants_activos,
                "certs_por_vencer": certs_por_vencer,
            },
            "queues": {
                "pending": pending,
                "retry": retry,
                "dlq": dlq,
            },
        }
    except Exception as e:
        logger.error("Error en system_stats: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500, detail="Error al obtener estadísticas. Revise los logs del servidor."
        )


# ---------------------------------------------------------------------------
# DGII RNC Lookup
# ---------------------------------------------------------------------------


@router.get("/dgii/rnc/{rnc}")
async def lookup_rnc_dgii(
    rnc: str,
    _: None = Depends(require_admin),
):
    """
    Looks up RNC data directly from DGII or a reliable scraper.
    """
    # Sanitize RNC: remove dashes or spaces
    rnc = "".join(filter(str.isdigit, rnc))

    if not (len(rnc) == 9 or len(rnc) == 11):
        raise HTTPException(status_code=422, detail="RNC debe tener 9 u 11 dígitos")

    # 1. Búsqueda en Base de Datos local (tabla dgii_rnc)
    db = _get_pool()
    # Limpiamos el RNC de entrada de guiones o espacios
    rnc_clean = "".join(filter(str.isdigit, rnc))

    try:
        async with db.acquire() as conn:
            # Buscamos limpiando también lo que haya en la base de datos (por si acaso)
            row = await conn.fetchrow(
                """
                SELECT rnc, razon_social, estado
                FROM public.dgii_rnc
                WHERE regexp_replace(rnc, '[^0-9]', '', 'g') = $1
            """,
                rnc_clean,
            )

            if row:
                logger.info(f"RNC {rnc} found in local PostgreSQL DB")
                return {
                    "rnc": row["rnc"],
                    "razon_social": row["razon_social"],
                    "nombre_comercial": row["razon_social"],
                    "direccion": "Consultar en DGII",
                    "estado": row["estado"],
                    "source": "local_db",
                }
    except Exception as e:
        logger.error(f"Error querying local RNC DB: {e}")
        raise HTTPException(
            status_code=503, detail="Error consultando base de datos local de RNC. Intente más tarde."
        )

    # No hay fallback con datos fabricados: si el RNC no está en el padrón
    # cargado desde DGII (tabla public.dgii_rnc), respondemos 404. Devolver
    # datos hardcodeados induce a emitir e-CF con razón social incorrecta.
    raise HTTPException(
        status_code=404,
        detail=(
            "RNC no encontrado en el padrón DGII local. "
            "Verifique que se haya ejecutado scripts/cargar_padron_dgii.sh "
            "con el último archivo RNC_Contribuyentes."
        ),
    )


@router.post("/tenants/{tenant_id}/test-webhook")
async def test_webhook(
    tenant_id: str,
    _: None = Depends(require_admin),
):
    """
    Sends a test 'ping' event to the tenant's Odoo webhook URL.
    """
    db = _get_pool()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT rnc, odoo_webhook_url, odoo_webhook_secret FROM public.tenants "
            "WHERE id = $1 AND deleted_at IS NULL",
            uuid.UUID(tenant_id),
        )

    if not row:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    webhook_url = normalize_odoo_webhook_url(row["odoo_webhook_url"])
    if not webhook_url:
        raise HTTPException(status_code=400, detail="URL de webhook no configurada para esta empresa")

    # Decrypt secret
    webhook_secret = row["odoo_webhook_secret"]
    try:
        vault = CertVault()
        webhook_secret = vault.descifrar_campo(webhook_secret)
    except Exception as exc:
        # Almacenado en texto plano o vault no disponible — caemos al valor crudo.
        logger.debug("webhook_secret no descifrable, usando valor crudo: %s", exc)

    if not webhook_secret:
        raise HTTPException(status_code=400, detail="Webhook secret no disponible")

    # Prepare test payload
    import hashlib
    import hmac
    import json
    from datetime import datetime

    import httpx

    payload = json.dumps(
        {
            "event": "ping",
            "message": "Prueba de conectividad desde Renace SaaS",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "rnc": row["rnc"],
        }
    ).encode()

    # Firmar con el MISMO formato que el worker: "sha256=<hex>"
    firma = hmac.new(webhook_secret.encode(), payload, hashlib.sha256).hexdigest()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                webhook_url,
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-ECF-Signature": f"sha256={firma}",
                    "X-ECF-Tenant-RNC": row["rnc"],
                    "X-ECF-Event": "ping",
                },
            )
            return {
                "status_code": resp.status_code,
                "response_body": resp.text[:200],
                "webhook_url_used": webhook_url,
                "success": resp.is_success,
                "error": None if resp.is_success else f"HTTP {resp.status_code}: {resp.text[:200]}",
            }
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/tenants/{tenant_id}/compras")
async def get_tenant_compras(
    tenant_id: str,
    _: None = Depends(require_admin),
):
    """
    Retorna la lista de e-CF recibidas para un tenant (vía admin).
    """
    db = _get_pool()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT schema_name FROM public.tenants WHERE id = $1 AND deleted_at IS NULL",
            uuid.UUID(tenant_id),
        )
        if not row:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")

        schema = _safe_schema(row["schema_name"])
        rows = await conn.fetch(f"""
            SELECT ncf, rnc_proveedor, nombre_proveedor, tipo_ecf, codigo_seguridad,
                   fecha_comprobante, total_monto, itbis_facturado,
                   monto_servicios, monto_bienes, ambiente, estado_odoo,
                   odoo_bill_id, created_at
            FROM {schema}.compras
            ORDER BY fecha_comprobante DESC, created_at DESC
            LIMIT 100
        """)
        return {"received": [dict(r) for r in rows]}


@router.post("/tenants/{tenant_id}/sync-compras")
async def sync_tenant_compras(
    tenant_id: str,
    _: None = Depends(require_admin),
):
    """
    Dispara la sincronización manual de e-CF recibidas para un tenant (vía admin).
    """
    db = _get_pool()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM public.tenants WHERE id = $1 AND deleted_at IS NULL", uuid.UUID(tenant_id)
        )
        if not row:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")

    from ecf_core.cert_vault import CertVaultRepository
    from ecf_core.ecf_recibidas_service import ECFRecibidasService

    repo = CertVaultRepository(db, CertVault())
    service = ECFRecibidasService(db, repo)

    # Ejecutar en segundo plano
    import asyncio

    asyncio.create_task(service.sincronizar_tenant(dict(row)))

    return {"status": "accepted"}


@router.post("/tenants/{tenant_id}/postulacion")
async def generate_signed_postulacion(
    tenant_id: str,
    xml_file: UploadFile = File(...),
    _: None = Depends(require_admin),
):
    """
    Recibe el XML de postulación descargado de la DGII, lo firma con el certificado activo
    del tenant, y lo devuelve con el mismo nombre y estructura sin modificaciones.
    """
    db = _get_pool()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM public.tenants WHERE id = $1 AND deleted_at IS NULL", uuid.UUID(tenant_id)
        )
        if not row:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")

    # 1. Obtener certificado activo del tenant
    try:
        vault = CertVault()
        cert_repo = CertVaultRepository(db, vault)
        cert_info = await cert_repo.obtener_certificado(tenant_id)
    except CertVaultError as e:
        raise HTTPException(status_code=400, detail=f"Error al recuperar el certificado del tenant: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error inesperado al recuperar el certificado: {e}")

    p12_data = cert_info["cert_data"]
    p12_password = cert_info["cert_password"]

    if not p12_data:
        raise HTTPException(status_code=400, detail="El tenant no tiene un certificado activo cargado en el vault")

    # 2. Leer archivo subido
    xml_bytes = await xml_file.read()

    # 3. Firmar usando ECFSigner
    from ecf_core.ecf_core_service import ECFSigner
    try:
        signer = ECFSigner()
        xml_firmado = signer.firmar(xml_bytes, p12_data, p12_password.encode("utf-8"), exclusive=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al firmar el XML de postulación: {e}")

    try:
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE public.tenants SET postulacion_firmada_at = NOW(), updated_at = NOW() "
                "WHERE id = $1",
                uuid.UUID(tenant_id),
            )
    except asyncpg.UndefinedColumnError:
        pass

    from fastapi.responses import Response
    return Response(
        content=xml_firmado,
        media_type="application/xml",
        headers={
            "Content-Disposition": f"attachment; filename={xml_file.filename}"
        }
    )


# ---------------------------------------------------------------------------
# Plataforma PSFE + checklist certificación (multi-tenant)
# ---------------------------------------------------------------------------


# Tipos NCF exigidos por DGII (Manual Técnico e-CF v2.1)
_DGII_TIPOS_NCF = (31, 32, 33, 34, 41, 43, 44, 45, 46, 47)
_DGII_HOMOLOGACION_TIPOS = (31, 32, 33, 34)  # Set de Pruebas obligatorio
_DGII_URLS = {
    "certificacion": "https://ecf.dgii.gov.do/CerteCF",
    "produccion": "https://ecf.dgii.gov.do/eCF",
    "simulacion": "mock local",
}


def _validate_pem_file(data: bytes, kind: str) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Archivo {kind} no es texto PEM") from exc
    if kind == "cert" and "BEGIN CERTIFICATE" not in text:
        raise HTTPException(status_code=422, detail="Certificado PSFE inválido (se espera PEM)")
    if kind == "key" and "PRIVATE KEY" not in text:
        raise HTTPException(status_code=422, detail="Llave PSFE inválida (se espera PEM)")
    if kind == "ca" and "BEGIN CERTIFICATE" not in text:
        raise HTTPException(status_code=422, detail="CA DGII inválida (se espera PEM)")


async def _emisiones_homologacion(conn, schema_name: str) -> dict:
    """Conteo de e-CF aprobados por tipo en el schema del tenant."""
    try:
        schema = _safe_schema(schema_name)
    except ValueError:
        return {"por_tipo": {}, "total_aprobados": 0, "codigo_seguridad_ok": 0}

    rows = await conn.fetch(
        f"""
        SELECT tipo_ecf, COUNT(*) AS cnt
        FROM {schema}.ecf
        WHERE estado = 'aprobado'
        GROUP BY tipo_ecf
        """
    )
    por_tipo = {int(r["tipo_ecf"]): int(r["cnt"]) for r in rows}
    codigo_ok = await conn.fetchval(
        f"""
        SELECT COUNT(*) FROM {schema}.ecf
        WHERE estado = 'aprobado'
          AND security_code IS NOT NULL
          AND LENGTH(security_code) = 6
        """
    )
    return {
        "por_tipo": por_tipo,
        "total_aprobados": sum(por_tipo.values()),
        "codigo_seguridad_ok": int(codigo_ok or 0),
    }


async def _certificacion_readiness(conn, tenant_row, psfe_ok: bool) -> dict:
    tenant_id = tenant_row["id"]
    schema_name = tenant_row.get("schema_name") or f"tenant_{tenant_row['rnc']}"
    ambiente = tenant_row.get("ambiente", "certificacion")
    is_operator = bool(tenant_row.get("is_platform_operator"))
    dgii_test_ok = bool(tenant_row.get("dgii_test_ok_at"))
    postulacion_ok = bool(tenant_row.get("postulacion_firmada_at"))

    cert_row = await conn.fetchrow(
        """
        SELECT valid_to FROM public.tenant_certs
        WHERE tenant_id = $1 AND activo = TRUE
        ORDER BY valid_to DESC
        LIMIT 1
        """,
        tenant_id,
    )
    cert_cargado = cert_row is not None
    hoy = date.today()
    cert_vigente = bool(cert_row and cert_row["valid_to"] and cert_row["valid_to"] >= hoy)

    seq_rows = await conn.fetch(
        """
        SELECT tipo_ecf FROM public.ncf_sequences
        WHERE tenant_id = $1 AND activo = TRUE
        """,
        tenant_id,
    )
    tipos_ncf = {int(r["tipo_ecf"]) for r in seq_rows}
    ncf_completo = all(t in tipos_ncf for t in _DGII_TIPOS_NCF)

    emisiones = await _emisiones_homologacion(conn, schema_name)
    por_tipo = emisiones["por_tipo"]
    homologacion_tipos_ok = all(por_tipo.get(t, 0) >= 1 for t in _DGII_HOMOLOGACION_TIPOS)
    homologacion_detalle = {
        f"E{t}": {"requerido": True, "aprobados": por_tipo.get(t, 0), "ok": por_tipo.get(t, 0) >= 1}
        for t in _DGII_HOMOLOGACION_TIPOS
    }

    webhook_ok = bool(tenant_row.get("odoo_webhook_url"))
    # Homologación en CerteCF; producción cuenta como OK post-homologación
    ambiente_ok = ambiente in ("certificacion", "produccion")
    activo_ok = tenant_row["estado"] == "activo"

    checks = [
        {
            "id": "psfe",
            "label": "PSFE plataforma (mTLS → CerteCF)",
            "ok": psfe_ok,
            "hint": "Menú Plataforma — certificado cliente Renace (Manual Técnico §2.1)",
        },
        {
            "id": "activo",
            "label": "Empresa activa en Renace e-CF",
            "ok": activo_ok,
        },
        {
            "id": "ambiente",
            "label": "Ambiente DGII (certificación o producción)",
            "ok": ambiente_ok,
            "hint": "Homologación: certificacion (CerteCF). Luego producción.",
        },
        {
            "id": "cert_p12",
            "label": "Certificado .p12 del contribuyente (vigente)",
            "ok": cert_cargado and cert_vigente,
            "hint": "Certificado emitido por DGII — pestaña Certificados",
        },
        {
            "id": "dgii_auth",
            "label": "Autenticación CerteCF verificada",
            "ok": dgii_test_ok,
            "hint": "Botón «Probar CerteCF» en este asistente",
        },
        {
            "id": "ncf",
            "label": "Secuencias NCF E31–E47 (10 tipos)",
            "ok": ncf_completo,
            "hint": "Automático al registrar empresa",
        },
        {
            "id": "homologacion",
            "label": "Set de Pruebas: E31, E32, E33, E34 aprobados",
            "ok": homologacion_tipos_ok,
            "hint": "Odoo → Set de Pruebas DGII → confirmar y emitir",
        },
        {
            "id": "codigo_seguridad",
            "label": "Código de Seguridad (6 caracteres DGII)",
            "ok": emisiones["codigo_seguridad_ok"] >= 1,
            "hint": "Generado automáticamente al aprobar e-CF",
        },
        {
            "id": "webhook",
            "label": "Webhook ERP (Odoo / Citrus)",
            "ok": webhook_ok,
            "optional": True,
            "hint": "Recomendado para recibir estado de cada e-CF",
        },
    ]

    required = [c for c in checks if not c.get("optional")]
    score = sum(1 for c in required if c["ok"])
    total = len(required)

    pasos = [
        {
            "orden": 1,
            "id": "psfe",
            "titulo": "Configurar PSFE (mTLS plataforma)",
            "descripcion": "Sube cert.pem, key.pem y ca.pem que entrega la DGII al registrar Renace como PSFE.",
            "ref_dgii": "Manual Técnico e-CF — Autenticación mTLS",
            "ok": psfe_ok,
            "accion": "plataforma",
        },
        {
            "orden": 2,
            "id": "empresa",
            "titulo": (
                "Empresa operadora Renace registrada"
                if is_operator
                else "Empresa cliente registrada con NCF"
            ),
            "descripcion": f"RNC {tenant_row['rnc']} — schema {schema_name} con prefijos E31–E47.",
            "ref_dgii": "Formato NCF DGII (13 caracteres, prefijo E + tipo)",
            "ok": activo_ok and ncf_completo,
            "accion": None,
        },
        {
            "orden": 3,
            "id": "cert_p12",
            "titulo": "Subir certificado .p12 del contribuyente",
            "descripcion": "Certificado digital del emisor para firmar XML (XAdES-BES RSA-SHA256).",
            "ref_dgii": "Manual Técnico e-CF — Firma digital",
            "ok": cert_cargado and cert_vigente,
            "accion": "upload_cert",
        },
        {
            "orden": 4,
            "id": "test_dgii",
            "titulo": "Probar conexión con CerteCF",
            "descripcion": "Valida mTLS PSFE y autenticación semilla con el .p12 del contribuyente.",
            "ref_dgii": "API Autenticación — GET /fe/autenticacion/api/semilla",
            "ok": dgii_test_ok,
            "accion": "test_dgii",
        },
        {
            "orden": 5,
            "id": "postulacion",
            "titulo": "Firmar XML de postulación DGII",
            "descripcion": "Descarga el XML en portal DGII, fírmalo aquí con el .p12 y súbelo de vuelta a dgii.gov.do.",
            "ref_dgii": "Portal DGII → Facturación Electrónica → Postulación",
            "ok": postulacion_ok,
            "accion": "postulacion",
        },
        {
            "orden": 6,
            "id": "odoo",
            "titulo": "Conectar Odoo (ecf_connector)",
            "descripcion": "URL SaaS, API Key, Webhook Secret y ambiente certificacion en Ajustes → e-CF DGII.",
            "ref_dgii": "Integración ERP — callbacks HMAC-SHA256",
            "ok": webhook_ok,
            "accion": "odoo_config",
        },
        {
            "orden": 7,
            "id": "set_pruebas",
            "titulo": "Set de Pruebas DGII en Odoo",
            "descripcion": "Importar Excel de homologación, confirmar facturas; emisión automática a CerteCF.",
            "ref_dgii": "Casos obligatorios E31, E32, E33, E34 + ITBIS exento + USD",
            "ok": homologacion_tipos_ok,
            "accion": None,
        },
        {
            "orden": 8,
            "id": "presentar",
            "titulo": "Presentar evidencia a la DGII",
            "descripcion": "Formulario PSFE, capturas de e-CF aprobados, plan de contingencia y SLA 99.5%.",
            "ref_dgii": "dgii.gov.do/ecf — Área de Tecnología",
            "ok": homologacion_tipos_ok and emisiones["codigo_seguridad_ok"] >= 4,
            "accion": "evidencia",
            "evidencia": [
                "Capturas de e-CF E31–E34 aprobados en CerteCF (panel Homologación)",
                "XML firmados / Códigos de Seguridad de 6 caracteres",
                "Constancia de PSFE Renace ante DGII",
                "Plan de contingencia (docs/contingencia.md)",
                "Formulario de presentación al Área de Tecnología DGII",
            ],
        },
    ]

    paso_actual = next((p for p in pasos if not p["ok"]), None)
    next_blocker = None
    if paso_actual:
        next_blocker = {
            "paso": paso_actual["orden"],
            "id": paso_actual["id"],
            "titulo": paso_actual["titulo"],
            "descripcion": paso_actual["descripcion"],
            "accion": paso_actual.get("accion"),
            "hint": paso_actual.get("ref_dgii"),
        }

    return {
        "ready": score == total,
        "score": score,
        "total": total,
        "checks": checks,
        "pasos": pasos,
        "paso_actual": paso_actual["orden"] if paso_actual else None,
        "next_blocker": next_blocker,
        "is_platform_operator": is_operator,
        "dgii": {
            "ambiente": ambiente,
            "url": _DGII_URLS.get(ambiente, _DGII_URLS["certificacion"]),
            "homologacion": homologacion_detalle,
            "emisiones_aprobadas": emisiones["total_aprobados"],
        },
        "config_odoo": {
            "rnc": tenant_row["rnc"],
            "razon_social": tenant_row["razon_social"],
            "ambiente": ambiente,
            "webhook_url": tenant_row.get("odoo_webhook_url") or "",
            "nota_api_key": "El API Key se muestra solo al crear o rotar la empresa.",
        },
        "odoo_pasos": [
            "Apps → Renace e-CF Connector → Instalar (v18 o v19 según tu Odoo)",
            "Ajustes → e-CF DGII: URL del SaaS, API Key, Webhook Secret, ambiente certificacion",
            "Activar emisión automática (ecf_emision_automatica)",
            "Contabilidad → Set de Pruebas DGII: importar Excel de homologación de la DGII",
            "Confirmar cada factura de prueba → el worker envía a CerteCF y Odoo recibe el webhook",
        ],
    }


@router.get("/platform/psfe")
async def get_platform_psfe(_: None = Depends(require_admin)):
    """Estado del certificado PSFE (sin exponer secretos)."""
    from ecf_core.platform_config import psfe_status

    db = _get_pool()
    return await psfe_status(db)


@router.post("/platform/psfe")
async def upload_platform_psfe(
    cert_file: UploadFile = File(..., description="Certificado cliente PSFE (.pem)"),
    key_file: UploadFile = File(..., description="Llave privada PSFE (.pem)"),
    ca_file: UploadFile = File(..., description="CA raíz DGII (.pem)"),
    _: None = Depends(require_admin),
):
    """Sube certificados PSFE de la plataforma (cifrados en DB)."""
    from ecf_core.platform_config import save_psfe_to_db, psfe_status

    cert_pem = await cert_file.read()
    key_pem = await key_file.read()
    ca_pem = await ca_file.read()

    for data, kind in ((cert_pem, "cert"), (key_pem, "key"), (ca_pem, "ca")):
        if len(data) > 100_000:
            raise HTTPException(status_code=422, detail=f"Archivo {kind} demasiado grande")
        if len(data) < 50:
            raise HTTPException(status_code=422, detail=f"Archivo {kind} vacío o inválido")
        _validate_pem_file(data, kind)

    db = _get_pool()
    try:
        await save_psfe_to_db(db, cert_pem, key_pem, ca_pem)
    except CertVaultError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Señal a workers/scheduler para recargar PSFE sin reinicio manual
    try:
        from ecf_core.platform_config import signal_psfe_reload
        await signal_psfe_reload(_get_redis())
    except Exception as exc:
        logger.warning("Señal reload PSFE no enviada: %s", exc)

    logger.info("PSFE plataforma actualizado desde panel admin")
    status = await psfe_status(db)
    return {
        **status,
        "mensaje": (
            "PSFE guardado cifrado. API ya lo usa; workers/scheduler lo recargan "
            "en el próximo ciclo (señal Redis)."
        ),
    }


@router.get("/platform/readiness")
async def platform_readiness(_: None = Depends(require_admin)):
    """Estado global: PSFE + operador + resumen de certificación por empresa."""
    from ecf_core.platform_config import psfe_status

    db = _get_pool()
    psfe = await psfe_status(db)
    psfe_ok = psfe["configured"]

    async with db.acquire() as conn:
        gate = await _evaluate_onboarding_gate(conn, psfe_ok)
        try:
            rows = await conn.fetch(
                """
                SELECT id, rnc, razon_social, ambiente, estado, odoo_webhook_url,
                       schema_name, is_platform_operator, dgii_test_ok_at,
                       postulacion_firmada_at
                FROM public.tenants
                WHERE deleted_at IS NULL
                ORDER BY is_platform_operator DESC, created_at ASC
                """
            )
        except asyncpg.UndefinedColumnError:
            rows = await conn.fetch(
                """
                SELECT id, rnc, razon_social, ambiente, estado, odoo_webhook_url, schema_name
                FROM public.tenants
                WHERE deleted_at IS NULL
                ORDER BY created_at ASC
                """
            )
        tenants = []
        for row in rows:
            readiness = await _certificacion_readiness(conn, row, psfe_ok)
            tenants.append(
                {
                    "id": str(row["id"]),
                    "rnc": row["rnc"],
                    "razon_social": row["razon_social"],
                    "ambiente": row["ambiente"],
                    "estado": row["estado"],
                    "is_platform_operator": bool(row.get("is_platform_operator")),
                    "ready": readiness["ready"],
                    "score": readiness["score"],
                    "total": readiness["total"],
                    "paso_actual": readiness.get("paso_actual"),
                    "next_blocker": readiness.get("next_blocker"),
                }
            )

    ready_count = sum(1 for t in tenants if t["ready"])
    return {
        "psfe": psfe,
        "onboarding": gate,
        "tenants": tenants,
        "summary": {
            "total": len(tenants),
            "listos": ready_count,
            "pendientes": len(tenants) - ready_count,
            "can_onboard_clients": gate["can_onboard_clients"],
        },
        "flujo": [
            {"orden": 1, "id": "psfe", "titulo": "PSFE plataforma", "ok": psfe_ok},
            {
                "orden": 2,
                "id": "operador",
                "titulo": f"Empresa Renace ({gate['operator_rnc_esperado']})",
                "ok": bool(gate.get("operator")),
            },
            {
                "orden": 3,
                "id": "auth",
                "titulo": "Probar CerteCF (operador)",
                "ok": bool(gate.get("operator") and gate["operator"].get("dgii_auth_ok")),
            },
            {
                "orden": 4,
                "id": "clientes",
                "titulo": "Registrar empresas cliente",
                "ok": gate["can_onboard_clients"],
            },
        ],
    }


@router.get("/tenants/{tenant_id}/certificacion")
async def tenant_certificacion_readiness(
    tenant_id: str,
    _: None = Depends(require_admin),
):
    """Checklist de certificación DGII para una empresa."""
    from ecf_core.platform_config import psfe_status

    db = _get_pool()
    psfe = await psfe_status(db)

    async with db.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                SELECT id, rnc, razon_social, ambiente, estado, odoo_webhook_url,
                       schema_name, is_platform_operator, dgii_test_ok_at,
                       postulacion_firmada_at
                FROM public.tenants
                WHERE id = $1 AND deleted_at IS NULL
                """,
                uuid.UUID(tenant_id),
            )
        except asyncpg.UndefinedColumnError:
            row = await conn.fetchrow(
                """
                SELECT id, rnc, razon_social, ambiente, estado, odoo_webhook_url, schema_name
                FROM public.tenants
                WHERE id = $1 AND deleted_at IS NULL
                """,
                uuid.UUID(tenant_id),
            )
        if not row:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")
        readiness = await _certificacion_readiness(conn, row, psfe["configured"])

    return {
        "tenant_id": tenant_id,
        "rnc": row["rnc"],
        "razon_social": row["razon_social"],
        **readiness,
    }


@router.post("/platform/test-dgii")
async def test_platform_dgii_connection(_: None = Depends(require_admin)):
    """Prueba mTLS PSFE contra semilla CerteCF (sin .p12 del contribuyente)."""
    from ecf_core.platform_config import psfe_status

    db = _get_pool()
    psfe = await psfe_status(db)
    if not psfe["configured"]:
        raise HTTPException(
            status_code=422,
            detail="PSFE no configurado. Sube certificado, llave y CA en Plataforma.",
        )

    try:
        async with DGIIClient("certificacion") as client:
            result = await client.probar_conexion_mtls()
    except DGIIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not result["ok"]:
        raise HTTPException(
            status_code=502,
            detail=f"CerteCF respondió HTTP {result['status_code']}: {result.get('detalle', '')}",
        )
    return {**result, "mensaje": "Conexión mTLS PSFE → CerteCF OK (semilla DGII recibida)"}


@router.post("/tenants/{tenant_id}/test-dgii")
async def test_tenant_dgii_auth(
    tenant_id: str,
    _: None = Depends(require_admin),
):
    """Prueba autenticación completa DGII (semilla + firma .p12 → token)."""
    from ecf_core.platform_config import psfe_status

    db = _get_pool()
    psfe = await psfe_status(db)
    if not psfe["configured"]:
        raise HTTPException(status_code=422, detail="PSFE no configurado en Plataforma")

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, ambiente, rnc FROM public.tenants
            WHERE id = $1 AND deleted_at IS NULL
            """,
            uuid.UUID(tenant_id),
        )
    if not row:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    if row["ambiente"] not in ("certificacion", "produccion"):
        raise HTTPException(
            status_code=422,
            detail="Prueba DGII solo aplica en ambiente certificacion o produccion",
        )

    try:
        vault = CertVault()
        cert_repo = CertVaultRepository(db, vault)
        cert_info = await cert_repo.obtener_certificado(tenant_id)
    except CertVaultError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not cert_info.get("cert_data"):
        raise HTTPException(status_code=400, detail="Sube el certificado .p12 del contribuyente primero")

    try:
        async with DGIIClient(ambiente=row["ambiente"]) as client:
            client.set_certificate(cert_info["cert_data"], cert_info["cert_password"].encode("utf-8"))
            mtls = await client.probar_conexion_mtls()
            if not mtls["ok"]:
                raise HTTPException(
                    status_code=502,
                    detail=f"mTLS falló: HTTP {mtls['status_code']}",
                )
            auth = await client.probar_autenticacion_contribuyente()
    except DGIIClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE public.tenants SET dgii_test_ok_at = NOW(), updated_at = NOW() WHERE id = $1",
                uuid.UUID(tenant_id),
            )
    except asyncpg.UndefinedColumnError:
        pass

    return {
        **auth,
        "rnc": row["rnc"],
        "ambiente": row["ambiente"],
        "mtls": mtls,
        "mensaje": "Autenticación DGII OK. Paso «Probar CerteCF» marcado como completado.",
    }

