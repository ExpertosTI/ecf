-- Migración: Eliminar columna redundante api_key_hash de la tabla public.tenants
-- Aplicable a bases de datos existentes en transición a la v2.6.

ALTER TABLE public.tenants DROP COLUMN IF EXISTS api_key_hash;
