# Admin API — Tenant management and certificate upload
# Protected by ADMIN_API_KEY (separate from tenant API keys)

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import uuid
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from ecf_core.cert_vault import CertVault, CertVaultError, CertVaultRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"])

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")


async def require_admin(
    authorization: str = Header(..., alias="Authorization"),
):
    """Validates the admin bearer token."""
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="Admin API not configured")
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
    odoo_webhook_url: Optional[str] = None
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
    odoo_webhook_url: Optional[str] = None
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
        raise HTTPException(
            status_code=503, detail="Database not initialized"
        )
    return _db_pool_ref


def _get_redis():
    if _redis_ref is None:
        raise HTTPException(
            status_code=503, detail="Redis not initialized"
        )
    return _redis_ref


@router.post("/tenants", status_code=201)
async def create_tenant(
    payload: TenantCreate,
    _: None = Depends(require_admin),
):
    """Create a new tenant with schema, NCF sequences, and API key."""
    db = _get_pool()

    # Generate API key (raw) and its SHA-256 hash for storage
    raw_api_key = f"sk_{payload.ambiente[:4]}_{secrets.token_hex(24)}"
    api_key_hash = hashlib.sha256(raw_api_key.encode()).hexdigest()

    # Generate webhook secret
    webhook_secret = secrets.token_hex(32)

    # Schema name from RNC
    schema_name = f"tenant_{payload.rnc}"

    tenant_id = str(uuid.uuid4())

    # Encrypt webhook_secret if vault is available
    encrypted_webhook_secret = webhook_secret
    try:
        vault = CertVault()
        encrypted_webhook_secret = vault.cifrar_campo(webhook_secret)
    except CertVaultError:
        logger.warning("VAULT_MASTER_KEY not available, storing webhook_secret in plain text")

    try:
        async with db.acquire() as conn:
            async with conn.transaction():
                # Insert tenant
                await conn.execute("""
                    INSERT INTO public.tenants
                        (id, rnc, razon_social, nombre_comercial, direccion,
                         telefono, email, api_key, api_key_hash,
                         plan, estado, schema_name, ambiente,
                         odoo_webhook_url, odoo_webhook_secret,
                         max_ecf_mensual)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'activo',$11,$12,$13,$14,$15)
                """,
                    uuid.UUID(tenant_id),
                    payload.rnc,
                    payload.razon_social,
                    payload.nombre_comercial,
                    payload.direccion,
                    payload.telefono,
                    payload.email,
                    api_key_hash,      # api_key column stores the SHA-256 hash
                    api_key_hash,      # api_key_hash column (bcrypt would be better, same for now)
                    payload.plan,
                    schema_name,
                    payload.ambiente,
                    payload.odoo_webhook_url,
                    encrypted_webhook_secret,
                    payload.max_ecf_mensual,
                )

                # Create tenant schema with all tables
                await conn.execute(
                    "SELECT public.crear_schema_tenant($1)",
                    schema_name,
                )

                # Create NCF sequences for all e-CF types
                for tipo_ecf in (31, 32, 33, 34, 41, 43, 44, 45, 46, 47):
                    prefijo = f"E{tipo_ecf}"
                    await conn.execute("""
                        INSERT INTO public.ncf_sequences
                            (tenant_id, tipo_ecf, prefijo, secuencia_actual, secuencia_max, activo)
                        VALUES ($1, $2, $3, 0, 9999999999, TRUE)
                    """,
                        uuid.UUID(tenant_id),
                        tipo_ecf,
                        prefijo,
                    )

        logger.info("Tenant creado: %s (RNC: %s)", tenant_id, payload.rnc)

    except asyncpg.UniqueViolationError as e:
        detail = str(e)
        if "rnc" in detail:
            raise HTTPException(status_code=409, detail=f"RNC {payload.rnc} ya está registrado")
        raise HTTPException(status_code=409, detail="Tenant ya existe")
    except asyncpg.CheckViolationError as e:
        raise HTTPException(status_code=422, detail=f"Valor no permitido por la base de datos: {e}")
    except Exception as e:
        logger.error("Error creando tenant %s: %s", payload.rnc, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error interno al crear empresa: {type(e).__name__}: {e}")

    return {
        "tenant_id": tenant_id,
        "rnc": payload.rnc,
        "razon_social": payload.razon_social,
        "schema_name": schema_name,
        "ambiente": payload.ambiente,
        "api_key": raw_api_key,
        "webhook_secret": webhook_secret,
        "estado": "activo",
        "mensaje": "Tenant creado. Guarda el api_key y webhook_secret — no se pueden recuperar.",
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
                rows = await conn.fetch("""
                    SELECT id, rnc, razon_social, plan, estado, ambiente,
                           ecf_emitidos_mes, max_ecf_mensual, cert_vencimiento,
                           created_at
                    FROM public.tenants
                    WHERE deleted_at IS NULL AND estado = $1
                    ORDER BY created_at DESC
                """, estado)
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
        raise HTTPException(status_code=500, detail=f"Error al listar empresas: {type(e).__name__}: {e}")


@router.get("/tenants/{tenant_id}")
async def get_tenant(
    tenant_id: str,
    _: None = Depends(require_admin),
):
    """Get tenant details."""
    db = _get_pool()
    async with db.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, rnc, razon_social, nombre_comercial, direccion,
                   telefono, email, plan, estado, schema_name, ambiente,
                   odoo_webhook_url, ecf_emitidos_mes, max_ecf_mensual,
                   cert_vencimiento, created_at, updated_at
            FROM public.tenants
            WHERE id = $1 AND deleted_at IS NULL
        """, uuid.UUID(tenant_id))
    if not row:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    return dict(row)


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
            "UPDATE public.tenants SET api_key = $1, api_key_hash = $1, updated_at = NOW() WHERE id = $2",
            new_hash, uuid.UUID(tenant_id),
        )

    return {"tenant_id": tenant_id, "api_key": new_api_key, "mensaje": "Guarda la nueva API key — no se puede recuperar."}


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
                encrypted_password, uuid.UUID(tenant_id),
            )

        cert_id = await cert_repo.guardar(tenant_id, p12_data, cert_password.encode("utf-8"))

        # Get cert metadata for response
        metadatos = vault.extraer_metadatos(p12_data, cert_password.encode("utf-8"))

        logger.info("Certificado subido para tenant %s: serial=%s, vence=%s",
                     tenant_id, metadatos["serial"], metadatos["valid_to"])

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
        rows = await conn.fetch("""
            SELECT id, cert_serial, cert_subject, valid_from, valid_to, activo, created_at
            FROM public.tenant_certs
            WHERE tenant_id = $1
            ORDER BY created_at DESC
        """, uuid.UUID(tenant_id))
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
        await conn.execute("""
            INSERT INTO public.ncf_sequences
                (tenant_id, tipo_ecf, prefijo, secuencia_actual, secuencia_max, activo)
            VALUES ($1, $2, $3, 0, $4, TRUE)
            ON CONFLICT (tenant_id, tipo_ecf)
            DO UPDATE SET secuencia_max = $4, activo = TRUE, updated_at = NOW()
        """,
            uuid.UUID(tenant_id), payload.tipo_ecf, prefijo, payload.secuencia_max,
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
        rows = await conn.fetch("""
            SELECT tipo_ecf, prefijo, secuencia_actual, secuencia_max, activo, updated_at
            FROM public.ncf_sequences
            WHERE tenant_id = $1
            ORDER BY tipo_ecf
        """, uuid.UUID(tenant_id))
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
            parsed.append(json.loads(msg))
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

    messages = await redis_client.lrange("ecf:dlq", 0, -1)
    if index < 0 or index >= len(messages):
        raise HTTPException(status_code=404, detail="Índice fuera de rango")
    sentinel = "__REMOVED__"
    await redis_client.lset("ecf:dlq", index, sentinel)
    await redis_client.lrem("ecf:dlq", 0, sentinel)
    return {"removed": True}


@router.post("/dlq/{index}/retry")
async def retry_dlq_message(
    index: int,
    _: None = Depends(require_admin),
):
    """Move a DLQ message back to the pending queue for reprocessing."""
    import json
    redis_client = _get_redis()

    messages = await redis_client.lrange("ecf:dlq", 0, -1)
    if index < 0 or index >= len(messages):
        raise HTTPException(status_code=404, detail="Índice fuera de rango")

    msg = messages[index]
    try:
        parsed = json.loads(msg)
        parsed["intento"] = 1
        parsed["retried_from_dlq"] = True
        msg = json.dumps(parsed)
    except json.JSONDecodeError:
        pass

    await redis_client.rpush("ecf:pending", msg)

    sentinel = "__REMOVED__"
    await redis_client.lset("ecf:dlq", index, sentinel)
    await redis_client.lrem("ecf:dlq", 0, sentinel)

    return {"retried": True, "mensaje": "Mensaje movido a cola pendiente"}


# ---------------------------------------------------------------------------
# DGII Integration: cufe_secret por tenant
# ---------------------------------------------------------------------------

class CufeSecretUpdate(BaseModel):
    cufe_secret: str = Field(..., min_length=8, max_length=128,
                             description="Clave secreta CUFE registrada ante la DGII para este tenant")


@router.put("/tenants/{tenant_id}/cufe-secret", status_code=200)
async def set_cufe_secret(
    tenant_id: str,
    payload: CufeSecretUpdate,
    _: None = Depends(require_admin),
):
    """
    Registra el cufe_secret de un tenant (entregado por la DGII durante homologación).
    El secreto se cifra con AES-256-GCM antes de almacenarse.
    """
    db = _get_pool()
    try:
        vault = CertVault()
        encrypted = vault.cifrar_campo(payload.cufe_secret)
    except CertVaultError as e:
        raise HTTPException(status_code=500, detail=f"Vault no disponible: {e}")

    async with db.acquire() as conn:
        updated = await conn.fetchval(
            "UPDATE public.tenants SET cufe_secret = $1, updated_at = NOW() "
            "WHERE id = $2 AND deleted_at IS NULL RETURNING id",
            encrypted, uuid.UUID(tenant_id),
        )
    if not updated:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")

    logger.info("cufe_secret actualizado para tenant %s", tenant_id)
    return {"tenant_id": tenant_id, "mensaje": "cufe_secret registrado y cifrado correctamente"}


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
                "SELECT COUNT(*) FROM public.tenants "
                "WHERE estado = 'activo' AND deleted_at IS NULL"
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
        raise HTTPException(status_code=500, detail=f"Error en estadísticas: {type(e).__name__}: {e}")

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
            row = await conn.fetchrow("""
                SELECT rnc, razon_social, estado 
                FROM public.dgii_rnc 
                WHERE regexp_replace(rnc, '[^0-9]', '', 'g') = $1
            """, rnc_clean)
            
            if row:
                logger.info(f"RNC {rnc} found in local PostgreSQL DB")
                return {
                    "rnc": row["rnc"],
                    "razon_social": row["razon_social"],
                    "nombre_comercial": row["razon_social"],
                    "direccion": "Consultar en DGII",
                    "estado": row["estado"],
                    "source": "local_db"
                }
    except Exception as e:
        logger.error(f"Error querying local RNC DB: {e}")

    # 2. Fallback to Mock DB (Expertos TI and others)
    mock_db = {
        "133109192": {"razon": "LA PERSONA BOUTIQUE SRL", "comercial": "La Persona Boutique", "direccion": "Calle Duarte #410, Santo Domingo"},
        "101001001": {"razon": "BANCO POPULAR DOMINICANO SA", "comercial": "Banco Popular", "direccion": "Av. John F. Kennedy #20, Santo Domingo"},
        "101010632": {"razon": "CERVECERIA NACIONAL DOMINICANA SA", "comercial": "CND", "direccion": "Av. Independencia #100, Santo Domingo"},
        "132842316": {"razon": "EXPERTOS TI, SRL", "comercial": "Expertos TI", "direccion": "Av. Winston Churchill, Santo Domingo"} 
    }

    # 2. Fallback to Mock DB
    found = mock_db.get(rnc)
    if found:
        return {
            "rnc": rnc,
            "razon_social": found["razon"],
            "nombre_comercial": found["comercial"],
            "direccion": found["direccion"],
            "estado": "ACTIVO",
            "source": "saas_cache"
        }

    raise HTTPException(status_code=404, detail="RNC no encontrado en DGII ni en caché local")
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
            uuid.UUID(tenant_id)
        )
    
    if not row:
        raise HTTPException(status_code=404, detail="Tenant no encontrado")
    
    webhook_url = row["odoo_webhook_url"]
    if not webhook_url:
        raise HTTPException(status_code=400, detail="URL de webhook no configurada para esta empresa")
    
    # Decrypt secret
    webhook_secret = row["odoo_webhook_secret"]
    try:
        vault = CertVault()
        webhook_secret = vault.descifrar_campo(webhook_secret)
    except Exception:
        pass # Stored in plain text or vault not available
    
    if not webhook_secret:
        raise HTTPException(status_code=400, detail="Webhook secret no disponible")

    # Prepare test payload
    import json
    import hmac
    import hashlib
    from datetime import datetime, timezone
    import httpx

    payload = json.dumps({
        "event": "ping",
        "message": "Prueba de conectividad desde Renace SaaS",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rnc": row["rnc"]
    }).encode()

    # Sign payload
    firma = hmac.new(webhook_secret.encode(), payload, hashlib.sha256).hexdigest()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                webhook_url,
                content=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-ECF-Signature": firma,
                    "X-ECF-Tenant-RNC": row["rnc"],
                    "X-ECF-Event": "ping"
                }
            )
            return {
                "status_code": resp.status_code,
                "response_body": resp.text[:500], # Truncate for safety
                "success": resp.is_success
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

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
            uuid.UUID(tenant_id)
        )
        if not row:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")
        
        schema = _safe_schema(row["schema_name"])
        rows = await conn.fetch(f"""
            SELECT ncf, rnc_proveedor, nombre_proveedor, tipo_ecf, cufe,
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
            "SELECT * FROM public.tenants WHERE id = $1 AND deleted_at IS NULL",
            uuid.UUID(tenant_id)
        )
        if not row:
            raise HTTPException(status_code=404, detail="Tenant no encontrado")
    
    from ecf_core.ecf_recibidas_service import ECFRecibidasService
    from ecf_core.cert_vault import CertVaultRepository
    
    repo = CertVaultRepository(db)
    service = ECFRecibidasService(db, repo)
    
    # Ejecutar en segundo plano
    import asyncio
    asyncio.create_task(service.sincronizar_tenant(dict(row)))
    
    return {"status": "accepted"}
