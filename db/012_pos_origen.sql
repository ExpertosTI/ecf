-- Origen del e-CF: facturación de contabilidad o un punto de venta concreto.
-- Permite ver qué caja emitió cada comprobante.

DO $$
DECLARE
    v_schema TEXT;
BEGIN
    FOR v_schema IN
        SELECT schema_name FROM public.tenants WHERE deleted_at IS NULL
    LOOP
        EXECUTE format(
            'ALTER TABLE %I.ecf
                ADD COLUMN IF NOT EXISTS origen VARCHAR(20) NOT NULL DEFAULT ''facturacion'',
                ADD COLUMN IF NOT EXISTS pos_config_id VARCHAR(64),
                ADD COLUMN IF NOT EXISTS pos_config_nombre VARCHAR(128),
                ADD COLUMN IF NOT EXISTS pos_session_id VARCHAR(64)',
            v_schema
        );
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS idx_ecf_pos ON %I.ecf(pos_config_id)',
            v_schema
        );
        RAISE NOTICE 'Schema % actualizado: origen del punto de venta', v_schema;
    END LOOP;
END$$;
