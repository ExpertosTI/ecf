-- Migración 012: certificados PSFE de plataforma (mTLS DGII) cifrados en reposo
CREATE TABLE IF NOT EXISTS public.platform_psfe (
    id          SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    payload_enc BYTEA NOT NULL,
    iv          BYTEA NOT NULL,
    tag         BYTEA NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE public.platform_psfe IS
    'Singleton — certificado cliente PSFE + CA DGII (JSON cifrado AES-256-GCM)';
