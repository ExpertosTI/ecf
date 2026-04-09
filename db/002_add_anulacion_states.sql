-- Migration 002: Add anulacion_pendiente/anulacion_fallida states to existing schemas
-- Run AFTER deploying the new code.
-- Safe to re-run (idempotent).

DO $$
DECLARE
    r RECORD;
BEGIN
    -- 1. Update the CHECK constraint on ecf.estado for all tenant schemas
    FOR r IN
        SELECT schema_name FROM public.tenants WHERE deleted_at IS NULL
    LOOP
        -- Drop old constraint if it exists (name may vary)
        EXECUTE format(
            'ALTER TABLE IF EXISTS %I.ecf DROP CONSTRAINT IF EXISTS ecf_estado_check',
            r.schema_name
        );
        -- Add new constraint with anulacion_pendiente and anulacion_fallida
        EXECUTE format(
            'ALTER TABLE IF EXISTS %I.ecf ADD CONSTRAINT ecf_estado_check '
            'CHECK (estado IN (''pendiente'',''enviado'',''aprobado'',''rechazado'',''condicionado'',''anulacion_pendiente'',''anulado'',''anulacion_fallida''))',
            r.schema_name
        );
        RAISE NOTICE 'Updated CHECK constraint for schema %', r.schema_name;
    END LOOP;

    -- 2. Add CHECK constraint on ecf_estado_log.estado_new (optional but consistent)
    FOR r IN
        SELECT schema_name FROM public.tenants WHERE deleted_at IS NULL
    LOOP
        EXECUTE format(
            'ALTER TABLE IF EXISTS %I.ecf_estado_log DROP CONSTRAINT IF EXISTS ecf_estado_log_estado_new_check',
            r.schema_name
        );
        EXECUTE format(
            'ALTER TABLE IF EXISTS %I.ecf_estado_log ADD CONSTRAINT ecf_estado_log_estado_new_check '
            'CHECK (estado_new IN (''pendiente'',''enviado'',''aprobado'',''rechazado'',''condicionado'',''anulacion_pendiente'',''anulado'',''anulacion_fallida''))',
            r.schema_name
        );
    END LOOP;
END;
$$;
