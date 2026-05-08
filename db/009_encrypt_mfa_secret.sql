-- Migración 009: cifrado de mfa_secret en portal_users
--
-- Añade la columna mfa_secret_enc (valor cifrado con AES-256-GCM vía VAULT_MASTER_KEY)
-- y depreca la columna mfa_secret en texto plano.
-- El borrado definitivo de mfa_secret se hace en 010_drop_mfa_plaintext.sql
-- una vez que todos los registros existentes hayan sido migrados por la aplicación.

ALTER TABLE public.portal_users
    ADD COLUMN IF NOT EXISTS mfa_secret_enc TEXT;  -- AES-256-GCM, base64url

COMMENT ON COLUMN public.portal_users.mfa_secret_enc
    IS 'TOTP secret cifrado con AES-256-GCM (VAULT_MASTER_KEY). Formato: base64url(nonce12||ct||tag16)';

COMMENT ON COLUMN public.portal_users.mfa_secret
    IS 'DEPRECATED — usar mfa_secret_enc. Se eliminará en migración 010.';
