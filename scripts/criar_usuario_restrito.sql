-- =============================================================================
-- Cria o papel de aplicação `contabil_app`, para tirar a API de cima do
-- superusuário `postgres`.
--
-- POR QUE
-- A aplicação conecta hoje como `postgres`, superusuário do cluster, e a porta
-- 3308 é alcançável da internet. A senha foi rotacionada em 31/07, mas o desenho
-- continua o mesmo: um vazamento futuro do `DATABASE_URL` dá acesso total. Em
-- particular, o mesmo papel alcança o banco `fiscargo`, do Frota-Link, que vive
-- no mesmo cluster — comprometer o contabil-core hoje é comprometer os dois.
--
-- O QUE ISTO RESOLVE (verificado num banco descartável, PostgreSQL 16)
--   ✅ Perde CREATE ROLE / ALTER ROLE e a leitura de `pg_shadow`
--   ✅ Perde `COPY ... FROM PROGRAM`, que é execução de comando no host
--   ✅ Perde CREATE DATABASE e `DROP DATABASE`
--   ✅ Perde o bypass automático de permissão que superusuário tem
--
-- O QUE ISTO **NÃO** RESOLVE
--   ❌ Sozinho, NÃO isola o `fiscargo`. Ver a PARTE 2 no fim do arquivo — é um
--      passo separado, que mexe no banco do Frota-Link e por isso não roda aqui.
--   ❌ O papel continua dono das tabelas do `contabil_db` e pode alterá-las ou
--      apagá-las. Isso é consequência de `entrypoint.sh` rodar
--      `alembic upgrade head` no startup com a MESMA credencial da API.
--      Ver "Variante mais restritiva" no fim do arquivo.
--   ❌ A porta 3308 continua aberta. Restringir por firewall é passo separado
--      e continua no backlog.
--
-- COMO RODAR
--   1. Escolha uma senha forte e substitua TROQUE_ESTA_SENHA abaixo.
--      Não commite o arquivo com a senha preenchida.
--   2. psql "postgres://postgres:SENHA@vps.41tech.cloud:3308/contabil_db" \
--        -v ON_ERROR_STOP=1 -f scripts/criar_usuario_restrito.sql
--   3. Rode o bloco de VERIFICAÇÃO no fim e confira as saídas.
--   4. Troque o DATABASE_URL do serviço contabil-core no EasyPanel para o novo
--      usuário e reinicie. Acompanhe os logs: o `alembic upgrade head` do
--      startup é o primeiro teste real de que as permissões bastam.
--   5. Só depois de confirmar que subiu, considere reduzir os poderes do
--        `postgres` ou trocar a senha dele de novo.
--
-- ROLLBACK
--   Basta voltar o DATABASE_URL para o usuário anterior e reiniciar o serviço.
--   O papel novo pode ficar criado sem efeito nenhum.
-- =============================================================================

-- Nome do banco. Sobrescreva com -v dbname=outro para testar num banco
-- descartável antes de aplicar em produção.
\if :{?dbname}
\else
  \set dbname contabil_db
\endif

\echo '=== Criando papel contabil_app ==='

-- Idempotente: não falha se já existir.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contabil_app') THEN
        CREATE ROLE contabil_app LOGIN PASSWORD 'TROQUE_ESTA_SENHA';
        RAISE NOTICE 'Papel contabil_app criado.';
    ELSE
        RAISE NOTICE 'Papel contabil_app já existe — apenas ajustando permissões.';
    END IF;
END
$$;

-- Explícito por clareza: nada de superusuário, criar bancos ou criar papéis.
ALTER ROLE contabil_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

\echo '=== Permissões no banco e no schema ==='

GRANT CONNECT ON DATABASE :"dbname" TO contabil_app;
GRANT USAGE  ON SCHEMA public TO contabil_app;

-- CREATE no schema é necessário porque o entrypoint roda as migrations do
-- Alembic com esta mesma credencial (CREATE TABLE / CREATE INDEX / CREATE TYPE).
GRANT CREATE ON SCHEMA public TO contabil_app;

\echo '=== Dados: leitura e escrita nas tabelas existentes ==='

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO contabil_app;

-- Sequences: as tabelas do ConcilPro (cp_arquivo, cp_fornecedor, cp_lancamento…)
-- usam PK inteira serial. Sem USAGE na sequence, todo INSERT nelas falha.
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO contabil_app;

\echo '=== Objetos futuros criados pelo postgres ==='

-- Se uma migration futura for aplicada como `postgres`, o objeto nasceria sem
-- permissão para o app. Isto cobre esse caso.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO contabil_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO contabil_app;

\echo '=== Transferindo a posse das tabelas existentes ==='

-- ALTER TABLE / DROP INDEX exigem ser dono do objeto, não bastam GRANTs.
-- Sem este bloco, a primeira migration que altere uma tabela existente falha
-- com "must be owner of table".
DO $$
DECLARE
    obj record;
