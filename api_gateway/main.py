import asyncio
import hashlib
import hmac
import json
import logging
import os
import pathlib
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import asyncpg
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from lxml import etree
from pydantic import BaseModel, Field, field_validator, model_validator, AliasChoices

from api_gateway.admin import (
    router as admin_router,
)
from api_gateway.admin import (
    set_db_pool as admin_set_db_pool,
)
from api_gateway.admin import (
    set_redis as admin_set_redis,
)
from api_gateway.reportes import (
    HEADERS_606,
    HEADERS_607,
    HEADERS_608,
    ExportFormat,
    _606_to_txt,
    _607_to_txt,
    _608_to_txt,
    _build_response,
)
from ecf_core.cert_vault import CertVault, CertVaultRepository
from ecf_core.dgii_client import DGIIClient
from ecf_core.ecf_core_service import ECFSigner
from ecf_core.ecf_interchange_service import ECFInterchangeService
from ecf_core.ecf_recibidas_service import ECFRecibidasService
from ecf_core.utils import safe_schema as _safe_schema
from ecf_core.utils import validar_rnc_o_cedula

logger = logging.getLogger(__name__)


def _validar_periodo(anio: int, mes: int):
    """Valida que año y mes sean razonables para reportes DGII."""
    if not (2020 <= anio <= 2100):
        raise HTTPException(status_code=422, detail=f"Año fuera de rango válido: {anio}")
    if not (1 <= mes <= 12):
        raise HTTPException(status_code=422, detail=f"Mes inválido: {mes}")

# Lifespan (reemplaza on_event deprecated)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup. Si los tests pre-inyectaron pools mock, no los pisamos.
    owns_resources = False
    if not getattr(app.state, "db_pool", None):
        app.state.db_pool = await asyncpg.create_pool(
            dsn=os.environ["DATABASE_URL"],
            min_size=5,
            max_size=20,
        )
        owns_resources = True
    if not getattr(app.state, "redis", None):
        app.state.redis = await aioredis.from_url(
            os.environ["REDIS_URL"],
            password=os.environ.get("REDIS_PASSWORD"),
            decode_responses=True,
        )
    admin_set_db_pool(app.state.db_pool)
    admin_set_redis(app.state.redis)
    try:
        from ecf_core.platform_config import load_psfe_from_db

        if await load_psfe_from_db(app.state.db_pool):
            logger.info("PSFE plataforma listo (DB)")
        elif os.environ.get("PSFE_CERT_B64"):
            logger.info("PSFE plataforma listo (.env)")
        else:
            logger.warning("PSFE no configurado — subir en panel Plataforma o .env")
    except Exception as exc:
        logger.warning("PSFE startup check: %s", exc)
    logger.info("API Gateway iniciado")
    yield
    # Shutdown — sólo cerramos los recursos que nosotros creamos.
    if owns_resources:
        await app.state.db_pool.close()
        await app.state.redis.aclose()


# App

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").split(",")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()]

if not ALLOWED_ORIGINS:
    logger.warning("ALLOWED_ORIGINS no configurado — CORS deshabilitado")

app = FastAPI(
    title="RENECF — DGII Gateway",
    version=os.environ.get("DGII_SOFTWARE_VERSION", "2.5"),
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
_landing_file = pathlib.Path(__file__).resolve().parent.parent / "landing" / "index.html"

@app.get("/", include_in_schema=False)
async def landing():
    if _landing_file.is_file():
        return FileResponse(str(_landing_file), media_type="text/html")
    return JSONResponse({"service": "RENECF", "status": "running"})

@app.get("/renacelogo.svg", include_in_schema=False)
async def logo():
    logo_file = pathlib.Path(__file__).resolve().parent.parent / "landing" / "renacelogo.svg"
    if logo_file.is_file():
        return FileResponse(str(logo_file), media_type="image/svg+xml")
    return Response(status_code=404)

@app.get("/apple-touch-icon.png", include_in_schema=False)
async def apple_touch_icon():
    icon_file = pathlib.Path(__file__).resolve().parent.parent / "landing" / "apple-touch-icon.png"
    if icon_file.is_file():
        return FileResponse(str(icon_file), media_type="image/png")
    return Response(status_code=404)

# ── Endpoints de homologación DGII mock — SOLO en modo simulación ──
# En certificación/producción estos mocks son peligrosos: pueden enmascarar
# una mala configuración de URLs DGII o servir como "DGII falsa" a terceros.
_ECF_AMBIENTE_SISTEMA = os.environ.get("ECF_AMBIENTE", "").lower()
if _ECF_AMBIENTE_SISTEMA in {"simulacion", "sim"}:
    @app.post("/fe/aprobacioncomercial/api/ecf", include_in_schema=False)
    async def recibir_aprobacion_comercial_mock():
        return JSONResponse(status_code=200, content={"status": "received"})

    @app.get("/fe/autenticacion/api/semilla", include_in_schema=False)
    @app.get("/Autenticacion/api/Autenticacion/Semilla", include_in_schema=False)
    async def semilla_mock():
        return Response(status_code=200, content="<Semilla>MockSeed</Semilla>", media_type="application/xml")

    @app.post("/fe/autenticacion/api/validacioncertificado", include_in_schema=False)
    @app.post("/Autenticacion/api/Autenticacion/ValidarSemilla", include_in_schema=False)
    async def validacion_certificado_mock():
        return JSONResponse(status_code=200, content={"token": "mock_token"})

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


# Límites para el endpoint público de recepción (sin API key)
IP_RATE_LIMIT_MAX = int(os.environ.get("IP_RATE_LIMIT_MAX", "30"))
IP_RATE_LIMIT_WINDOW = int(os.environ.get("IP_RATE_LIMIT_WINDOW", "60"))


def _get_client_ip(request: Request) -> str:
    """Extrae la IP real del cliente respetando X-Forwarded-For del proxy.

    Se toma el ÚLTIMO valor de la cadena: es el único añadido por nuestro
    proxy de confianza (Traefik). El primero puede ser falsificado por el
    cliente para evadir el rate-limit por IP.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


async def _check_rate_limit_ip(request: Request, redis: aioredis.Redis):
    """Limita requests al endpoint público por IP de origen."""
    ip = _get_client_ip(request)
    key = f"rl:ip:{ip}"
    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, IP_RATE_LIMIT_WINDOW)
    if current > IP_RATE_LIMIT_MAX:
        ttl = await redis.ttl(key)
        raise HTTPException(
            status_code=429,
            detail="Demasiadas solicitudes desde esta IP.",
            headers={"Retry-After": str(ttl if ttl > 0 else IP_RATE_LIMIT_WINDOW)},
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
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    db: asyncpg.Pool = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict:
    """Autentica y retorna el tenant por su API key."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key requerido")
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
        raise HTTPException(status_code=403, detail="Certificado digital vencido")

    return dict(tenant)

