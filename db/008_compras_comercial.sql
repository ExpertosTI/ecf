-- Migration 008: Columnas estado_comercial y motivo_rechazo en tabla compras
-- Necesarias para el flujo de aprobación/rechazo de e-CF recibidas por el comprador.

DO $$
DECLARE
    v_schema TEXT;
BEGIN
    FOR v_schema IN
        SELECT schema_name FROM public.tenants WHERE deleted_at IS NULL
    LOOP
        EXECUTE format('
            ALTER TABLE %I.compras
                ADD COLUMN IF NOT EXISTS estado_comercial VARCHAR(20)
                    DEFAULT ''pendiente''
                    CHECK (estado_comercial IN (''pendiente'',''aprobado'',''rechazado'')),
                ADD COLUMN IF NOT EXISTS motivo_rechazo   TEXT
        ', v_schema);

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_compras_estado_comercial ON %I.compras(estado_comercial)',
            v_schema
        );

        RAISE NOTICE 'Schema % actualizado: estado_comercial + motivo_rechazo', v_schema;
    END LOOP;
END;
$$;
