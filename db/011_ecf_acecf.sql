-- Migration 011: Columnas estado_acecf y motivo_rechazo_acecf en tabla ecf
-- Registra la aprobación/rechazo comercial que los COMPRADORES envían
-- a través del endpoint /fe/aprobacioncomercial/api/ecf.
-- Estado 1 = aprobado, Estado 2 = rechazado (según ACECF.xsd DGII).

DO $$
DECLARE
    v_schema TEXT;
BEGIN
    FOR v_schema IN
        SELECT schema_name FROM public.tenants WHERE deleted_at IS NULL
    LOOP
        EXECUTE format(
            'ALTER TABLE %I.ecf
                ADD COLUMN IF NOT EXISTS estado_acecf        VARCHAR(20)
                    CHECK (estado_acecf IN (''aprobado'', ''rechazado'')),
                ADD COLUMN IF NOT EXISTS motivo_rechazo_acecf TEXT,
                ADD COLUMN IF NOT EXISTS acecf_recibido_at    TIMESTAMPTZ',
            v_schema
        );
        RAISE NOTICE 'Schema % actualizado: estado_acecf + motivo_rechazo_acecf + acecf_recibido_at', v_schema;
    END LOOP;
END$$;
