-- PostgreSQL bootstrap for STICKS. Idempotent: safe to run more than once.
-- Enables pgvector, required by the AI memory tables.
-- The Alembic migration "criar_tabelas_agente_ia_e_habilitar_pgvector" runs the same statement,
-- so this script is only needed to prepare a database before migrations (for example, on a
-- managed service) and does not replace the migrations.
CREATE EXTENSION IF NOT EXISTS vector;