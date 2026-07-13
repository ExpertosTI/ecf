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

-- NOTA: la redefinición de crear_schema_tenant() se movió a
-- db/013_rfce_por_factura.sql (estado final v2.7 con RFCE por factura).
-- Mantener una sola fuente de verdad evita que migraciones intermedias
-- sobrescriban la función con estados stale.
