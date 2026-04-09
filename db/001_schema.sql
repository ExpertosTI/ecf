-- SAAS ECF DGII — Esquema multitenant PostgreSQL
-- Versión: 1.0.0
-- Compatible con requisitos de homologación DGII RD

-- Extensiones necesarias
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- SCHEMA: public — tablas del sistema / plataforma

-- Tenants (empresas clientes del SaaS)
CREATE TABLE public.tenants (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rnc                 VARCHAR(11) NOT NULL UNIQUE,          -- RNC sin guiones
    razon_social        VARCHAR(255) NOT NULL,
    nombre_comercial    VARCHAR(255),
    direccion           TEXT,
    telefono            VARCHAR(20),
    email               VARCHAR(255) NOT NULL,
    api_key             VARCHAR(64) NOT NULL UNIQUE,          -- SHA-256 hex
    api_key_hash        VARCHAR(128) NOT NULL,                -- bcrypt del api_key
    plan                VARCHAR(30) NOT NULL DEFAULT 'basico' CHECK (plan IN ('basico','profesional','enterprise')),
    estado              VARCHAR(20) NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente','activo','suspendido','cancelado')),
    schema_name         VARCHAR(63) NOT NULL UNIQUE,          -- schema PostgreSQL del tenant
    ambiente            VARCHAR(10) NOT NULL DEFAULT 'certificacion' CHECK (ambiente IN ('certificacion','produccion')),
    odoo_webhook_url    TEXT,                                 -- URL donde notificar callbacks
    odoo_webhook_secret VARCHAR(128),                         -- HMAC-SHA256 secret para validar
    max_ecf_mensual     INTEGER NOT NULL DEFAULT 1000,
    ecf_emitidos_mes    INTEGER NOT NULL DEFAULT 0,
    cert_vencimiento    DATE,                                 -- vencimiento del .p12 del tenant
    cert_alerta_enviada BOOLEAN NOT NULL DEFAULT FALSE,
    cert_password       VARCHAR(255),                         -- password del .p12 (cifrado en app layer)
    cufe_secret         VARCHAR(128),                         -- clave secreta CUFE registrada ante DGII
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ                          -- soft delete
);

CREATE INDEX idx_tenants_rnc        ON public.tenants(rnc);
CREATE INDEX idx_tenants_api_key    ON public.tenants(api_key);
CREATE INDEX idx_tenants_estado     ON public.tenants(estado);
CREATE INDEX idx_tenants_cert_venc  ON public.tenants(cert_vencimiento) WHERE deleted_at IS NULL;

-- Certificados .p12 de los tenants (Cert Vault)
-- La columna cert_data almacena el .p12 cifrado con AES-256-GCM
-- La llave de cifrado vive en variable de entorno VAULT_MASTER_KEY
CREATE TABLE public.tenant_certs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    cert_data       BYTEA NOT NULL,              -- .p12 cifrado AES-256-GCM
    iv              BYTEA NOT NULL,              -- IV de 12 bytes para GCM
    tag             BYTEA NOT NULL,              -- tag de autenticación GCM
    cert_serial     VARCHAR(64),                 -- número de serie del certificado
    cert_subject    TEXT,                        -- CN del certificado
    valid_from      DATE NOT NULL,
    valid_to        DATE NOT NULL,
    activo          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_tenant_certs_tenant ON public.tenant_certs(tenant_id);
CREATE INDEX idx_tenant_certs_activo ON public.tenant_certs(tenant_id, activo);

-- Usuarios del portal de administración
CREATE TABLE public.portal_users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID REFERENCES public.tenants(id) ON DELETE CASCADE,  -- NULL = superadmin
    email           VARCHAR(255) NOT NULL UNIQUE,
    password_hash   VARCHAR(128) NOT NULL,                   -- bcrypt
    nombre          VARCHAR(255) NOT NULL,
    rol             VARCHAR(20) NOT NULL DEFAULT 'admin' CHECK (rol IN ('superadmin','admin','viewer')),
    activo          BOOLEAN NOT NULL DEFAULT TRUE,
    last_login      TIMESTAMPTZ,
    mfa_secret      VARCHAR(32),                             -- TOTP secret (opcional)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_portal_users_tenant ON public.portal_users(tenant_id);

-- Sesiones / tokens de acceso al portal
CREATE TABLE public.portal_sessions (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id     UUID NOT NULL REFERENCES public.portal_users(id) ON DELETE CASCADE,
    token_hash  VARCHAR(128) NOT NULL UNIQUE,
    ip_address  INET,
    user_agent  TEXT,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sessions_token   ON public.portal_sessions(token_hash);
CREATE INDEX idx_sessions_expires ON public.portal_sessions(expires_at);

-- Secuencias NCF por tenant y tipo de comprobante
-- Crítico: la DGII auditará que no haya saltos ni duplicados
CREATE TABLE public.ncf_sequences (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES public.tenants(id) ON DELETE CASCADE,
    tipo_ecf        SMALLINT NOT NULL,   -- 31,32,33,34,41,43,44,45,46,47
    prefijo         VARCHAR(3) NOT NULL, -- 'E31', 'E32', etc.
    secuencia_actual BIGINT NOT NULL DEFAULT 0,
    secuencia_max   BIGINT NOT NULL DEFAULT 9999999999,
    activo          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, tipo_ecf)
);

