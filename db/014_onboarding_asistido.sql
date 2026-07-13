-- Renace e-CF — Onboarding asistido multi-tenant
-- Versión: 014
-- Añade operador de plataforma + marcas de progreso DGII por empresa.
-- Idempotente: seguro re-ejecutar.

ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS is_platform_operator BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS dgii_test_ok_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS postulacion_firmada_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS onboarding_started_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

COMMENT ON COLUMN public.tenants.is_platform_operator IS
    'True solo para la empresa operadora (Renace PSFE). Un único tenant activo.';
COMMENT ON COLUMN public.tenants.dgii_test_ok_at IS
    'Última autenticación CerteCF exitosa (semilla + .p12 → token).';
COMMENT ON COLUMN public.tenants.postulacion_firmada_at IS
    'Última firma exitosa del XML de postulación DGII desde el panel.';

-- Solo un operador activo a la vez
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenants_one_platform_operator
    ON public.tenants (is_platform_operator)
    WHERE is_platform_operator = TRUE AND deleted_at IS NULL;

-- Marcar Renace (RNC por defecto) si ya existe y aún no hay operador
UPDATE public.tenants
SET is_platform_operator = TRUE,
    updated_at = NOW()
WHERE deleted_at IS NULL
  AND rnc = COALESCE(NULLIF(current_setting('app.platform_operator_rnc', true), ''), '132842316')
  AND NOT EXISTS (
      SELECT 1 FROM public.tenants t2
      WHERE t2.is_platform_operator = TRUE AND t2.deleted_at IS NULL
  );
