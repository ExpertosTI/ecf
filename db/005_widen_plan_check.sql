-- Migration 005: Widen plan CHECK constraint
-- Accepts both legacy plan names (basico/profesional/enterprise)
-- and portal-facing names (pyme/standard/empresarial)

ALTER TABLE public.tenants
    DROP CONSTRAINT IF EXISTS tenants_plan_check;

ALTER TABLE public.tenants
    ADD CONSTRAINT tenants_plan_check
    CHECK (plan IN ('basico','profesional','enterprise','pyme','standard','empresarial'));
