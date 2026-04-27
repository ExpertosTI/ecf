-- Migration 006: Fix ambiente CHECK constraint
-- Ensures 'simulacion' is allowed in the tenants table

ALTER TABLE public.tenants
    DROP CONSTRAINT IF EXISTS tenants_ambiente_check;

ALTER TABLE public.tenants
    ADD CONSTRAINT tenants_ambiente_check
    CHECK (ambiente IN ('certificacion', 'produccion', 'simulacion'));
