-- SAAS ECF DGII — Migración 007
-- Soporte para Resumen de Facturación de Consumo Electrónico (RFCE - Tipo 31)

CREATE OR REPLACE FUNCTION public.apply_rfce_support(p_schema VARCHAR)
RETURNS VOID AS $$
BEGIN
    -- 1. Crear tabla de resúmenes en el schema del tenant
    EXECUTE format('
    CREATE TABLE IF NOT EXISTS %I.rfce (
        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        fecha_resumen   DATE NOT NULL,
        estado          VARCHAR(20) NOT NULL DEFAULT ''pendiente''
                        CHECK (estado IN (''pendiente'',''enviado'',''aprobado'',''rechazado'')),
        track_id        VARCHAR(128),
        secuencia_ncf   BIGINT,                               -- NCF del resumen (Tipo 31)
        cantidad_facturas INTEGER NOT NULL,
        monto_total     NUMERIC(18,2) NOT NULL,
        xml_firmado     BYTEA,
        respuesta_dgii  JSONB,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (fecha_resumen)
    )', p_schema);

    -- 2. Añadir columna de referencia en la tabla ecf
    EXECUTE format('
    ALTER TABLE %I.ecf 
    ADD COLUMN IF NOT EXISTS rfce_id UUID REFERENCES %I.rfce(id) ON DELETE SET NULL
    ', p_schema, p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS idx_ecf_rfce_id ON %I.ecf(rfce_id)', p_schema);

END;
$$ LANGUAGE plpgsql;

-- Aplicar a todos los tenants existentes
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN SELECT schema_name FROM public.tenants WHERE deleted_at IS NULL LOOP
        PERFORM public.apply_rfce_support(r.schema_name);
    END LOOP;
END $$;
