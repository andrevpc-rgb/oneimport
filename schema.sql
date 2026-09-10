-- schema.sql — OneImport: schema Postgres multi-tenant para Supabase.
-- Rode este script uma vez no SQL Editor do seu projeto Supabase (ou via
-- `psql "$DATABASE_URL" -f schema.sql`). É seguro rodar mais de uma vez —
-- todos os comandos são idempotentes (IF NOT EXISTS / WHERE NOT EXISTS).

create extension if not exists "pgcrypto";

-- --------------------------------------------------------------------- --
-- usuarios — um por escritório contábil que acessa o OneImport. O próprio
-- `id` deste registro É o tenant_id usado em todas as outras tabelas — uma
-- conta aqui, um "espaço" isolado de empresas/regras/layouts. Autenticação
-- própria (e-mail + senha com hash PBKDF2 em senha_hash, ver database.py),
-- sem depender do SDK do Supabase Auth.
-- --------------------------------------------------------------------- --
create table if not exists usuarios (
    id              uuid primary key default gen_random_uuid(),
    email           text not null unique,
    senha_hash      text,
    nome_escritorio text not null,
    created_at      timestamptz not null default now()
);

-- migração segura para quem já criou a tabela usuarios antes do login existir
alter table usuarios add column if not exists senha_hash text;

-- --------------------------------------------------------------------- --
-- empresas — clientes de cada escritório (tenant). Sempre presas a um
-- tenant_id: nunca aparecem para outro escritório.
-- --------------------------------------------------------------------- --
create table if not exists empresas (
    id             bigserial primary key,
    tenant_id      uuid not null,
    cnpj           text not null,
    razao_social   text not null,
    codigo_sistema text default '',
    created_at     timestamptz not null default now(),
    unique (tenant_id, cnpj)
);

create index if not exists idx_empresas_tenant on empresas (tenant_id);

-- --------------------------------------------------------------------- --
-- regras_de_para — regras de classificação "De/Para" (Nível 1 da cascata
-- do motor de classificação). cnpj_empresa e codigo_banco em branco/nulos
-- valem como coringa (a regra vale para qualquer empresa/banco DESTE
-- tenant) — mas uma regra nunca atravessa para outro tenant.
-- --------------------------------------------------------------------- --
create table if not exists regras_de_para (
    id                 bigserial primary key,
    tenant_id          uuid not null,
    cnpj_empresa       text,
    codigo_banco       text,
    padrao_texto       text not null,
    tipo_padrao        text not null default 'contém',
    conta_debito       text default '',
    conta_credito      text default '',
    numero_historico   text default '',
    formato_variavel   text default '',
    created_at         timestamptz not null default now()
);

create index if not exists idx_regras_tenant on regras_de_para (tenant_id);
create index if not exists idx_regras_tenant_cnpj_banco on regras_de_para (tenant_id, cnpj_empresa, codigo_banco);

-- --------------------------------------------------------------------- --
-- layouts_exportacao — layouts do Motor de Layouts Dinâmicos (multi-ERP).
-- tenant_id NULL = layout global do sistema, visível para TODOS os
-- tenants (caso do "Calima ERP", semeado abaixo, protegido contra
-- exclusão na camada de aplicação); tenant_id preenchido = layout próprio
-- de um escritório, só visível para ele.
-- --------------------------------------------------------------------- --
create table if not exists layouts_exportacao (
    id                  bigserial primary key,
    tenant_id           uuid,
    nome_layout         text not null,
    formato_arquivo     text not null default 'xlsx',
    separador           text default ';',
    formato_data        text default '%d/%m/%Y',
    mapeamento_colunas  jsonb not null,
    is_padrao           boolean not null default false,
    created_at          timestamptz not null default now(),
    unique (tenant_id, nome_layout)
);

create index if not exists idx_layouts_tenant on layouts_exportacao (tenant_id);

-- --------------------------------------------------------------------- --
-- Seed: layout "Calima ERP" — padrão do sistema, disponível para todos os
-- tenants (tenant_id NULL). As 13 colunas do layout de importação do
-- Calima, na ordem oficial, mapeadas 1:1.
--
-- Usa WHERE NOT EXISTS em vez de ON CONFLICT porque o Postgres trata cada
-- NULL como distinto de outro NULL em constraints UNIQUE — um
-- "ON CONFLICT (tenant_id, nome_layout)" não pegaria duplicatas quando
-- tenant_id é NULL.
-- --------------------------------------------------------------------- --
insert into layouts_exportacao
    (tenant_id, nome_layout, formato_arquivo, separador, formato_data, mapeamento_colunas, is_padrao)
select
    null,
    'Calima ERP',
    'xlsx',
    ';',
    '%d/%m/%Y',
    '[
        {"coluna_saida": "Data", "coluna_sistema": "Data"},
        {"coluna_saida": "Valor", "coluna_sistema": "Valor"},
        {"coluna_saida": "Conta débito", "coluna_sistema": "Conta débito"},
        {"coluna_saida": "Conta crédito", "coluna_sistema": "Conta crédito"},
        {"coluna_saida": "Número histórico", "coluna_sistema": "Número histórico"},
        {"coluna_saida": "Variável", "coluna_sistema": "Variável"},
        {"coluna_saida": "Centro custo débito", "coluna_sistema": "Centro custo débito"},
        {"coluna_saida": "Centro custo crédito", "coluna_sistema": "Centro custo crédito"},
        {"coluna_saida": "Número lote", "coluna_sistema": "Número lote"},
        {"coluna_saida": "Código do imóvel", "coluna_sistema": "Código do imóvel"},
        {"coluna_saida": "Tipo de documento", "coluna_sistema": "Tipo de documento"},
        {"coluna_saida": "CPF/CNPJ", "coluna_sistema": "CPF/CNPJ"},
        {"coluna_saida": "Motivo de modificação do patrimônio líquido", "coluna_sistema": "Motivo de modificação do patrimônio líquido"}
    ]'::jsonb,
    true
where not exists (
    select 1 from layouts_exportacao where tenant_id is null and nome_layout = 'Calima ERP'
);