# --- Endpoints ---

@app.get("/v1/health")
async def health_tenant(tenant: dict = Depends(get_tenant)):
    """Endpoint de monitoreo y validación de conexión para los clientes."""
    cert_vencimiento = tenant.get("cert_vencimiento")
    dias_para_vencer = None
    if cert_vencimiento:
        dias_para_vencer = (cert_vencimiento - date.today()).days
    return {
        "status":             "online",
        "service":            "RENECF",
        "version":             app.version,
        "ambiente":           tenant["ambiente"],
        "rnc":                tenant["rnc"],
        "cert_vencimiento":   cert_vencimiento.isoformat() if cert_vencimiento else None,
        "cert_dias_restantes": dias_para_vencer,
        "ecf_emitidos_mes":   tenant.get("ecf_emitidos_mes", 0),
        "max_ecf_mensual":    tenant.get("max_ecf_mensual", 0),
        "timestamp":          datetime.now(timezone.utc).isoformat(),
    }


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
    items:              list[ItemPayload] = Field(..., min_length=1, max_length=200)
    ncf_referencia:     Optional[str] = Field(None, min_length=13, max_length=13)
    fecha_ncf_referencia: Optional[date] = None
    codigo_modificacion: str = Field(default="1", pattern=r"^[1234]$")
    moneda:             str = Field(default="DOP", min_length=3, max_length=3)
    tipo_cambio:        Decimal = Field(default=Decimal("1"), gt=0)
    # DGII: TipoPago acepta 1..9 (1=Contado, 2=Crédito, 3=Gratuito, 4=Permuta,
    # 5=Pagos por Cuenta de Terceros, 6=Otra Forma de Pago, 7=Pagos al Exterior, ...).
    tipo_pago:          str = Field(default="1", pattern=r"^[1-9]$")
    # DGII: TipoIngresos acepta 01..06 (incluye 06 = Otros Ingresos).
    tipo_ingresos:      str = Field(default="01", pattern=r"^0[1-6]$")
    indicador_envio_diferido: int = Field(default=0, ge=0, le=1)
    fecha_limite_pago:  Optional[date] = None
    direccion_comprador: Optional[str] = Field(None, max_length=255)
    ambiente_emision: Optional[str] = Field(
        None, pattern=r"^(simulacion|certificacion|produccion)$",
        description="Ambiente solicitado por Odoo; el worker lo usa si el tenant lo permite",
    )
    odoo_move_id:       Optional[str] = Field(None, max_length=64, validation_alias=AliasChoices("external_id", "odoo_move_id"))
    odoo_move_name:     Optional[str] = Field(None, max_length=64, validation_alias=AliasChoices("external_name", "odoo_move_name"))

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
        if v and not validar_rnc_o_cedula(v):
            raise ValueError("RNC/Cédula inválido (dígito verificador mod-11 incorrecto)")
        return v

    @model_validator(mode="after")
    def validar_referencias(self):
        if self.tipo_ecf != 32 and not self.rnc_comprador:
            raise ValueError(f"RNC comprador es requerido para e-CF tipo {self.tipo_ecf}")
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

    # Deduplicación por Idempotency-Key (ventana de 24h).
    # SET NX cierra la ventana TOCTOU: dos requests concurrentes con la misma
    # key no pueden asignar dos NCF distintos.
    idem_key = None
    if x_idempotency_key:
        idem_key = f"idem:{tenant['id']}:{x_idempotency_key}"
        adquirido = await redis.set(idem_key, "__processing__", nx=True, ex=120)
        if not adquirido:
            cached = await redis.get(idem_key)
            if cached and cached != "__processing__":
                return json.loads(cached)
            # Otra request con la misma key está en curso en este instante
            raise HTTPException(
                status_code=409,
                detail="Solicitud duplicada en curso (Idempotency-Key). Reintente en unos segundos.",
            )

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
            # Cuota mensual atómica: dos requests concurrentes no pueden
            # superar max_ecf_mensual (el UPDATE condicional serializa).
            cupo = await conn.fetchval(
                "UPDATE public.tenants SET ecf_emitidos_mes = ecf_emitidos_mes + 1 "
                "WHERE id = $1 AND ecf_emitidos_mes < max_ecf_mensual "
                "RETURNING ecf_emitidos_mes",
                tenant["id"],
            )
            if cupo is None:
                if idem_key:
                    await redis.delete(idem_key)
                raise HTTPException(status_code=429, detail="Límite mensual de e-CF alcanzado")

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

            # (el contador mensual ya se incrementó atómicamente arriba)

    # Encolar para procesamiento async
    mensaje = json.dumps({
        "ecf_id":      ecf_id,
        "tenant_id":   str(tenant["id"]),
        "schema_name": schema,
        "ncf":         ncf,
        "tipo_ecf":    payload.tipo_ecf,
        "ambiente_emision": payload.ambiente_emision,
        "fecha_limite_pago": (
            payload.fecha_limite_pago.isoformat() if payload.fecha_limite_pago else None
        ),
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

    # Guardar respuesta para idempotencia (24h TTL) — reemplaza el placeholder NX
    if idem_key:
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
            f"SELECT ncf, estado, codigo_seguridad, track_id, security_code, qr_url, "
            f"intentos_envio, ultimo_error, created_at, approved_at "
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


# ──────────────────────────────────────────────────────────────────────────────
# Reportes contables ampliados — IT-1, IR-17, NCF status, conciliación 606
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/v1/reportes/it1")
async def reporte_it1(
    anio: int, mes: int,
    tenant: dict = Depends(get_tenant),
    db: asyncpg.Pool = Depends(get_db),
):
    """Borrador IT-1 (Declaración Mensual de ITBIS).

    Consolida:
      - ITBIS facturado en ventas (607) — débito fiscal.
      - ITBIS adelantado en compras (606) — crédito fiscal.
      - ITBIS retenido por agentes a este contribuyente.
      - ITBIS retenido por este contribuyente a terceros.
      - Saldo a pagar (>0) o crédito a favor (<0).
    """
    _validar_periodo(anio, mes)
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        ventas = await conn.fetchrow(f"""
            SELECT COALESCE(SUM(subtotal), 0) AS base,
                   COALESCE(SUM(itbis), 0) AS itbis,
                   COUNT(*) AS cantidad
            FROM {schema}.ecf
            WHERE estado IN ('aprobado','condicionado')
              AND EXTRACT(YEAR FROM fecha_emision) = $1
              AND EXTRACT(MONTH FROM fecha_emision) = $2
        """, anio, mes)
        compras = await conn.fetchrow(f"""
            SELECT COALESCE(SUM(monto_servicios + monto_bienes), 0) AS base,
                   COALESCE(SUM(itbis_facturado), 0) AS itbis_adelantado,
                   COALESCE(SUM(itbis_retenido), 0) AS itbis_retenido_a_terceros,
                   COUNT(*) AS cantidad
            FROM {schema}.compras
            WHERE EXTRACT(YEAR FROM fecha_comprobante) = $1
              AND EXTRACT(MONTH FROM fecha_comprobante) = $2
        """, anio, mes)

    debito_fiscal       = Decimal(str(ventas["itbis"]))
    credito_fiscal      = Decimal(str(compras["itbis_adelantado"]))
    retenido_a_terceros = Decimal(str(compras["itbis_retenido_a_terceros"]))
    saldo               = debito_fiscal - credito_fiscal + retenido_a_terceros

    return {
        "periodo":   f"{anio}-{mes:02d}",
        "rnc":       tenant["rnc"],
        "ventas": {
            "cantidad":      ventas["cantidad"],
            "base_imponible": str(ventas["base"]),
            "debito_fiscal":  str(debito_fiscal),
        },
        "compras": {
            "cantidad":               compras["cantidad"],
            "base_imponible":          str(compras["base"]),
            "credito_fiscal":          str(credito_fiscal),
            "itbis_retenido_a_terceros": str(retenido_a_terceros),
        },
        "saldo_a_pagar":         str(saldo) if saldo >= 0 else "0.00",
        "credito_a_favor":       str(-saldo) if saldo < 0 else "0.00",
        "moneda":                "DOP",
        "fecha_calculo":         datetime.now(timezone.utc).isoformat(),
    }


@app.get("/v1/reportes/ir17")
async def reporte_ir17(
    anio: int, mes: int,
    tenant: dict = Depends(get_tenant),
    db: asyncpg.Pool = Depends(get_db),
):
    """Reporte IR-17 (Retenciones a Terceros) del período.

    Lee de ``{schema}.retenciones`` que llena el módulo Odoo al validar pagos.
    """
    _validar_periodo(anio, mes)
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT ncf, rnc_retenido, cedula_retenido, nombre_retenido,
                   fecha, monto_pagado, isr_retenido
            FROM {schema}.retenciones
            WHERE EXTRACT(YEAR FROM fecha) = $1
              AND EXTRACT(MONTH FROM fecha) = $2
            ORDER BY fecha, ncf
        """, anio, mes)
    registros = [dict(r) for r in rows]
    total_pagado = sum(Decimal(str(r["monto_pagado"])) for r in registros)
    total_retenido = sum(Decimal(str(r["isr_retenido"])) for r in registros)
    return {
        "periodo":         f"{anio}-{mes:02d}",
        "rnc":             tenant["rnc"],
        "total_registros": len(registros),
        "total_pagado":    str(total_pagado),
        "total_retenido":  str(total_retenido),
        "retenciones":     [
            {**r, "fecha": r["fecha"].isoformat() if r.get("fecha") else None,
             "monto_pagado": str(r["monto_pagado"]),
             "isr_retenido": str(r["isr_retenido"])}
            for r in registros
        ],
    }


@app.get("/v1/ncf/secuencias")
async def listar_secuencias_ncf(
    tenant: dict = Depends(get_tenant),
    db: asyncpg.Pool = Depends(get_db),
):
    """Estado de las secuencias NCF activas del tenant — alerta cuando faltan < 1000."""
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """SELECT tipo_ecf, prefijo, secuencia_actual, secuencia_max, activo
               FROM public.ncf_sequences
               WHERE tenant_id = $1
               ORDER BY tipo_ecf""",
            tenant["id"],
        )
    secuencias = []
    for r in rows:
        disponibles = r["secuencia_max"] - r["secuencia_actual"]
        consumo = int(100 * r["secuencia_actual"] / r["secuencia_max"]) if r["secuencia_max"] else 0
        nivel = "ok"
        if disponibles == 0:
            nivel = "agotada"
        elif disponibles < 1000:
            nivel = "critico"
        elif disponibles < 10000:
            nivel = "alerta"
        secuencias.append({
            "tipo_ecf":         r["tipo_ecf"],
            "prefijo":          r["prefijo"],
            "secuencia_actual": r["secuencia_actual"],
            "secuencia_max":    r["secuencia_max"],
            "disponibles":      disponibles,
            "consumo_pct":      consumo,
            "activo":           r["activo"],
            "nivel_alerta":     nivel,
        })
    return {"rnc": tenant["rnc"], "secuencias": secuencias}


@app.get("/v1/ecf/limbo")
async def listar_ecf_en_limbo(
    horas: int = 24,
    tenant: dict = Depends(get_tenant),
    db: asyncpg.Pool = Depends(get_db),
):
    """Lista e-CF en estado pendiente/enviado por más de N horas — alerta operativa."""
    if horas < 1 or horas > 720:
        raise HTTPException(status_code=422, detail="horas fuera de rango (1..720)")
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT ncf, tipo_ecf, estado, intentos_envio, ultimo_error,
                   created_at, sent_at, total
            FROM {schema}.ecf
            WHERE estado IN ('pendiente','enviado','anulacion_pendiente')
              AND created_at < NOW() - ($1::int * INTERVAL '1 hour')
            ORDER BY created_at
            LIMIT 500
        """, horas)
    return {
        "rnc":      tenant["rnc"],
        "umbral_horas": horas,
        "total":    len(rows),
        "registros": [dict(r) for r in rows],
    }


# ─────────────────────────────────────────────────────────────────────────────
# e-CF COMPRAS RECIBIDAS — endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/v1/compras")
async def listar_compras(
    anio:          int,
    mes:           int,
    estado_odoo:   Optional[str] = None,
    estado_erp:    Optional[str] = None,
    rnc_proveedor: Optional[str] = None,
    formato:       ExportFormat = ExportFormat.json,
    tenant:        dict = Depends(get_tenant),
    db:            asyncpg.Pool = Depends(get_db),
):
    """Lista los e-CF recibidos (compras) del período con filtros opcionales."""
    _validar_periodo(anio, mes)
    schema = _safe_schema(tenant["schema_name"])
    conditions = [
        "EXTRACT(YEAR FROM fecha_comprobante) = $1",
        "EXTRACT(MONTH FROM fecha_comprobante) = $2",
    ]
    params: list = [anio, mes]
    estado_filtro = estado_erp or estado_odoo
    if estado_filtro:
        params.append(estado_filtro)
        conditions.append(f"estado_odoo = ${len(params)}")
    if rnc_proveedor:
        params.append(rnc_proveedor)
        conditions.append(f"rnc_proveedor = ${len(params)}")
    where = " AND ".join(conditions)
    async with db.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT ncf, rnc_proveedor, nombre_proveedor, tipo_bienes,
                   fecha_comprobante, fecha_pago, monto_servicios, monto_bienes,
                   total_monto, itbis_facturado, itbis_retenido, isr_retencion,
                   estado_odoo, odoo_bill_id, codigo_seguridad, tipo_ecf, ambiente
            FROM {schema}.compras WHERE {where}
            ORDER BY fecha_comprobante, ncf
        """, *params)
    registros = [dict(r) for r in rows]
    for r in registros:
        r["estado_erp"] = r.get("estado_odoo")
        r["bill_id"] = r.get("odoo_bill_id")
    periodo = f"{anio}-{mes:02d}"
    keys = ["ncf","rnc_proveedor","nombre_proveedor","tipo_bienes",
            "fecha_comprobante","fecha_pago","monto_servicios","monto_bienes",
            "total_monto","itbis_facturado","itbis_retenido","isr_retencion"]
    if formato == ExportFormat.json:
        return {"periodo": periodo, "total": len(registros), "registros": registros}
    return _build_response(registros, formato, "606", HEADERS_606, keys,
        "606 \u2014 Compras", tenant["rnc"], periodo, _606_to_txt)


@app.post("/v1/compras/sincronizar", status_code=202)
async def sincronizar_compras(
    tenant: dict = Depends(get_tenant),
    db:     asyncpg.Pool = Depends(get_db),
):
    """Dispara manualmente la sincronización de e-CF recibidas con la DGII (202 Accepted)."""
    cert_repo = CertVaultRepository(db, CertVault())
    servicio  = ECFRecibidasService(db, cert_repo)
    asyncio.create_task(servicio.sincronizar_tenant(dict(tenant)))
    await _audit_log(db, str(tenant["id"]), "compras.sincronizar")
    return {"mensaje": "Sincronización iniciada.", "tenant": tenant["rnc"]}


@app.get("/v1/compras/{ncf}/xml")
async def descargar_xml_compra(
    ncf: str,
    tenant: dict = Depends(get_tenant),
    db:     asyncpg.Pool = Depends(get_db),
):
    """Descarga el XML original del e-CF recibido (retención 10 años DGII)."""
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        row = await conn.fetchrow(f"SELECT xml_original FROM {schema}.compras WHERE ncf = $1", ncf)
    if not row or not row["xml_original"]:
        raise HTTPException(status_code=404, detail="XML no disponible para este NCF")
    return Response(bytes(row["xml_original"]), media_type="application/xml",
        headers={"Content-Disposition": f"attachment; filename=compra_{ncf}.xml"})


@app.patch("/v1/compras/{ncf}/pagar")
async def registrar_pago_compra(
    ncf: str,
    fecha_pago: date,
    tenant: dict = Depends(get_tenant),
    db:     asyncpg.Pool = Depends(get_db),
):
    """Registra la fecha de pago — requerida por DGII para el reporte 606."""
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        result = await conn.execute(
            f"UPDATE {schema}.compras SET fecha_pago=$1, updated_at=NOW() WHERE ncf=$2",
            fecha_pago, ncf)
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="NCF no encontrado")
    return {"ncf": ncf, "fecha_pago": fecha_pago.isoformat()}


@app.patch("/v1/compras/{ncf}/estado-erp")
async def actualizar_estado_erp(
    ncf: str,
    estado_erp: str,
    bill_id: Optional[str] = None,
    tenant: dict = Depends(get_tenant),
    db:     asyncpg.Pool = Depends(get_db),
):
    """Actualiza el estado de procesamiento en el ERP (ej: Citrus, Odoo)."""
    if estado_erp not in {"nueva","enviada","procesada","error"}:
        raise HTTPException(status_code=422, detail="Estado inválido")
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        result = await conn.execute(
            f"UPDATE {schema}.compras SET estado_odoo=$1, odoo_bill_id=$2, updated_at=NOW() WHERE ncf=$3",
            estado_erp, bill_id, ncf)
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="NCF no encontrado")
    return {"ncf": ncf, "estado_erp": estado_erp, "bill_id": bill_id}


@app.patch("/v1/compras/{ncf}/estado-odoo")
async def actualizar_estado_odoo(
    ncf: str,
    estado_odoo: str,
    odoo_bill_id: Optional[str] = None,
    tenant: dict = Depends(get_tenant),
    db:     asyncpg.Pool = Depends(get_db),
):
    """[DEPRECATED] Actualiza el estado de procesamiento Odoo. Use /estado-erp en su lugar."""
    res = await actualizar_estado_erp(
        ncf=ncf,
        estado_erp=estado_odoo,
        bill_id=odoo_bill_id,
        tenant=tenant,
        db=db
    )
    return {
        "ncf": res["ncf"],
        "estado_odoo": res["estado_erp"],
        "odoo_bill_id": res["bill_id"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# INTERCAMBIO COMERCIAL — endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/v1/compras/{ncf}/aprobar")
async def aprobar_compra_comercial(
    ncf: str,
    tenant: dict = Depends(get_tenant),
    db:     asyncpg.Pool = Depends(get_db),
):
    """Genera y envía la Aprobación Comercial (ACECF, Estado=1) para una compra.

    Conforme a ``xsd/ACECF.xsd``: requiere RNCEmisor, eNCF, FechaEmision,
    MontoTotal, RNCComprador, Estado=1, FechaHoraAprobacionComercial.
    """
    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        compra = await conn.fetchrow(
            f"SELECT rnc_proveedor, fecha_comprobante, total_monto "
            f"FROM {schema}.compras WHERE ncf = $1",
            ncf,
        )
    if not compra:
        raise HTTPException(status_code=404, detail="Compra no encontrada")

    cert_repo = CertVaultRepository(db, CertVault())
    cert = await cert_repo.obtener_certificado(str(tenant["id"]))

    interchange = ECFInterchangeService(ECFSigner())
    cert_password_bytes = (cert["cert_password"] or "").encode()

    xml_firmado = await interchange.procesar_aprobacion_comercial(
        ncf=ncf,
        rnc_emisor=compra["rnc_proveedor"],
        rnc_comprador=tenant["rnc"],
        fecha_emision=compra["fecha_comprobante"],
        monto_total=compra["total_monto"],
        estado=1,
        cert_data=cert["cert_data"],
        cert_password=cert_password_bytes,
    )

    async with DGIIClient(tenant["ambiente"]) as client:
        client.set_certificate(cert["cert_data"], cert_password_bytes)
        await client._authenticate()
        resp = await client._client.post(
            "/fe/aprobacioncomercial/api/ecf",
            content=xml_firmado,
            headers=client._auth_headers(),
        )
        if resp.status_code not in (200, 202):
            raise HTTPException(status_code=502, detail=f"Error DGII: {resp.text}")

    async with db.acquire() as conn:
        await conn.execute(
            f"UPDATE {schema}.compras SET estado_comercial='aprobado', updated_at=NOW() WHERE ncf=$1",
            ncf,
        )

    await _audit_log(db, str(tenant["id"]), "compras.aprobar", entidad="compras", entidad_id=ncf)
    return {"status": "aprobado", "ncf": ncf}


class RechazarComercialPayload(BaseModel):
    motivo: str = Field(..., min_length=1, max_length=250)


@app.post("/v1/compras/{ncf}/rechazar")
async def rechazar_compra_comercial(
    ncf: str,
    payload: RechazarComercialPayload,
    tenant: dict = Depends(get_tenant),
    db:     asyncpg.Pool = Depends(get_db),
):
    """Genera y envía el Rechazo Comercial (ACECF, Estado=2) para una compra.

    Conforme a ``xsd/ACECF.xsd`` Estado=2 con DetalleMotivoRechazo (≤250 chars).
    """
    motivo = payload.motivo.strip()
    if not motivo:
        raise HTTPException(status_code=422, detail="El motivo de rechazo es obligatorio")

    schema = _safe_schema(tenant["schema_name"])
    async with db.acquire() as conn:
        compra = await conn.fetchrow(
            f"SELECT rnc_proveedor, fecha_comprobante, total_monto "
            f"FROM {schema}.compras WHERE ncf = $1",
            ncf,
        )
    if not compra:
        raise HTTPException(status_code=404, detail="Compra no encontrada")

    cert_repo = CertVaultRepository(db, CertVault())
    cert = await cert_repo.obtener_certificado(str(tenant["id"]))

    interchange = ECFInterchangeService(ECFSigner())
    cert_password_bytes = (cert["cert_password"] or "").encode()

    xml_firmado = await interchange.procesar_aprobacion_comercial(
        ncf=ncf,
        rnc_emisor=compra["rnc_proveedor"],
        rnc_comprador=tenant["rnc"],
        fecha_emision=compra["fecha_comprobante"],
        monto_total=compra["total_monto"],
        estado=2,
        motivo_rechazo=motivo[:250],
        cert_data=cert["cert_data"],
        cert_password=cert_password_bytes,
    )

    async with DGIIClient(tenant["ambiente"]) as client:
        client.set_certificate(cert["cert_data"], cert_password_bytes)
        await client._authenticate()
        resp = await client._client.post(
            "/fe/aprobacioncomercial/api/ecf",
            content=xml_firmado,
            headers=client._auth_headers(),
        )
        if resp.status_code not in (200, 202):
            raise HTTPException(status_code=502, detail=f"Error DGII: {resp.text}")

    async with db.acquire() as conn:
        await conn.execute(
            f"UPDATE {schema}.compras "
            f"SET estado_comercial='rechazado', motivo_rechazo=$1, updated_at=NOW() "
            f"WHERE ncf=$2",
            motivo, ncf,
        )

    await _audit_log(db, str(tenant["id"]), "compras.rechazar",
                     entidad="compras", entidad_id=ncf, detalle={"motivo": motivo})
    return {"status": "rechazado", "ncf": ncf}


async def _enviar_arecf_background(
    db: asyncpg.Pool,
    ncf: str,
    rnc_emisor: str,
    rnc_comprador: str,
    tenant_id: str,
    ambiente: str,
) -> None:
    """Envía ARECF Estado=0 (Recibido) a la DGII. Fire-and-forget — no bloquea la respuesta."""
    try:
        cert_repo = CertVaultRepository(db, CertVault())
        cert = await cert_repo.obtener_certificado(tenant_id)
        cert_password_bytes = (cert.get("cert_password") or "").encode()

        interchange = ECFInterchangeService(ECFSigner())
        xml_firmado = await interchange.procesar_acuse_recibo(
            ncf=ncf,
            rnc_emisor=rnc_emisor,
            rnc_comprador=rnc_comprador,
            cert_data=cert["cert_data"],
            cert_password=cert_password_bytes,
            estado=0,
        )

        async with DGIIClient(ambiente) as client:
            client.set_certificate(cert["cert_data"], cert_password_bytes)
            await client._authenticate()
            resp = await client._client.post(
                "/fe/acuserecibo/api/ecf",
                content=xml_firmado,
                headers=client._auth_headers(),
            )
            if resp.status_code not in (200, 202):
                logger.warning(
                    "ARECF rechazado por DGII: NCF=%s HTTP=%s body=%s",
                    ncf, resp.status_code, resp.text[:200],
                )
            else:
                logger.info("ARECF (Estado=0) enviado OK: NCF=%s", ncf)
    except Exception as exc:
        logger.error("Error enviando ARECF para NCF=%s: %s", ncf, exc)


@app.post("/fe/recepcion/api/ecf", include_in_schema=False)
async def recibir_ecf_externo(
    request: Request,
    db: asyncpg.Pool = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Endpoint público para recibir e-CF de otros contribuyentes.

    Este es el URL que se registra en la DGII como 'URL de Recepción'.
    Al recibir un e-CF nuevo responde 202 y dispara automáticamente
    el Acuse de Recibo (ARECF Estado=0) en segundo plano.
    """
    await _check_rate_limit_ip(request, redis)
    xml_bytes = await request.body()
    if not xml_bytes:
        return Response(status_code=400, content="Body vacío")

    # Verificar firma digital XML-DSig (obligatorio fuera de simulación)
    ambiente_sistema = os.environ.get("ECF_AMBIENTE", "certificacion")
    if ambiente_sistema != "simulacion":
        from ecf_core.xml_signature import verificar_firma_xml
        firma_ok, motivo = verificar_firma_xml(xml_bytes)
        if not firma_ok:
            logger.warning("e-CF entrante rechazado — firma inválida: %s", motivo)
            return Response(status_code=400, content=f"Firma digital inválida: {motivo}")

    # 1. Parsear identificadores y datos fiscales del e-CF
    def _texto(nodo) -> str:
        return (nodo.text or "").strip() if nodo is not None else ""

    def _monto(nodo) -> Decimal:
        try:
            return Decimal(_texto(nodo).replace(",", "")) if _texto(nodo) else Decimal("0")
        except Exception:
            return Decimal("0")

    try:
        root = etree.fromstring(xml_bytes)
        rnc_receptor_node = root.find(".//{*}RNCComprador") \
            or root.find(".//{*}RNCReceptor")
        ncf_node = root.find(".//{*}eNCF") or root.find(".//{*}NCF")
        rnc_emisor_node = root.find(".//{*}RNCEmisor")
        if rnc_receptor_node is None or ncf_node is None or rnc_emisor_node is None:
            return Response(status_code=400, content="XML inválido (faltan campos obligatorios)")
        rnc_receptor = _texto(rnc_receptor_node)
        ncf = _texto(ncf_node)
        rnc_emisor = _texto(rnc_emisor_node)

        # Datos fiscales para 606/ACECF (no bloquean la recepción si faltan)
        nombre_emisor = _texto(root.find(".//{*}RazonSocialEmisor"))[:255] or None
        total_monto = _monto(root.find(".//{*}MontoTotal"))
        itbis_facturado = _monto(root.find(".//{*}TotalITBIS"))
        tipo_ecf_recibido = None
        tipo_txt = _texto(root.find(".//{*}TipoeCF"))
        if tipo_txt.isdigit():
            tipo_ecf_recibido = int(tipo_txt)
        fecha_comprobante = date.today()
        fecha_txt = _texto(root.find(".//{*}FechaEmision"))
        if fecha_txt:
            for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
                try:
                    fecha_comprobante = datetime.strptime(fecha_txt, fmt).date()
                    break
                except ValueError:
                    continue
            else:
                try:
                    fecha_comprobante = date.fromisoformat(fecha_txt[:10])
                except ValueError:
                    pass
        codigo_seguridad_recibido = None
        sig_value = root.find(".//{http://www.w3.org/2000/09/xmldsig#}SignatureValue")
        if sig_value is not None and sig_value.text:
            import re as _re
            _clean = _re.sub(r"\s+", "", sig_value.text)
            codigo_seguridad_recibido = "".join(c for c in _clean if c.isalnum())[:6] or None
    except etree.XMLSyntaxError:
        return Response(status_code=400, content="XML mal formado")
    except Exception as exc:
        logger.warning("Error parseando e-CF entrante: %s", exc)
        return Response(status_code=400, content="XML inválido")

    # 2. Identificar tenant receptor (incluye ambiente para ARECF)
    async with db.acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, schema_name, rnc, ambiente FROM public.tenants "
            "WHERE rnc = $1 AND estado = 'activo' AND deleted_at IS NULL",
            rnc_receptor,
        )
    if not tenant:
        return Response(status_code=404, content="Receptor no registrado")

    # 3. Persistir como compra 'nueva' (idempotente)
    try:
        schema = _safe_schema(tenant["schema_name"])
    except ValueError:
        logger.error("Schema inválido para tenant %s", tenant["id"])
        return Response(status_code=500, content="Configuración inválida")

    async with db.acquire() as conn:
        result = await conn.execute(
            f"""INSERT INTO {schema}.compras (ncf, rnc_proveedor, nombre_proveedor,
                                              tipo_ecf, codigo_seguridad, xml_original,
                                              estado_odoo, fecha_comprobante,
                                              total_monto, itbis_facturado)
                VALUES ($1, $2, $3, $4, $5, $6, 'nueva', $7, $8, $9)
                ON CONFLICT (ncf) DO NOTHING""",
            ncf, rnc_emisor, nombre_emisor, tipo_ecf_recibido,
            codigo_seguridad_recibido, xml_bytes,
            fecha_comprobante, total_monto, itbis_facturado,
        )

    # "INSERT 0 1" → nuevo; "INSERT 0 0" → duplicado (no re-enviamos ARECF)
    if result.endswith("1"):
        asyncio.create_task(_enviar_arecf_background(
            db=db,
            ncf=ncf,
            rnc_emisor=rnc_emisor,
            rnc_comprador=rnc_receptor,
            tenant_id=str(tenant["id"]),
            ambiente=tenant["ambiente"] or "certificacion",
        ))

    logger.info("e-CF recibido: NCF=%s emisor=%s receptor=%s", ncf, rnc_emisor, rnc_receptor)
    return Response(status_code=202, content="e-CF recibido correctamente")


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
    ncfs: list[str] = Field(..., min_length=1, max_length=100)


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
            f"SELECT ncf, estado, codigo_seguridad, track_id, security_code, qr_url, "
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
        tenant_count = await conn.fetchval(
            "SELECT COUNT(*) FROM public.tenants WHERE deleted_at IS NULL"
        )
        # Lista de tenants para iterar y agregar contadores por schema.
        tenants_rows = await conn.fetch(
            "SELECT schema_name FROM public.tenants WHERE deleted_at IS NULL"
        )

    # Build per-state / per-tipo counters by querying each tenant schema
    estado_tipo_counts: dict = {}
    latency_by_tipo: dict = {}  # tipo_ecf → list of seconds

    async with db.acquire() as conn:
        for t_row in tenants_rows:
            try:
                schema = _safe_schema(t_row["schema_name"])
            except ValueError:
                continue
            rows = await conn.fetch(f"""
                SELECT estado, tipo_ecf, COUNT(*) AS cnt
                FROM {schema}.ecf
                GROUP BY estado, tipo_ecf
            """)
            for r in rows:
                key = (r["estado"], str(r["tipo_ecf"]))
                estado_tipo_counts[key] = estado_tipo_counts.get(key, 0) + r["cnt"]

            # Average approval latency per tipo (seconds)
            lat_rows = await conn.fetch(f"""
                SELECT tipo_ecf,
                       EXTRACT(EPOCH FROM AVG(approved_at - created_at))::float AS avg_secs
                FROM {schema}.ecf
                WHERE approved_at IS NOT NULL AND created_at IS NOT NULL
                GROUP BY tipo_ecf
            """)
            for lr in lat_rows:
                tipo = str(lr["tipo_ecf"])
                if lr["avg_secs"] is not None:
                    latency_by_tipo.setdefault(tipo, []).append(lr["avg_secs"])

    lines: list[str] = [
        "# HELP ecf_queue_pending Pending ECFs in queue",
        "# TYPE ecf_queue_pending gauge",
        f"ecf_queue_pending {pending}",
        "# HELP ecf_queue_retry ECFs waiting for retry",
        "# TYPE ecf_queue_retry gauge",
        f"ecf_queue_retry {retry_cnt}",
        "# HELP ecf_queue_dlq ECFs in dead letter queue",
        "# TYPE ecf_queue_dlq gauge",
        f"ecf_queue_dlq {dlq_cnt}",
        "# HELP ecf_tenants_active Active tenants",
        "# TYPE ecf_tenants_active gauge",
        f"ecf_tenants_active {tenant_count}",
        "# HELP ecf_total Total e-CF by state and tipo",
        "# TYPE ecf_total counter",
    ]
    for (estado, tipo), cnt in sorted(estado_tipo_counts.items()):
        lines.append(f'ecf_total{{estado="{estado}",tipo="{tipo}"}} {cnt}')

    if latency_by_tipo:
        lines += [
            "# HELP ecf_aprobacion_latency_avg_seconds Average approval latency per tipo",
            "# TYPE ecf_aprobacion_latency_avg_seconds gauge",
        ]
        for tipo, vals in sorted(latency_by_tipo.items()):
            avg = sum(vals) / len(vals)
            lines.append(f'ecf_aprobacion_latency_avg_seconds{{tipo="{tipo}"}} {avg:.3f}')

    body = "\n".join(lines) + "\n"
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")
