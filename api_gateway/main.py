# SaaS API - FastAPI Gateway

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import List, Optional

import asyncpg
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from api_gateway.admin import (
    router as admin_router,
    set_db_pool as admin_set_db_pool,
    set_redis as admin_set_redis,
)
from api_gateway.reportes import (
    ExportFormat,
    HEADERS_606, HEADERS_607, HEADERS_608,
    _606_to_txt, _607_to_txt, _608_to_txt,
    _build_response,
)

logger = logging.getLogger(__name__)

# Schema name sanitization (prevent SQL injection)

import re

_SAFE_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_SCHEMA_BLACKLIST = frozenset({
    "public", "pg_catalog", "pg_toast", "information_schema",
    "pg_temp", "pg_toast_temp",
})


def _safe_schema(name: str) -> str:
    """Validate and return schema name. Raises ValueError if unsafe."""
    if not _SAFE_SCHEMA_RE.match(name):
        raise ValueError(f"Invalid schema name: {name!r}")
    if name in _SCHEMA_BLACKLIST:
        raise ValueError(f"Reserved schema name: {name!r}")
    return name


def _validar_periodo(anio: int, mes: int):
    """Valida que año y mes sean razonables para reportes DGII."""
    if not (2020 <= anio <= 2100):
        raise HTTPException(status_code=422, detail=f"Año fuera de rango válido: {anio}")
    if not (1 <= mes <= 12):
        raise HTTPException(status_code=422, detail=f"Mes inválido: {mes}")

# Lifespan (reemplaza on_event deprecated)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    app.state.db_pool = await asyncpg.create_pool(
        dsn=os.environ["DATABASE_URL"],
        min_size=5,
        max_size=20,
    )
    app.state.redis = await aioredis.from_url(
        os.environ["REDIS_URL"],
        password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True,
    )
    admin_set_db_pool(app.state.db_pool)
    admin_set_redis(app.state.redis)
    logger.info("API Gateway iniciado")
    yield
    # Shutdown
    await app.state.db_pool.close()
    await app.state.redis.aclose()


# App

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").split(",")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()]

if not ALLOWED_ORIGINS:
    logger.warning("ALLOWED_ORIGINS no configurado — CORS deshabilitado")

app = FastAPI(
    title="SaaS ECF DGII",
    version="1.0.0",
    docs_url=None,          # Deshabilitar Swagger en producción
    redoc_url=None,
    lifespan=lifespan,
)

if ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_methods=["POST", "GET", "PATCH", "DELETE"],
        allow_headers=["X-API-Key", "Content-Type", "Authorization"],
    )

app.include_router(admin_router)

# ── Landing page (ruta raíz) ──
import pathlib
from fastapi.responses import FileResponse

_landing_file = pathlib.Path(__file__).resolve().parent.parent / "landing" / "index.html"

@app.get("/", include_in_schema=False)
async def landing():
    if _landing_file.is_file():
        return FileResponse(str(_landing_file), media_type="text/html")
    return JSONResponse({"service": "SaaS ECF DGII", "status": "running"})

# ── Portal Admin (static SPA) ──
_portal_dir = pathlib.Path(__file__).resolve().parent.parent / "portal_admin"
if _portal_dir.is_dir():
    app.mount("/portal", StaticFiles(directory=str(_portal_dir), html=True), name="portal")

# Rate Limiting basado en Redis (soporta múltiples instancias)

RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "60"))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))


async def _check_rate_limit(api_key_hash: str, redis: aioredis.Redis):
    """Limita requests por ventana de tiempo por tenant usando Redis."""
    key = f"rl:{api_key_hash}"
    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, RATE_LIMIT_WINDOW)
    if current > RATE_LIMIT_MAX:
        ttl = await redis.ttl(key)
        raise HTTPException(
            status_code=429,
            detail="Demasiadas solicitudes. Intente de nuevo en unos segundos.",
            headers={"Retry-After": str(ttl if ttl > 0 else RATE_LIMIT_WINDOW)},
        )


