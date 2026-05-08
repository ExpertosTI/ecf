-- Migration 010: Renombrar columna cufe → codigo_seguridad
-- Alinea la nomenclatura con la Norma DGII RD ("Código de Seguridad").
-- La columna security_code (6 chars) sigue existiendo como campo calculado.
-- Seguro para ejecutar múltiples veces (IF EXISTS protege contra errores).

DO $$
DECLARE
    v_schema TEXT;
BEGIN
    FOR v_schema IN
        SELECT schema_name FROM public.tenants WHERE deleted_at IS NULL
    LOOP
        -- Tabla ecf: cufe → codigo_seguridad
        BEGIN
            EXECUTE format(
                'ALTER TABLE %I.ecf RENAME COLUMN cufe TO codigo_seguridad',
                v_schema
            );
            RAISE NOTICE 'ecf.cufe renombrado en schema %', v_schema;
        EXCEPTION
            WHEN undefined_column THEN
                RAISE NOTICE 'ecf.cufe ya renombrado en schema %, saltando', v_schema;
            WHEN OTHERS THEN
                RAISE NOTICE 'Error en ecf para schema %: %', v_schema, SQLERRM;
        END;

        -- Índice en ecf
        BEGIN
            EXECUTE format(
                'ALTER INDEX IF EXISTS %I.idx_ecf_cufe RENAME TO idx_ecf_codigo_seguridad',
                v_schema
            );
        EXCEPTION WHEN OTHERS THEN NULL;
        END;

        -- Tabla compras: cufe → codigo_seguridad
        BEGIN
            EXECUTE format(
                'ALTER TABLE %I.compras RENAME COLUMN cufe TO codigo_seguridad',
                v_schema
            );
            RAISE NOTICE 'compras.cufe renombrado en schema %', v_schema;
        EXCEPTION
            WHEN undefined_column THEN
                RAISE NOTICE 'compras.cufe ya renombrado en schema %, saltando', v_schema;
            WHEN OTHERS THEN
                RAISE NOTICE 'Error en compras para schema %: %', v_schema, SQLERRM;
        END;
    END LOOP;
END;
$$;

-- Actualizar la función crear_schema_tenant para nuevos tenants
-- (refleja el estado final post-migración)
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

    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_compras_ncf         ON %I.compras(ncf)',               p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_compras_rnc_prov    ON %I.compras(rnc_proveedor)',     p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_compras_fecha       ON %I.compras(fecha_comprobante)', p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_compras_estado_odoo ON %I.compras(estado_odoo)',       p_schema);

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

    RAISE NOTICE 'Schema % creado correctamente (v2.6 — codigo_seguridad)', p_schema;
END;
$$ LANGUAGE plpgsql;
