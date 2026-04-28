-- Migration 004: DGII RNC Lookup Table
-- Stores the taxpayer registry for easy company creation

CREATE TABLE IF NOT EXISTS public.dgii_rnc (
    rnc                 VARCHAR(11) PRIMARY KEY,
    razon_social        VARCHAR(255) NOT NULL,
    nombre_comercial    VARCHAR(255),
    actividad_economica TEXT,
    fecha_inicio        DATE,
    estado              VARCHAR(50),
    regimen_pago        VARCHAR(50),
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dgii_rnc_clean ON public.dgii_rnc (regexp_replace(rnc, '[^0-9]', '', 'g'));
CREATE INDEX IF NOT EXISTS idx_dgii_rnc_razon ON public.dgii_rnc USING gin (to_tsvector('spanish', razon_social));