CREATE INDEX idx_ncf_sequences_tenant ON public.ncf_sequences(tenant_id);

-- Audit log del sistema (nivel plataforma)
CREATE TABLE public.system_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   UUID REFERENCES public.tenants(id),
    user_id     UUID REFERENCES public.portal_users(id),
    accion      VARCHAR(100) NOT NULL,
    entidad     VARCHAR(100),
    entidad_id  VARCHAR(100),
    detalle     JSONB,
    ip_address  INET,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_tenant    ON public.system_audit_log(tenant_id);
CREATE INDEX idx_audit_created   ON public.system_audit_log(created_at);
CREATE INDEX idx_audit_accion    ON public.system_audit_log(accion);

-- FUNCIÓN: crear schema y tablas por tenant
-- Se invoca al activar un tenant nuevo
CREATE OR REPLACE FUNCTION public.crear_schema_tenant(p_schema VARCHAR)
RETURNS VOID AS $$
BEGIN
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', p_schema);

    -- e-CF emitidos
    EXECUTE format('
    CREATE TABLE IF NOT EXISTS %I.ecf (
        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        ncf             VARCHAR(13) NOT NULL UNIQUE,          -- E310000000001
        tipo_ecf        SMALLINT NOT NULL,
        estado          VARCHAR(20) NOT NULL DEFAULT ''pendiente''
                        CHECK (estado IN (''pendiente'',''enviado'',''aprobado'',''rechazado'',''condicionado'',''anulacion_pendiente'',''anulado'',''anulacion_fallida'')),
        cufe            VARCHAR(128),                         -- hash SHA-384 DGII
        rnc_comprador   VARCHAR(11),
        nombre_comprador VARCHAR(255),
        fecha_emision   DATE NOT NULL,
        subtotal        NUMERIC(18,2) NOT NULL DEFAULT 0,
        itbis           NUMERIC(18,2) NOT NULL DEFAULT 0,
        total           NUMERIC(18,2) NOT NULL,
        moneda          CHAR(3) NOT NULL DEFAULT ''DOP'',
        tipo_cambio     NUMERIC(12,4) NOT NULL DEFAULT 1,
        xml_original    BYTEA,                               -- XML sin firmar
        xml_firmado     BYTEA,                               -- XML firmado (almacenado 10 años)
        respuesta_dgii  JSONB,                               -- respuesta completa DGII
        intentos_envio  SMALLINT NOT NULL DEFAULT 0,
        ultimo_error    TEXT,
        odoo_move_id    VARCHAR(64),                         -- id del account.move en Odoo
        odoo_move_name  VARCHAR(64),
        referencia_ncf  VARCHAR(13),                         -- para notas de crédito/débito
        fecha_ncf_referencia DATE,                           -- fecha del NCF de referencia
        codigo_modificacion VARCHAR(1) DEFAULT ''1'',        -- 1=Descuento 2=Devol 3=Anul 4=Otro
        tipo_pago       VARCHAR(1) DEFAULT ''1'',             -- 1=Contado 2=Crédito 3=Gratuito
        tipo_ingresos   VARCHAR(2) DEFAULT ''01'',            -- 01..05
        tipo_rnc_comprador VARCHAR(1) DEFAULT ''1'',          -- 1=RNC 2=Cédula 3=Pasaporte
        indicador_envio_diferido SMALLINT DEFAULT 0,         -- 0=Tiempo real 1=Diferido
        direccion_comprador VARCHAR(255),
        track_id        VARCHAR(128),                        -- trackId retornado por DGII
        security_code   VARCHAR(6),                          -- primeros 6 chars del hash firma
        qr_url          TEXT,                                -- URL para QR de representación impresa
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        sent_at         TIMESTAMPTZ,
        approved_at     TIMESTAMPTZ
    )', p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_ncf     ON %I.ecf(ncf)',           p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_estado  ON %I.ecf(estado)',         p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_fecha   ON %I.ecf(fecha_emision)',  p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_cufe    ON %I.ecf(cufe)',           p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_rnc_c   ON %I.ecf(rnc_comprador)', p_schema);

    -- Items del e-CF
    EXECUTE format('
    CREATE TABLE IF NOT EXISTS %I.ecf_items (
        id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        ecf_id      UUID NOT NULL REFERENCES %I.ecf(id) ON DELETE CASCADE,
        linea       SMALLINT NOT NULL,
        descripcion TEXT NOT NULL,
        cantidad    NUMERIC(12,4) NOT NULL,
        precio_unitario NUMERIC(18,4) NOT NULL,
        descuento   NUMERIC(18,2) NOT NULL DEFAULT 0,
        itbis_tasa  NUMERIC(5,2) NOT NULL DEFAULT 18,
        itbis_monto NUMERIC(18,2) NOT NULL DEFAULT 0,
        subtotal    NUMERIC(18,2) NOT NULL,
        unidad      VARCHAR(20),
        indicador_bien_servicio SMALLINT NOT NULL DEFAULT 2  -- 1=Bien 2=Servicio
    )', p_schema, p_schema);

    -- Historial de estados del e-CF
    EXECUTE format('
    CREATE TABLE IF NOT EXISTS %I.ecf_estado_log (
        id          BIGSERIAL PRIMARY KEY,
        ecf_id      UUID NOT NULL REFERENCES %I.ecf(id) ON DELETE CASCADE,
        estado_prev VARCHAR(20),
        estado_new  VARCHAR(20) NOT NULL
                    CHECK (estado_new IN (''pendiente'',''enviado'',''aprobado'',''rechazado'',''condicionado'',''anulacion_pendiente'',''anulado'',''anulacion_fallida'')),
        detalle     TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )', p_schema, p_schema);

    -- Compras (para 606)
    EXECUTE format('
    CREATE TABLE IF NOT EXISTS %I.compras (
        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        ncf             VARCHAR(13) NOT NULL,
        rnc_proveedor   VARCHAR(11) NOT NULL,
        nombre_proveedor VARCHAR(255),
        tipo_bienes     SMALLINT,
        fecha_comprobante DATE NOT NULL,
        fecha_pago      DATE,
        monto_servicios NUMERIC(18,2) NOT NULL DEFAULT 0,
        monto_bienes    NUMERIC(18,2) NOT NULL DEFAULT 0,
        total_monto     NUMERIC(18,2) NOT NULL,
        itbis_facturado NUMERIC(18,2) NOT NULL DEFAULT 0,
        itbis_retenido  NUMERIC(18,2) NOT NULL DEFAULT 0,
        isr_retencion   NUMERIC(18,2) NOT NULL DEFAULT 0,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )', p_schema);

    -- Retenciones ISR (para reportes IR-17 / uso auxiliar)
    EXECUTE format('
    CREATE TABLE IF NOT EXISTS %I.retenciones (
        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        ncf             VARCHAR(13) NOT NULL,
        rnc_retenido    VARCHAR(11),
        cedula_retenido VARCHAR(11),
        nombre_retenido VARCHAR(255) NOT NULL,
        fecha           DATE NOT NULL,
        monto_pagado    NUMERIC(18,2) NOT NULL,
        isr_retenido    NUMERIC(18,2) NOT NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )', p_schema);

    RAISE NOTICE 'Schema % creado correctamente', p_schema;
END;
$$ LANGUAGE plpgsql;

-- FUNCIÓN: próximo NCF (atómica — evita duplicados)
CREATE OR REPLACE FUNCTION public.next_ncf(
    p_tenant_id UUID,
    p_tipo_ecf  SMALLINT
) RETURNS VARCHAR AS $$
DECLARE
    v_prefijo   VARCHAR(3);
    v_seq       BIGINT;
    v_ncf       VARCHAR(13);
BEGIN
    UPDATE public.ncf_sequences
    SET    secuencia_actual = secuencia_actual + 1,
           updated_at = NOW()
    WHERE  tenant_id = p_tenant_id
      AND  tipo_ecf  = p_tipo_ecf
      AND  activo    = TRUE
      AND  secuencia_actual < secuencia_max
    RETURNING prefijo, secuencia_actual
    INTO v_prefijo, v_seq;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Secuencia NCF agotada o inactiva para tenant % tipo %',
                        p_tenant_id, p_tipo_ecf;
    END IF;

    -- Formato: E + tipo(2) + secuencia(10 dígitos con ceros)
    v_ncf := v_prefijo || LPAD(v_seq::TEXT, 10, '0');
    RETURN v_ncf;
END;
$$ LANGUAGE plpgsql;

-- TRIGGER: updated_at automático
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_tenants_updated_at
    BEFORE UPDATE ON public.tenants
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TRIGGER trg_ncf_sequences_updated_at
    BEFORE UPDATE ON public.ncf_sequences
    FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- VISTA: resumen de tenants para el superadmin
CREATE OR REPLACE VIEW public.v_tenants_resumen AS
SELECT
    t.id,
    t.rnc,
    t.razon_social,
    t.plan,
    t.estado,
    t.ambiente,
    t.ecf_emitidos_mes,
    t.max_ecf_mensual,
    t.cert_vencimiento,
    CASE WHEN t.cert_vencimiento <= CURRENT_DATE + 30 THEN TRUE ELSE FALSE END AS cert_por_vencer,
    t.created_at,
    COUNT(c.id) AS total_certs_activos
FROM public.tenants t
LEFT JOIN public.tenant_certs c ON c.tenant_id = t.id AND c.activo = TRUE
WHERE t.deleted_at IS NULL
GROUP BY t.id;