# Auditoría

async def _audit_log(
    db: asyncpg.Pool,
    tenant_id: str,
    accion: str,
    entidad: str = None,
    entidad_id: str = None,
    detalle: dict = None,
    ip_address: str = None,
):
    """Registra una acción en system_audit_log."""
    try:
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO public.system_audit_log "
                "(tenant_id, accion, entidad, entidad_id, detalle, ip_address) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6::inet)",
                uuid.UUID(tenant_id), accion, entidad, entidad_id,
                json.dumps(detalle) if detalle else None,
                ip_address,
            )
    except Exception as e:
        logger.warning("Error escribiendo audit log: %s", e)


# Dependencias

async def get_db() -> asyncpg.Pool:
    return app.state.db_pool

async def get_redis() -> aioredis.Redis:
    return app.state.redis


async def get_tenant(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: asyncpg.Pool = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict:
    """Autentica y retorna el tenant por su API key."""
    # Hash de la key para comparar con lo almacenado
    key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()

    # Rate limiting por tenant (Redis-based)
    await _check_rate_limit(key_hash, redis)

    async with db.acquire() as conn:
        tenant = await conn.fetchrow("""
            SELECT id, rnc, razon_social, schema_name, ambiente,
                   estado, ecf_emitidos_mes, max_ecf_mensual,
                   cert_vencimiento, odoo_webhook_url, odoo_webhook_secret
            FROM public.tenants
            WHERE api_key = $1 AND deleted_at IS NULL
        """, key_hash)

    if not tenant:
        raise HTTPException(status_code=401, detail="API key inválida")

    if tenant["estado"] != "activo":
        raise HTTPException(status_code=403, detail=f"Tenant en estado: {tenant['estado']}")

    if tenant["ecf_emitidos_mes"] >= tenant["max_ecf_mensual"]:
        raise HTTPException(status_code=429, detail="Límite mensual de e-CF alcanzado")

    # Verificar cert no vencido
    if tenant["cert_vencimiento"] and tenant["cert_vencimiento"] < date.today():
        raise HTTPException(status_code=403, detail="Certificado .p12 del tenant vencido")

    return dict(tenant)


# Modelos Pydantic

class ItemPayload(BaseModel):
    descripcion:             str = Field(..., max_length=200)
    cantidad:                Decimal = Field(..., gt=0)
    precio_unitario:         Decimal = Field(..., gt=0)
    descuento:               Decimal = Field(default=Decimal("0"), ge=0)
    itbis_tasa:              Decimal = Field(default=Decimal("18"))
    unidad:                  str = Field(default="Unidad", max_length=20)
    indicador_bien_servicio: int = Field(default=2, ge=1, le=2)

    @field_validator("itbis_tasa")
    @classmethod
    def validar_itbis(cls, v):
        if v not in (Decimal("0"), Decimal("16"), Decimal("18")):
            raise ValueError("Tasa ITBIS debe ser 0, 16 o 18")
        return v


class FacturaPayload(BaseModel):
    tipo_ecf:           int = Field(..., ge=31, le=47)
    rnc_comprador:      Optional[str] = Field(None, min_length=9, max_length=11)
    nombre_comprador:   Optional[str] = Field(None, max_length=255)
    tipo_rnc_comprador: str = Field(default="1", pattern=r"^[123]$")
    fecha_emision:      date
    items:              List[ItemPayload] = Field(..., min_length=1, max_length=200)
    ncf_referencia:     Optional[str] = Field(None, min_length=13, max_length=13)
    fecha_ncf_referencia: Optional[date] = None
    codigo_modificacion: str = Field(default="1", pattern=r"^[1234]$")
    moneda:             str = Field(default="DOP", min_length=3, max_length=3)
    tipo_cambio:        Decimal = Field(default=Decimal("1"), gt=0)
    tipo_pago:          str = Field(default="1", pattern=r"^[123]$")
    tipo_ingresos:      str = Field(default="01", pattern=r"^0[1-5]$")
    indicador_envio_diferido: int = Field(default=0, ge=0, le=1)
    direccion_comprador: Optional[str] = Field(None, max_length=255)
    odoo_move_id:       Optional[str] = Field(None, max_length=64)
    odoo_move_name:     Optional[str] = Field(None, max_length=64)

    @field_validator("tipo_ecf")
    @classmethod
    def validar_tipo_ecf(cls, v):
        tipos_validos = {31, 32, 33, 34, 41, 43, 44, 45, 46, 47}
        if v not in tipos_validos:
            raise ValueError(f"Tipo e-CF no válido. Válidos: {tipos_validos}")
        return v

    @field_validator("rnc_comprador")
    @classmethod
    def validar_rnc(cls, v):
        if v and not v.isdigit():
            raise ValueError("RNC/Cédula debe contener solo dígitos")
        return v

    @model_validator(mode="after")
    def validar_referencias(self):
        if self.ncf_referencia and not (self.ncf_referencia.startswith("E") and len(self.ncf_referencia) == 13):
            raise ValueError("NCF de referencia debe tener formato E + 12 dígitos")
        if self.tipo_ecf in (33, 34) and not self.ncf_referencia:
            raise ValueError(f"Tipo e-CF {self.tipo_ecf} requiere ncf_referencia obligatorio (Norma DGII)")
        if self.ncf_referencia and not self.fecha_ncf_referencia:
            raise ValueError("fecha_ncf_referencia es requerida cuando ncf_referencia está presente")
        return self


# Endpoints

@app.post("/v1/ecf/emitir", status_code=202)
async def emitir_ecf(
    payload: FacturaPayload,
    tenant: dict = Depends(get_tenant),
    db:     asyncpg.Pool = Depends(get_db),
    redis:  aioredis.Redis = Depends(get_redis),
    x_idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    """
    Recibe una factura de Odoo, asigna NCF y encola para procesamiento.
    Retorna el NCF asignado inmediatamente (respuesta 202 Accepted).
    El estado final llega via webhook cuando la DGII responde.
    """

    # Deduplicación por Idempotency-Key (ventana de 24h)
    if x_idempotency_key:
        idem_key = f"idem:{tenant['id']}:{x_idempotency_key}"
        cached = await redis.get(idem_key)
        if cached:
            return json.loads(cached)

    # Asignar NCF y crear registro en una sola transacción (evita NCF huérfanos)
    ecf_id = str(uuid.uuid4())
    schema = _safe_schema(tenant["schema_name"])

    TWO_PLACES = Decimal("0.01")
    subtotal   = sum(
        (i.cantidad * i.precio_unitario - i.descuento).quantize(TWO_PLACES)
        for i in payload.items
    )
    total_itbis = sum(
        ((i.cantidad * i.precio_unitario - i.descuento) * i.itbis_tasa / 100).quantize(TWO_PLACES)
        for i in payload.items
    )
    total = (subtotal + total_itbis).quantize(TWO_PLACES)

    async with db.acquire() as conn:
        async with conn.transaction():
            # Asignar NCF de forma atómica (dentro de la transacción)
            ncf = await conn.fetchval(
                "SELECT public.next_ncf($1, $2)",
                tenant["id"], payload.tipo_ecf
            )

            await conn.execute(f"""
                INSERT INTO {schema}.ecf
                    (id, ncf, tipo_ecf, estado, rnc_comprador, nombre_comprador,
                     fecha_emision, subtotal, itbis, total, moneda, tipo_cambio,
                     odoo_move_id, odoo_move_name, referencia_ncf,
                     fecha_ncf_referencia, codigo_modificacion,
                     tipo_pago, tipo_ingresos, tipo_rnc_comprador,
                     indicador_envio_diferido, direccion_comprador)
                VALUES ($1,$2,$3,'pendiente',$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,
                        $15,$16,$17,$18,$19,$20,$21)
            """,
                uuid.UUID(ecf_id), ncf, payload.tipo_ecf,
                payload.rnc_comprador, payload.nombre_comprador,
                payload.fecha_emision, subtotal, total_itbis, total,
                payload.moneda, payload.tipo_cambio,
                payload.odoo_move_id, payload.odoo_move_name,
                payload.ncf_referencia,
                payload.fecha_ncf_referencia, payload.codigo_modificacion,
                payload.tipo_pago, payload.tipo_ingresos, payload.tipo_rnc_comprador,
                payload.indicador_envio_diferido, payload.direccion_comprador,
            )

            # Items
            for idx, item in enumerate(payload.items, 1):
                await conn.execute(f"""
                    INSERT INTO {schema}.ecf_items
                        (ecf_id, linea, descripcion, cantidad, precio_unitario,
                         descuento, itbis_tasa, itbis_monto, subtotal, unidad,
                         indicador_bien_servicio)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                    uuid.UUID(ecf_id), idx,
                    item.descripcion, item.cantidad, item.precio_unitario,
                    item.descuento, item.itbis_tasa,
                    ((item.cantidad * item.precio_unitario - item.descuento) * item.itbis_tasa / 100).quantize(TWO_PLACES),
                    (item.cantidad * item.precio_unitario - item.descuento).quantize(TWO_PLACES),
                    item.unidad, item.indicador_bien_servicio,
                )

            # Incrementar contador mensual
            await conn.execute(
                "UPDATE public.tenants SET ecf_emitidos_mes = ecf_emitidos_mes + 1 WHERE id = $1",
                tenant["id"]
            )

    # Encolar para procesamiento async
    mensaje = json.dumps({
        "ecf_id":      ecf_id,
        "tenant_id":   str(tenant["id"]),
        "schema_name": schema,
        "ncf":         ncf,
        "tipo_ecf":    payload.tipo_ecf,
        "intento":     1,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    })
    await redis.rpush("ecf:pending", mensaje)

    logger.info("ECF encolado. Tenant=%s NCF=%s", tenant["rnc"], ncf)

    # Auditoría
    await _audit_log(
        db, str(tenant["id"]), "ecf.emitir",
        entidad="ecf", entidad_id=ecf_id,
        detalle={"ncf": ncf, "tipo_ecf": payload.tipo_ecf},
    )

    respuesta = {
        "ncf":    ncf,
        "ecf_id": ecf_id,
        "estado": "pendiente",
        "mensaje": "e-CF encolado para procesamiento. El estado final llegará via webhook.",
    }

    # Guardar respuesta para idempotencia (24h TTL)
    if x_idempotency_key:
        idem_key = f"idem:{tenant['id']}:{x_idempotency_key}"
        await redis.set(idem_key, json.dumps(respuesta), ex=86400)

    return respuesta


@app.get("/v1/ecf/{ncf}/estado")
async def consultar_estado(
    ncf: str,
    tenant: dict = Depends(get_tenant),
    db: asyncpg.Pool = Depends(get_db),
):
    """Consulta el estado actual de un e-CF por su NCF."""
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT ncf, estado, cufe, intentos_envio, ultimo_error, created_at, approved_at "
            f"FROM {schema}.ecf WHERE ncf = $1",
            ncf
        )
    if not row:
        raise HTTPException(status_code=404, detail="NCF no encontrado")

    return dict(row)


@app.get("/v1/ecf/{ncf}/xml")
async def descargar_xml(
    ncf: str,
    tenant: dict = Depends(get_tenant),
    db: asyncpg.Pool = Depends(get_db),
):
    """
    Descarga el XML firmado del e-CF.
    Requerido para cumplimiento (retención 10 años según DGII).
    """
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT xml_firmado, estado FROM {schema}.ecf WHERE ncf = $1", ncf
        )
    if not row or not row["xml_firmado"]:
        raise HTTPException(status_code=404, detail="XML no disponible")

    from fastapi.responses import Response
    return Response(
        content=bytes(row["xml_firmado"]),
        media_type="application/xml",
        headers={"Content-Disposition": f"attachment; filename={ncf}.xml"}
    )


@app.get("/v1/reportes/606")
async def reporte_606(
    anio: int, mes: int,
    formato: ExportFormat = ExportFormat.json,
    tenant: dict = Depends(get_tenant),
    db: asyncpg.Pool = Depends(get_db),
):
    """Genera el reporte 606 (Compras) del período indicado.
    Formatos: json, txt (DGII), xlsx, pdf."""
    _validar_periodo(anio, mes)
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT ncf, rnc_proveedor, nombre_proveedor, tipo_bienes,
                   fecha_comprobante, fecha_pago, monto_servicios, monto_bienes,
                   total_monto, itbis_facturado, itbis_retenido, isr_retencion
            FROM {schema}.compras
            WHERE EXTRACT(YEAR FROM fecha_comprobante) = $1
              AND EXTRACT(MONTH FROM fecha_comprobante) = $2
            ORDER BY fecha_comprobante, ncf
        """, anio, mes)
    registros = [dict(r) for r in rows]
    periodo = f"{anio}-{mes:02d}"
    keys = ["ncf", "rnc_proveedor", "nombre_proveedor", "tipo_bienes",
            "fecha_comprobante", "fecha_pago", "monto_servicios", "monto_bienes",
            "total_monto", "itbis_facturado", "itbis_retenido", "isr_retencion"]
    return _build_response(
        registros, formato, "606", HEADERS_606, keys,
        "606 — Compras", tenant["rnc"], periodo, _606_to_txt,
    )


@app.get("/v1/reportes/607")
async def reporte_607(
    anio: int, mes: int,
    formato: ExportFormat = ExportFormat.json,
    tenant: dict = Depends(get_tenant),
    db: asyncpg.Pool = Depends(get_db),
):
    """Genera el reporte 607 (Ventas de Bienes y Servicios) del período.
    Formatos: json, txt (DGII), xlsx, pdf."""
    _validar_periodo(anio, mes)
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT ncf, tipo_ecf, rnc_comprador, nombre_comprador,
                   tipo_rnc_comprador, fecha_emision, tipo_ingresos,
                   subtotal AS monto_facturado, itbis AS itbis_facturado,
                   total, tipo_pago, referencia_ncf, estado
            FROM {schema}.ecf
            WHERE estado IN ('aprobado', 'condicionado')
              AND EXTRACT(YEAR FROM fecha_emision) = $1
              AND EXTRACT(MONTH FROM fecha_emision) = $2
            ORDER BY fecha_emision, ncf
        """, anio, mes)
    registros = [dict(r) for r in rows]
    periodo = f"{anio}-{mes:02d}"
    keys = ["ncf", "tipo_ecf", "rnc_comprador", "nombre_comprador",
            "tipo_rnc_comprador", "fecha_emision", "tipo_ingresos",
            "monto_facturado", "itbis_facturado", "total", "tipo_pago",
            "referencia_ncf", "estado"]
    return _build_response(
        registros, formato, "607", HEADERS_607, keys,
        "607 — Ventas", tenant["rnc"], periodo, _607_to_txt,
    )


@app.get("/v1/reportes/608")
async def reporte_608(
    anio: int, mes: int,
    formato: ExportFormat = ExportFormat.json,
    tenant: dict = Depends(get_tenant),
    db: asyncpg.Pool = Depends(get_db),
):
    """Genera el reporte 608 (Anulaciones) del periodo indicado. Requerido por DGII.
    Formatos: json, txt (DGII), xlsx, pdf."""
    _validar_periodo(anio, mes)
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT e.ncf, e.tipo_ecf, e.fecha_emision,
                   l.estado_new AS tipo_anulacion,
                   l.created_at AS fecha_anulacion
            FROM {schema}.ecf e
            JOIN {schema}.ecf_estado_log l ON l.ecf_id = e.id
            WHERE l.estado_new = 'anulado'
              AND EXTRACT(YEAR FROM l.created_at) = $1
              AND EXTRACT(MONTH FROM l.created_at) = $2
            ORDER BY l.created_at, e.ncf
        """, anio, mes)
    registros = [dict(r) for r in rows]
    periodo = f"{anio}-{mes:02d}"
    keys = ["ncf", "tipo_ecf", "fecha_emision", "tipo_anulacion", "fecha_anulacion"]
    return _build_response(
        registros, formato, "608", HEADERS_608, keys,
        "608 — Anulaciones", tenant["rnc"], periodo, _608_to_txt,
    )


@app.get("/health")
async def health():
    """Endpoint de salud para el load balancer interno. No expone info sensible."""
    return {"status": "ok"}


# Validación de e-CF (sin enviar a DGII)

@app.post("/v1/ecf/validar")
async def validar_ecf(
    payload: FacturaPayload,
    tenant: dict = Depends(get_tenant),
):
    """
    Valida un e-CF localmente sin encolarlo ni enviarlo a la DGII.
    Útil para que Odoo valide antes de emitir.
    """
    errores = []

    # Validaciones de negocio
    if payload.tipo_ecf != 32 and not payload.rnc_comprador:
        errores.append("RNC comprador es requerido para e-CF tipo " + str(payload.tipo_ecf))

    if payload.tipo_ecf in (33, 34) and not payload.ncf_referencia:
        errores.append(f"e-CF tipo {payload.tipo_ecf} requiere ncf_referencia")

    if payload.tipo_ecf in (33, 34) and not payload.fecha_ncf_referencia:
        errores.append(f"e-CF tipo {payload.tipo_ecf} requiere fecha_ncf_referencia")

    total = sum(
        (i.cantidad * i.precio_unitario - i.descuento)
        * (1 + i.itbis_tasa / 100)
        for i in payload.items
    )
    if total <= 0:
        errores.append("El total del e-CF debe ser mayor a 0")

    if payload.moneda != "DOP" and payload.tipo_cambio == Decimal("1"):
        errores.append("Tipo de cambio debe ser diferente de 1 para moneda extranjera")

    if errores:
        return {"valido": False, "errores": errores}

    return {"valido": True, "errores": []}


# Anulación de e-CF

class AnularPayload(BaseModel):
    ncf:    str = Field(..., min_length=13, max_length=13)
    motivo: str = Field(..., min_length=1, max_length=2)
    nota:   str = Field(default="", max_length=500)


@app.post("/v1/ecf/anular")
async def anular_ecf(
    payload: AnularPayload,
    tenant: dict = Depends(get_tenant),
    db:     asyncpg.Pool = Depends(get_db),
    redis:  aioredis.Redis = Depends(get_redis),
):
    """Solicita la anulación de un e-CF aprobado."""
    schema = _safe_schema(tenant["schema_name"])

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT id, ncf, estado, tipo_ecf FROM {schema}.ecf WHERE ncf = $1",
            payload.ncf,
        )
    if not row:
        raise HTTPException(status_code=404, detail="NCF no encontrado")
    if row["estado"] == "anulado":
        raise HTTPException(status_code=409, detail="e-CF ya está anulado")
    if row["estado"] not in ("aprobado", "condicionado"):
        raise HTTPException(
            status_code=422,
            detail=f"Solo se pueden anular e-CF aprobados o condicionados. Estado actual: {row['estado']}",
        )

    # Encolar petición de anulación
    mensaje = json.dumps({
        "tipo":        "anulacion",
        "ecf_id":      str(row["id"]),
        "tenant_id":   str(tenant["id"]),
        "schema_name": schema,
        "ncf":         payload.ncf,
        "motivo":      payload.motivo,
        "nota":        payload.nota,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    })
    await redis.rpush("ecf:pending", mensaje)

    # Marcar como en proceso de anulación (NO como anulado definitivo)
    async with db.acquire() as conn:
        await conn.execute(
            f"INSERT INTO {schema}.ecf_estado_log (ecf_id, estado_prev, estado_new, detalle) "
            f"VALUES ($1, $2, 'anulacion_pendiente', $3)",
            row["id"], row["estado"],
            f"Motivo: {payload.motivo}. {payload.nota}".strip(),
        )
        await conn.execute(
            f"UPDATE {schema}.ecf SET estado = 'anulacion_pendiente', updated_at = NOW() WHERE id = $1",
            row["id"],
        )

    logger.info("e-CF %s en anulación pendiente. Tenant=%s", payload.ncf, tenant["rnc"])

    # Auditoría
    await _audit_log(
        db, str(tenant["id"]), "ecf.anular",
        entidad="ecf", entidad_id=str(row["id"]),
        detalle={"ncf": payload.ncf, "motivo": payload.motivo},
    )

    return {"ncf": payload.ncf, "estado": "anulacion_pendiente", "mensaje": "Anulación encolada, pendiente confirmación DGII"}


