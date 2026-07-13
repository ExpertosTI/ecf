-- Migration 013: RFCE por factura (Manual Técnico DGII — consumo < RD$250,000)
-- El RFCE es un resumen POR FACTURA tipo 32, no un batch diario:
--   * rfce.ncf           → eNCF de la factura resumida (UNIQUE)
--   * rfce.fecha_resumen → deja de ser UNIQUE (varias facturas por día)
--   * ecf.rfce_id        → FK al resumen enviado a fc.dgii.gov.do
-- También restaura crear_schema_tenant() al estado final v2.7 (la migración
-- 010 la había sobrescrito sin las tablas RFCE).
-- Seguro para ejecutar múltiples veces.

DO $$
DECLARE
    v_schema TEXT;
BEGIN
    FOR v_schema IN
        SELECT schema_name FROM public.tenants WHERE deleted_at IS NULL
    LOOP
        -- 1. Crear tabla rfce si el schema fue creado con la función stale de 010
        BEGIN
            EXECUTE format('
                CREATE TABLE IF NOT EXISTS %I.rfce (
                    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    ncf             VARCHAR(13) UNIQUE,
                    fecha_resumen   DATE NOT NULL,
                    estado          VARCHAR(20) NOT NULL DEFAULT ''pendiente''
                                    CHECK (estado IN (''pendiente'',''enviado'',''aprobado'',''rechazado'')),
                    track_id        VARCHAR(128),
                    secuencia_ncf   BIGINT,
                    cantidad_facturas INTEGER NOT NULL DEFAULT 1,
                    monto_total     NUMERIC(18,2) NOT NULL,
                    xml_firmado     BYTEA,
                    respuesta_dgii  JSONB,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )', v_schema);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Error creando rfce en %: %', v_schema, SQLERRM;
        END;

        -- 2. Columna ncf (schemas con la tabla rfce vieja de 007)
        BEGIN
            EXECUTE format('ALTER TABLE %I.rfce ADD COLUMN IF NOT EXISTS ncf VARCHAR(13)', v_schema);
            EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS uq_rfce_ncf ON %I.rfce(ncf)', v_schema);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Error en rfce.ncf para %: %', v_schema, SQLERRM;
        END;

        -- 3. fecha_resumen deja de ser UNIQUE
        BEGIN
            EXECUTE format('ALTER TABLE %I.rfce DROP CONSTRAINT IF EXISTS rfce_fecha_resumen_key', v_schema);
            EXECUTE format('CREATE INDEX IF NOT EXISTS idx_rfce_fecha ON %I.rfce(fecha_resumen)', v_schema);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Error quitando UNIQUE fecha_resumen en %: %', v_schema, SQLERRM;
        END;

        -- 4. cantidad_facturas con default 1
        BEGIN
            EXECUTE format('ALTER TABLE %I.rfce ALTER COLUMN cantidad_facturas SET DEFAULT 1', v_schema);
        EXCEPTION WHEN OTHERS THEN NULL;
        END;

        -- 5. ecf.rfce_id + FK + índice
        BEGIN
            EXECUTE format('ALTER TABLE %I.ecf ADD COLUMN IF NOT EXISTS rfce_id UUID', v_schema);
            EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_rfce_id ON %I.ecf(rfce_id)', v_schema);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'Error en ecf.rfce_id para %: %', v_schema, SQLERRM;
        END;
        BEGIN
            EXECUTE format('
                ALTER TABLE %I.ecf
                ADD CONSTRAINT fk_ecf_rfce
                FOREIGN KEY (rfce_id) REFERENCES %I.rfce(id) ON DELETE SET NULL
            ', v_schema, v_schema);
        EXCEPTION
            WHEN duplicate_object THEN NULL;
            WHEN OTHERS THEN
                RAISE NOTICE 'Error FK fk_ecf_rfce en %: %', v_schema, SQLERRM;
        END;

        -- 6. motivo_rechazo alineado a VARCHAR(250) (migración 008 usaba TEXT)
        BEGIN
            EXECUTE format('ALTER TABLE %I.compras ALTER COLUMN motivo_rechazo TYPE VARCHAR(250)', v_schema);
        EXCEPTION WHEN OTHERS THEN NULL;
        END;

        RAISE NOTICE 'Schema % migrado a RFCE por factura (v2.7)', v_schema;
    END LOOP;
END;
$$;

-- ── Redefinición final de crear_schema_tenant (idéntica a db/001_schema.sql v2.7) ──

CREATE OR REPLACE FUNCTION public.crear_schema_tenant(p_schema VARCHAR)
RETURNS VOID AS $$
BEGIN
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', p_schema);

    -- ── e-CF emitidos ────────────────────────────────────────────────────────
    EXECUTE format('
    CREATE TABLE IF NOT EXISTS %I.ecf (
        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        ncf             VARCHAR(13) NOT NULL UNIQUE,
        tipo_ecf        SMALLINT NOT NULL,
        estado          VARCHAR(20) NOT NULL DEFAULT ''pendiente''
                        CHECK (estado IN (''pendiente'',''enviado'',''aprobado'',''rechazado'',
                                         ''condicionado'',''anulacion_pendiente'',''anulado'',''anulacion_fallida'')),
        codigo_seguridad VARCHAR(128),             -- Código de Seguridad DGII (hasta v2.5 llamado cufe)
        rnc_comprador    VARCHAR(11),
        nombre_comprador VARCHAR(255),
        fecha_emision    DATE NOT NULL,
        subtotal         NUMERIC(18,2) NOT NULL DEFAULT 0,
        itbis            NUMERIC(18,2) NOT NULL DEFAULT 0,
        total            NUMERIC(18,2) NOT NULL,
        moneda           CHAR(3) NOT NULL DEFAULT ''DOP'',
        tipo_cambio      NUMERIC(12,4) NOT NULL DEFAULT 1,
        xml_original     BYTEA,
        xml_firmado      BYTEA,
        respuesta_dgii   JSONB,
        intentos_envio   SMALLINT NOT NULL DEFAULT 0,
        ultimo_error     TEXT,
        odoo_move_id     VARCHAR(64),
        odoo_move_name   VARCHAR(64),
        referencia_ncf   VARCHAR(13),
        fecha_ncf_referencia DATE,
        codigo_modificacion VARCHAR(1) DEFAULT ''1'',
        tipo_pago        VARCHAR(1) DEFAULT ''1'',
        tipo_ingresos    VARCHAR(2) DEFAULT ''01'',
        tipo_rnc_comprador VARCHAR(1) DEFAULT ''1'',
        indicador_envio_diferido SMALLINT DEFAULT 0,
        direccion_comprador VARCHAR(255),
        track_id         VARCHAR(128),
        security_code    VARCHAR(6),              -- primeros 6 chars del hash de firma
        qr_url           TEXT,
        rfce_id          UUID,                    -- resumen RFCE (Tipo 31) si aplica
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        sent_at          TIMESTAMPTZ,
        approved_at      TIMESTAMPTZ
    )', p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_ncf              ON %I.ecf(ncf)',              p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_estado           ON %I.ecf(estado)',            p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_fecha            ON %I.ecf(fecha_emision)',     p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_codigo_seguridad ON %I.ecf(codigo_seguridad)', p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_rnc_c            ON %I.ecf(rnc_comprador)',    p_schema);

    -- ── Ítems del e-CF ───────────────────────────────────────────────────────
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
        indicador_bien_servicio SMALLINT NOT NULL DEFAULT 2
    )', p_schema, p_schema);

    -- ── Historial de estados ─────────────────────────────────────────────────
    EXECUTE format('
    CREATE TABLE IF NOT EXISTS %I.ecf_estado_log (
        id          BIGSERIAL PRIMARY KEY,
        ecf_id      UUID NOT NULL REFERENCES %I.ecf(id) ON DELETE CASCADE,
        estado_prev VARCHAR(20),
        estado_new  VARCHAR(20) NOT NULL
                    CHECK (estado_new IN (''pendiente'',''enviado'',''aprobado'',''rechazado'',
                                         ''condicionado'',''anulacion_pendiente'',''anulado'',''anulacion_fallida'')),
        detalle     TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )', p_schema, p_schema);

    -- ── RFCE — Resumen Factura Consumo < RD$250,000 (uno por factura Tipo 32)
    EXECUTE format('
    CREATE TABLE IF NOT EXISTS %I.rfce (
        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        ncf             VARCHAR(13) UNIQUE,       -- eNCF de la factura de consumo resumida
        fecha_resumen   DATE NOT NULL,
        estado          VARCHAR(20) NOT NULL DEFAULT ''pendiente''
                        CHECK (estado IN (''pendiente'',''enviado'',''aprobado'',''rechazado'')),
        track_id        VARCHAR(128),
        secuencia_ncf   BIGINT,
        cantidad_facturas INTEGER NOT NULL DEFAULT 1,
        monto_total     NUMERIC(18,2) NOT NULL,
        xml_firmado     BYTEA,
        respuesta_dgii  JSONB,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )', p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_rfce_fecha ON %I.rfce(fecha_resumen)', p_schema);

    -- FK rfce_id ahora que la tabla existe (idempotente)
    BEGIN
        EXECUTE format('
            ALTER TABLE %I.ecf
            ADD CONSTRAINT fk_ecf_rfce
            FOREIGN KEY (rfce_id) REFERENCES %I.rfce(id) ON DELETE SET NULL
        ', p_schema, p_schema);
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_rfce_id ON %I.ecf(rfce_id)', p_schema);

    -- ── Compras (e-CF Recibidas) ─────────────────────────────────────────────
    EXECUTE format('
    CREATE TABLE IF NOT EXISTS %I.compras (
        id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        ncf              VARCHAR(13) NOT NULL UNIQUE,
        rnc_proveedor    VARCHAR(11) NOT NULL,
        nombre_proveedor VARCHAR(255),
        tipo_bienes      SMALLINT,
        tipo_ecf         SMALLINT,
        codigo_seguridad VARCHAR(128),             -- Código de Seguridad del e-CF recibido
        xml_original     BYTEA,
        fecha_comprobante DATE NOT NULL,
        fecha_pago       DATE,
        monto_servicios  NUMERIC(18,2) NOT NULL DEFAULT 0,
        monto_bienes     NUMERIC(18,2) NOT NULL DEFAULT 0,
        total_monto      NUMERIC(18,2) NOT NULL,
        itbis_facturado  NUMERIC(18,2) NOT NULL DEFAULT 0,
        itbis_retenido   NUMERIC(18,2) NOT NULL DEFAULT 0,
        isr_retencion    NUMERIC(18,2) NOT NULL DEFAULT 0,
        ambiente         VARCHAR(20) DEFAULT ''produccion'',
        estado_odoo      VARCHAR(20) NOT NULL DEFAULT ''nueva''
                         CHECK (estado_odoo IN (''nueva'',''enviada'',''procesada'',''error'')),
        estado_comercial VARCHAR(20) NOT NULL DEFAULT ''pendiente''
                         CHECK (estado_comercial IN (''pendiente'',''aprobado'',''rechazado'')),
        motivo_rechazo   VARCHAR(250),
        odoo_bill_id     VARCHAR(64),
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )', p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_compras_ncf              ON %I.compras(ncf)',               p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_compras_rnc_prov         ON %I.compras(rnc_proveedor)',     p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_compras_fecha            ON %I.compras(fecha_comprobante)', p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_compras_estado_odoo      ON %I.compras(estado_odoo)',       p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_compras_estado_comercial ON %I.compras(estado_comercial)',  p_schema);

    -- ── Retenciones ISR ──────────────────────────────────────────────────────
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

    -- ── Tracking sincronización ──────────────────────────────────────────────
    EXECUTE format('
    CREATE TABLE IF NOT EXISTS %I.ecf_recibidas_sync (
        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        ultima_sync     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        ultima_fecha_consultada DATE NOT NULL DEFAULT CURRENT_DATE - 1,
        total_nuevos    INTEGER NOT NULL DEFAULT 0,
        total_errores   INTEGER NOT NULL DEFAULT 0,
        error_mensaje   TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )', p_schema);

    RAISE NOTICE 'Schema % creado correctamente (v2.7 — RFCE por factura)', p_schema;
END;
$$ LANGUAGE plpgsql;