BEGIN
    FOR obj IN
        SELECT tablename AS nome FROM pg_tables WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER TABLE public.%I OWNER TO contabil_app', obj.nome);
    END LOOP;

    FOR obj IN
        SELECT sequencename AS nome FROM pg_sequences WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER SEQUENCE public.%I OWNER TO contabil_app', obj.nome);
    END LOOP;

    FOR obj IN
        SELECT viewname AS nome FROM pg_views WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER VIEW public.%I OWNER TO contabil_app', obj.nome);
    END LOOP;

    -- Os ENUMs do schema (dc_enum, status_transacao_enum, resultado_neo_enum…)
    -- precisam trocar de dono junto: uma migration que adicione valor a um enum
    -- falha se o papel não for o dono do tipo.
    FOR obj IN
        SELECT t.typname AS nome
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public' AND t.typtype = 'e'
    LOOP
        EXECUTE format('ALTER TYPE public.%I OWNER TO contabil_app', obj.nome);
    END LOOP;
END
$$;

\echo '=== Fechando o schema public para os demais ==='

-- Por padrão o Postgres deixa qualquer papel autenticado criar objetos no
-- schema public. Em PG 15+ isso já vem revogado; em versões anteriores, não.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Impede que um papel qualquer do cluster se conecte ao contabil_db.
REVOKE CONNECT ON DATABASE :"dbname" FROM PUBLIC;
GRANT  CONNECT ON DATABASE :"dbname" TO contabil_app;

\echo ''
\echo '=== VERIFICAÇÃO ==='

\echo '--- 1. Atributos do papel (todos os booleanos devem ser false) ---'
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin
FROM pg_roles
WHERE rolname = 'contabil_app';

\echo '--- 2. Tabelas que NÃO ficaram com o dono certo (esperado: zero linhas) ---'
SELECT tablename, tableowner
FROM pg_tables
WHERE schemaname = 'public' AND tableowner <> 'contabil_app';

\echo '--- 3. Contagem de objetos sob o novo dono ---'
SELECT
    (SELECT count(*) FROM pg_tables    WHERE schemaname = 'public' AND tableowner = 'contabil_app')    AS tabelas,
    (SELECT count(*) FROM pg_sequences WHERE schemaname = 'public' AND sequenceowner = 'contabil_app') AS sequences;

\echo '--- 4. Outros bancos do cluster ainda abertos a qualquer papel ---'
\echo '    (datacl vazio = PUBLIC pode conectar; ver PARTE 2 no fim do arquivo)'
SELECT datname, coalesce(datacl::text, '(aberto a PUBLIC)') AS acl
FROM pg_database
WHERE datistemplate = false AND datname <> :'dbname'
ORDER BY datname;

\echo ''
\echo 'Feito. Próximo passo: trocar o DATABASE_URL no EasyPanel para'
\echo '  postgres://contabil_app:SENHA@vps.41tech.cloud:3308/contabil_db'
\echo 'e acompanhar o "Executando migrations..." nos logs do container.'

-- =============================================================================
-- PARTE 2 — ISOLAR O `fiscargo` (NÃO roda automaticamente: leia antes)
--
-- Criar o papel restrito, sozinho, NÃO impede que ele se conecte aos outros
-- bancos do cluster. Verificado na prática: `contabil_app` recém-criado conectou
-- num segundo banco sem nenhum privilégio explícito.
--
-- A razão é que o PostgreSQL concede `CONNECT` a `PUBLIC` em todo banco novo, e
-- `PUBLIC` inclui qualquer papel com login. O `REVOKE ... FROM PUBLIC` da PARTE 1
-- vale só para o `contabil_db` — o `fiscargo` continua aberto até alguém revogar
-- lá também.
--
-- Confira o estado atual (coluna vazia = aberto para todo mundo):
--
--   SELECT datname, datacl FROM pg_database WHERE datistemplate = false;
--
-- ⚠️ Os comandos abaixo mexem no banco do Frota-Link. Rodar o REVOKE sem o GRANT
-- correspondente TIRA O FROTA-LINK DO AR. Faça na ordem, e confirme antes qual
-- papel o serviço dele usa de fato:
--
--   SELECT DISTINCT usename FROM pg_stat_activity WHERE datname = 'fiscargo';
--
-- Depois, conectado ao banco `postgres` como superusuário:
--
--   -- 1º garante quem PRECISA entrar
--   GRANT CONNECT ON DATABASE fiscargo TO <papel_do_frota_link>;
--   -- 2º só então fecha para o resto
--   REVOKE CONNECT ON DATABASE fiscargo FROM PUBLIC;
--
-- TESTE DE ISOLAMENTO (depois de aplicar, como contabil_app):
--
--   psql "postgres://contabil_app:SENHA@vps.41tech.cloud:3308/fiscargo"
--
-- Esperado: FATAL: permission denied for database "fiscargo".
-- Enquanto conectar, comprometer o contabil-core é comprometer o Frota-Link.
-- =============================================================================

-- =============================================================================
-- VARIANTE MAIS RESTRITIVA (não aplicada aqui)
--
-- O papel acima ainda é dono das tabelas, porque o mesmo container roda as
-- migrations e serve a API. Para separar de verdade seriam necessários dois
-- papéis e duas URLs:
--
--   contabil_migrator  → dono dos objetos, usado só pelo `alembic upgrade head`
--   contabil_runtime   → apenas SELECT/INSERT/UPDATE/DELETE, usado pelo uvicorn
--
-- Custo: `entrypoint.sh` passa a precisar de uma segunda variável
-- (`MIGRATION_DATABASE_URL`) e o EasyPanel de mais um segredo. O ganho é que um
-- vazamento da credencial de runtime não permite mais DROP TABLE.
--
-- Vale a pena se e quando a porta 3308 continuar exposta. Se o firewall entrar
-- primeiro, o ganho marginal é bem menor.
-- =============================================================================