# Consulta batch de estados

class BatchStatusPayload(BaseModel):
    ncfs: List[str] = Field(..., min_length=1, max_length=100)


@app.post("/v1/ecf/estado-batch")
async def estado_batch(
    payload: BatchStatusPayload,
    tenant: dict = Depends(get_tenant),
    db: asyncpg.Pool = Depends(get_db),
):
    """Consulta estados de múltiples NCFs en una sola petición."""
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT ncf, estado, cufe, track_id, security_code, qr_url, "
            f"intentos_envio, ultimo_error, created_at, approved_at "
            f"FROM {schema}.ecf WHERE ncf = ANY($1)",
            payload.ncfs,
        )

    encontrados = {r["ncf"]: dict(r) for r in rows}
    resultado = []
    for ncf in payload.ncfs:
        if ncf in encontrados:
            resultado.append(encontrados[ncf])
        else:
            resultado.append({"ncf": ncf, "estado": "no_encontrado"})

    return {"resultados": resultado}


# Métricas básicas (Prometheus scrape-compatible, requiere autenticación)

METRICS_API_KEY = os.environ.get("METRICS_API_KEY", "")


@app.get("/metrics")
async def metrics(
    request: Request,
    db: asyncpg.Pool = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Métricas para Prometheus. Requiere METRICS_API_KEY en header Authorization."""
    auth = request.headers.get("Authorization", "")
    if not METRICS_API_KEY or not auth:
        raise HTTPException(status_code=401, detail="No autorizado")
    expected = f"Bearer {METRICS_API_KEY}"
    if not hmac.compare_digest(auth, expected):
        raise HTTPException(status_code=401, detail="No autorizado")
    pending   = await redis.llen("ecf:pending")
    retry_cnt = await redis.zcard("ecf:retry")
    dlq_cnt   = await redis.llen("ecf:dlq")

    async with db.acquire() as conn:
        tenant_count = await conn.fetchval("SELECT COUNT(*) FROM public.tenants WHERE deleted_at IS NULL")

    from fastapi.responses import PlainTextResponse
    body = (
        f"# HELP ecf_queue_pending Pending ECFs in queue\n"
        f"# TYPE ecf_queue_pending gauge\n"
        f"ecf_queue_pending {pending}\n"
        f"# HELP ecf_queue_retry ECFs waiting for retry\n"
        f"# TYPE ecf_queue_retry gauge\n"
        f"ecf_queue_retry {retry_cnt}\n"
        f"# HELP ecf_queue_dlq ECFs in dead letter queue\n"
        f"# TYPE ecf_queue_dlq gauge\n"
        f"ecf_queue_dlq {dlq_cnt}\n"
        f"# HELP ecf_tenants_active Active tenants\n"
        f"# TYPE ecf_tenants_active gauge\n"
        f"ecf_tenants_active {tenant_count}\n"
    )
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")
