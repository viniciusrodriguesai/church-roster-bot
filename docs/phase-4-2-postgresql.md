# Fase 4.2 — PostgreSQL e migração verificável

Implementação na `feature/phase-4-production`, a partir da base `5bfd27a`.
A aprovação de cada execução é registrada por SHA no relatório externo da validação.
Nenhum servidor externo ou banco original é alvo dos testes.

## Auditoria e arquitetura

Os modelos atuais abrangem identidade/igreja, ministérios, funções e conflitos,
permissões, cultos/vagas, disponibilidade, participações, pedidos excepcionais,
propostas, convites normais, fila, trocas, políticas de automação, lembretes,
emergências, conversas, inbox/outbox, auditoria e versões publicadas. A transferência
deve descobrir todas as tabelas pelo metadata, não uma lista parcial de cadastros.
O head da Fase 4.1 é 431d9e6a8102; a Fase 3 terminava em 231e4c9a7308. A Fase 2
tem esquemas em 88ca949752d6 e a27b6c1d4e90 (outbox dos convites). Não há
necessidade demonstrada de novo esquema nesta fase.

| Módulo | Responsabilidade |
| --- | --- |
| `core/database.py` | Política de conexão compartilhada e limites PostgreSQL |
| `database/schema.py` | Revisões conhecidas e verificação física do esquema |
| `database/source.py` | Snapshot SQLite somente leitura e atualização de cópia |
| `database/canonical.py` | Tipos sem coerções silenciosas e fingerprints por tabela |
| `database/transfer.py` | Plano autorizado, transação, locks e reconciliação |
| `scripts/migrate_sqlite.py` | Interface do operador e relatório sem dados sensíveis |

SQLite usa tipagem flexível, timestamps UTC sem offset na leitura e serialização
de transações locais; não implementa locks de linha/FOR UPDATE. O PostgreSQL
impõe tipos, tamanhos, FKs, índices parciais e locks reais. Contagem igual não
comprova transferência: são necessários fingerprints canônicos e verificação
de todas as relações e estados, inclusive históricos e mensagens incertas.

## Conexões

database_engine centraliza pool e timeouts do aplicativo. PostgreSQL usa
READ COMMITTED, UTC, pre_ping, reciclagem e pool limitado. Conexões Alembic usam
NullPool e preservam os limites nativos. Options de DSNs, como search_path,
são mantidas. Variáveis DB_* estão listadas em .env.example.

Pre_ping recupera conexões inválidas no checkout; não recupera uma transação
interrompida. Sessões sempre encerram por context manager. Deadlocks, timeout
e falhas de conexão revertem a transação; o worker conserva eventos e retenta
o ciclo, sem repetir transporte de resultado incerto. Não retentar apenas a
última instrução SQL nem uma operação externa já iniciada.

APP_ENV=production exige PostgreSQL/psycopg, credenciais próprias e sslmode=verify-full.
Preparar a CA confiável em sslrootcert quando necessária. A política é validada
sem conectar servidores externos. Separar papel de runtime com DML/USAGE de
papel de migração proprietário do esquema. O administrador de instalação local
do Compose não deve ser confundido com o papel mínimo de runtime em produção.

## Docker local e preservação

O Compose usa PostgreSQL 17, volume nomeado e bind em 127.0.0.1. A senha é
obrigatória, sem fallback change-me; .env não é sobrescrito. Gerar um segredo
aleatório e armazená-lo em arquivo .env ignorado, fora de exemplos públicos.
Para validação usar projeto Compose novo, arquivo de ambiente próprio e
POSTGRES_PORT=0 (porta dinâmica); registrar a porta com docker compose port db 5432.
Não executar down -v, seeds ou migrações sobre volumes preexistentes.

## Reconciliação canônica

database.canonical verifica tipos reais, limites INTEGER/VARCHAR, booleanos
estritos e precisão JSON com Decimal. Não transforma números em strings nem
silencia chaves JSON duplicadas, NaN, NUL ou Unicode inválido. Timestamp SQLite
sem offset segue o contrato UTC do projeto; datas e horários de cultos continuam
civis. SQL NULL e JSON null têm fingerprints distintos. A prova é SHA256 por
tabela, por linha em ordem de PK, com nomes de colunas explícitos. A mesma
contagem com conteúdo diferente é uma divergência, não uma migração aprovada.

## Preparação da origem e comparação dos esquemas

A origem é aberta com `mode=ro`, `query_only` e uma transação de leitura.
SHA256, identidade do arquivo, tamanho e data de modificação são comparados
antes e depois. Arquivo ausente não é criado. WAL, SHM, journal ou modo WAL
exigem um snapshot offline consistente produzido pelo operador; a ferramenta
recusa esses arquivos em vez de ignorar alterações ainda não incorporadas.
Não use `immutable=1` para encobrir um WAL existente.

O esquema físico precisa corresponder à revisão Alembic declarada. São
verificados tabelas, tipos, nulabilidade, PKs, FKs, unicidade, checks e índices
parciais. Tabelas adicionais, views e triggers não previstos são recusados.
No PostgreSQL, uma versão Alembic correta não basta: constraints precisam
estar validadas, triggers de integridade ativos e o esquema real precisa
corresponder aos modelos atuais. Uma constraint enfraquecida é divergência.

Versões conhecidas desde o final da Fase 2 podem ser preparadas somente com
autorização de atualização de cópia. O backup SQLite é feito em diretório
temporário privado; Alembic atualiza essa cópia, mantendo a origem somente
leitura. Fingerprints de todas as colunas antigas são comparados antes e
depois. Esquema sem versão só é aceito quando corresponde integralmente ao
head atual. Não são inventadas permissões, consentimentos ou outros dados
ausentes no histórico. O contrato de conexão fornecida ao Alembic mantém
essa conexão utilizável e não a encerra implicitamente.

Para executar os testes de preparação, configure `TEST_DATABASE_URL` para
um PostgreSQL descartável e execute:

```sh
pytest -q tests/test_transfer_canonical.py tests/test_transfer_schema.py \
  tests/test_transfer_source.py tests/test_migration_logging.py
```

A preparação da origem é validada separadamente das transações reais, rollback,
confirmação do destino e reconciliação pós-cópia descritos a seguir.

## Transferência controlada

Execute a ferramenta a partir de um checkout com os arquivos Alembic disponíveis.
Prepare um PostgreSQL novo e isolado com `alembic upgrade head`. Todas as 28
tabelas de negócio do esquema de destino precisam estar vazias; a versão Alembic
deve ser o head atual. Nenhuma tabela é apagada ou recriada pelo importador.
Use papel de migração proprietário das tabelas e sequences, separado do runtime.

O operador deve interromper API, workers e simuladores tanto da origem quanto
do destino durante a operação. `--offline-confirmed` registra essa declaração;
o programa não consegue garantir que outro processo externo permaneça parado.
Faça backup consistente antes do corte. Não migre dados reais sem autorização.
Armazene a URL somente em variável de ambiente ou gerenciador de segredos,
sem incluí-la em argumentos, logs ou histórico do shell.

Com `ROSTER_IMPORT_TARGET` já configurada para o destino autorizado:

```sh
python -m church_roster_bot.scripts.migrate_sqlite \
  --source /caminho/snapshot-offline.sqlite3 \
  --target-env ROSTER_IMPORT_TARGET --schema public \
  --report /caminho/novo-plano.json
```

O padrão é dry-run: transação PostgreSQL `REPEATABLE READ READ ONLY`, sem INSERT,
DDL, nextval ou setval. O relatório inclui SHA256 da origem, revisão, contagens
e fingerprints por tabela, identidade do destino e plano de sequences. Para
esquemas antigos conhecidos, acrescente `--upgrade-copy` em todas as etapas;
somente uma cópia temporária será atualizada.

Confira o relatório e copie seu `plan_token` para a confirmação:

```sh
python -m church_roster_bot.scripts.migrate_sqlite \
  --source /caminho/snapshot-offline.sqlite3 \
  --target-env ROSTER_IMPORT_TARGET --schema public \
  --report /caminho/novo-resultado.json \
  --apply --offline-confirmed --confirm TOKEN_DO_PLANO
```

O token vincula conteúdo e identidade da origem, esquema, banco, usuário,
instância PostgreSQL e estado do destino. Alterações nesses elementos exigem
novo plano. O executor adquire advisory lock e locks de tabelas, verifica
novamente o destino vazio e importa em uma transação. IDs, relações, datas,
consentimentos, auditoria e estados de mensagens são preservados. Divergência
de contagens ou de qualquer fingerprint reverte a operação.

Sequences são reiniciadas com `ALTER SEQUENCE RESTART`, incluindo a conferência
do próximo valor, na mesma transação das linhas. Evita-se `setval`, cuja alteração
não é revertida como uma gravação comum. A implementação atual recusa sequences
com incremento, cache ou ciclo não previstos, constraints diferidas, FKs com
ações distintas dos modelos e índices inválidos. Consulte a documentação de
[ALTER SEQUENCE](https://www.postgresql.org/docs/17/sql-altersequence.html) e de
[funções de sequences](https://www.postgresql.org/docs/17/functions-sequence.html).

Antes de religar a aplicação, confira o destino independentemente:

```sh
python -m church_roster_bot.scripts.migrate_sqlite \
  --source /caminho/snapshot-offline.sqlite3 \
  --target-env ROSTER_IMPORT_TARGET --schema public \
  --report /caminho/nova-conferencia.json --verify
```

O modo verify é somente leitura: compara dados e verifica que o próximo ID não
colide com os importados. Relatórios precisam ser arquivos novos e não são
sobrescritos. Erros não incluem valores de registros, SQL ou credenciais.

Se a conexão cair durante commit, o resultado é declarado incerto e não há
retry automático. Faça verify antes de qualquer nova tentativa. Destino
integralmente reconciliado indica importação concluída; destino vazio exige
novo dry-run; conteúdo parcial ou diferente exige investigação do operador.
A mesma cautela vale se houver falha ao escrever o relatório depois do commit.
Não apague registros para forçar uma segunda execução. Estados de entrega
incertos e eventos pendentes continuam preservados; não os marque como enviados
ou retente transporte sem reconciliação com o provedor na etapa operacional.

Os testes usam somente dados fictícios. Execute `pytest -q tests/test_transfer.py`
com `TEST_DATABASE_URL` descartável para testar confirmação, CLI, precisão,
versões antigas, importadores concorrentes, rollback de linhas e sequences e
perda real de conexão durante transação e commit.

### Desvios de visibilidade e geração de valores

Destinos com RLS ou FORCE RLS são recusados: políticas de linha podem ocultar
registros da comparação e tornar uma conferência de destino vazio incompleta.
O modelo atual não utiliza essas políticas; a transferência não tenta removê-las
ou contorná-las. A recusa vale inclusive quando o papel atual poderia ignorar RLS.

Além da propriedade da sequence, a validação confere se o default da PK realmente
executa `nextval` da sequence associada. Defaults ausentes, constantes ou ligados
a outra sequence, identity, colunas geradas e defaults não previstos são desvios
do contrato atual. A checagem lê os catálogos e compara expressões; não consome
IDs. Isso evita aprovar a cópia com IDs explícitos e deixar novas inserções do
aplicativo sem um gerador válido.

As regressões PostgreSQL reproduziram oito desvios antes da correção e depois
passaram junto com a aceitação do head normal. Veja as referências sobre
[RLS](https://www.postgresql.org/docs/17/ddl-rowsecurity.html) e
[informações de sequences](https://www.postgresql.org/docs/17/functions-info.html).

A comparação também mantém as fronteiras de chamadas funcionais, sem confundir
uma coluna com uma função ou funções aninhadas com um nome concatenado. Nomes
entre aspas fora do contrato suportado são recusados. CHECKs e predicados de
índices que dependam de funções ou operadores externos a `pg_catalog` também
são recusados: o mesmo texto pode resolver para uma rotina sobrecarregada de
outro esquema. A validação lê dependências do catálogo, sem executar essas
rotinas. Essas garantias cobrem os modelos e revisões suportados; não substituem
a revisão de configurações administrativas do servidor antes de um corte real.

## Backup e restauração em ambiente isolado

Use clientes oficiais da mesma versão principal do servidor. O ensaio utiliza
PostgreSQL 17, `pg_dump` custom e `pg_restore`; compare o resultado da restauração,
não apenas a existência do arquivo. Configure previamente serviços libpq distintos
para origem e destino, com credenciais fora do Git e do histórico do shell.
O destino precisa ser um banco novo e autorizado. Os nomes abaixo são aliases
de configuração, não servidores provisionados nesta fase:

```sh
PGSERVICE=roster_backup_source pg_dump --format=custom \
  --no-owner --no-privileges --file=/caminho/novo-backup.dump
pg_restore --list /caminho/novo-backup.dump
pg_restore --dbname=service=roster_validation_restore \
  --single-transaction --exit-on-error --no-owner --no-privileges \
  /caminho/novo-backup.dump
```

Não use `--clean` em bancos existentes nem sobrescreva backups anteriores.
Proteja o arquivo como dados pessoais quando contiver dados reais. `pg_dump`
não inclui papéis globais do cluster; propriedade e privilégios precisam ser
revisados e atribuídos aos papéis apropriados no destino. O ensaio exclui
ownership/ACLs para validar a restauração com papel de instalação separado.
Não há agendamento, retenção em nuvem ou monitoramento implementado nesta fase.

`tests/test_postgres_backup.py` migra uma origem fictícia com as 28 tabelas,
gera um dump apenas do esquema exclusivo do ensaio e verifica seu catálogo
com `pg_restore --list`. Cria um banco novo, restaura em transação única,
reconcilia fingerprints e sequences, executa Alembic upgrade/check e cadastra
um novo voluntário pelo aplicativo. A remoção final usa exclusivamente o nome
gerado e o OID do banco criado pelo próprio teste, sem alterar outros bancos.

Execute com `TEST_DATABASE_URL` descartável e clientes 17 em
`/usr/lib/postgresql/17/bin`, ou com Docker e `POSTGRES_TEST_CONTAINER`
explicitamente apontando para o contêiner PostgreSQL de teste. A CI passa o
ID do seu próprio serviço. Não há descoberta automática de contêineres ou uso
de um servidor PostgreSQL externo. O caminho Docker também foi validado no
Windows; credenciais não são passadas em argumentos dos clientes.

Consulte os manuais de [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html),
[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html) e
[serviços libpq](https://www.postgresql.org/docs/17/libpq-pgservice.html).

## Critérios de aprovação

A conclusão exige Docker local e volume persistente verificados, cadeia Alembic
válida, dry-run sem gravações, transferência e reconciliação das 28 tabelas,
backup/restauração, concorrência real, continuidade das operações do aplicativo,
preservação dos bancos originais, Ruff/formatação/segurança e CI final aprovada.
O relatório de execução registra os resultados e pendências por SHA. Não
considere um ensaio concluído por contagem de registros ou CI anterior.

## Papéis e preparação de produção

Separe desenvolvimento (`APP_ENV=development`), testes (`APP_ENV=test`, sempre
`TEST_DATABASE_URL` descartável) e produção futura (`APP_ENV=production`). Não
reutilize credenciais ou volumes de produção nos testes. O Compose é uma base
local de desenvolvimento, sem representar disponibilidade, retenção ou segurança
de um serviço gerenciado. `.env` existente nunca é sobrescrito por ferramentas
de migração; no deploy futuro, use o mecanismo de segredos do provedor.

O papel runtime precisa de CONNECT no banco, USAGE no esquema, DML nas tabelas
e USAGE/SELECT nas sequences. Não precisa de superusuário, CREATEDB, CREATEROLE,
REPLICATION, BYPASSRLS, propriedade de tabelas ou permissão para desativar FKs.
Use papel separado e proprietário para Alembic e transferência; remova suas
credenciais dos processos normais após a manutenção.

Exemplo de grants para papéis já criados pelo operador em banco autorizado:

```sql
GRANT CONNECT ON DATABASE roster_test TO roster_runtime;
GRANT USAGE ON SCHEMA public TO roster_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO roster_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO roster_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE roster_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO roster_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE roster_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO roster_runtime;
```

Os privilégios padrão precisam ser configurados para o papel que efetivamente
cria os objetos; grants sobre tabelas atuais não cobrem automaticamente tabelas
futuras. Ajuste nomes e esquema ao ambiente, sem executar esses exemplos contra
um banco existente sem autorização. A aplicação faz o isolamento de ministérios;
o papel compartilhado de runtime não oferece isolamento por ministério no SQL.

`tests/test_postgres_runtime_role.py` cria e remove apenas um papel fictício
exclusivo, verificando seu OID. Testa cadastro com DML mínimo, recusa de DDL e
de desativação de integridade, recusa do papel como importador, recuperação de
conexão obsoleta com pre_ping e rollback após encerramento real de uma conexão
durante transação. Não cria nem altera papéis de produção.

Antes de configurar PostgreSQL gerenciado na próxima fase, valide host, CA e
hostname de TLS (`sslmode=verify-full`), credenciais próprias, conectividade,
privilégios e orçamento de conexões. O total inclui cada processo HTTP, worker,
simulador, sondagem e manutenção; limite o número de processos junto com
DB_POOL_SIZE/DB_MAX_OVERFLOW. Não há provisionamento de Render nesta fase.

## Recuperação e riscos residuais

Falha de conexão durante uma transação exige rollback e repetição da operação
inteira conforme a política de inbox/worker, preservando deduplicação. pre_ping
recupera checkout posterior, sem tornar uma transação interrompida transparente.
Não repita automaticamente um envio de resultado incerto. Reconciliar entrega
externa depende do provedor e não foi validado com a Meta real.

O hash canônico preserva o contrato UTC do aplicativo; timestamps SQLite sem
offset são interpretados como UTC. Se dados externos históricos não seguirem
esse contrato, o operador deve investigar antes de autorizar a migração. A
ferramenta não adivinha fusos, consentimentos, permissões nem corrige inconsistências
históricas de negócio. Incompatibilidades estruturais ou de tipos são recusadas.

O importador precisa de uma janela de manutenção e acesso aos scripts Alembic
do checkout. Não faz sincronização contínua, merge de bancos ocupados, exportação
de um SQLite com WAL ativo nem migração online. A origem deve ser um snapshot
offline consistente. A senha/identidade do destino não prova por si só que ele
é operacionalmente autorizado: a revisão do relatório e a confirmação do operador
continuam obrigatórias. Não foi realizada importação de dados reais.

O backup de ensaio comprova restauração do esquema do aplicativo e registros
fictícios; não certifica backups antigos nem o conteúdo de volumes preexistentes.
Criptografia, retenção, recuperação de desastre e ensaios operacionais periódicos
continuam como trabalho de produção posterior. Testes aprovados não eliminam
todos os riscos nem substituem revisão operacional antes do corte real.

## Validação reproduzível

Configure DATABASE_URL e TEST_DATABASE_URL exclusivamente para PostgreSQL
descartável. Os testes criam e removem seus próprios esquemas, papéis e banco
de restauração; não os execute em bancos originais ou serviços de produção.

```sh
python -m pip check
alembic upgrade head
alembic check
ruff check .
ruff format --check .
bandit -r src
pip-audit --progress-spinner off
pytest -q
```

A suíte completa inclui SQLite, PostgreSQL, versões antigas, downgrade em
ambientes descartáveis, concorrência e falhas reais de conexão. Os testes novos
de backup usam clientes 17 nativos ou o ID explícito do serviço Docker de teste.
Não precisam de Meta, Render, credenciais de produção ou serviços pagos.

Para exercitar fluxos existentes depois de importar a base fictícia SQLite,
use o modo optativo de fixture abaixo. Cada caso PostgreSQL cria um esquema
novo, migra a base de configuração/coordenação e executa os fluxos reais sobre
os IDs preservados. A massa histórica independente testa todas as 28 tabelas.

```sh
pytest -q --migrate-roster-fixtures -m postgres \
  tests/test_distribution.py tests/test_proposals.py tests/test_automation_offers.py \
  tests/test_monthly_opening.py tests/test_waitlist.py tests/test_automatic_replacements.py \
  tests/test_reminders.py tests/test_emergencies.py tests/test_reports.py
```

Essa seleção ampla é opcional e mais demorada; o relatório identifica os casos
representativos efetivamente executados. O padrão da suíte permanece inalterado.
A CI também executa uma seleção de 30 casos representativos com a fixture
migrada, em etapa separada da suíte completa e com `APP_ENV=test` explícito.
Os testes de processos separados têm suas próprias fixtures de transferência.
`tests/test_migrated_simulator_processes.py` inicia coordenador e dois voluntários
em três processos reais compartilhando PostgreSQL, com transporte local. Verifica
convite e aceite espontâneos, isolamento de destinatários, transcrições coladas,
resposta repetida e reinício sem repetir mensagens. Evidências JSON opcionais,
incluindo horários UTC, podem ser gravadas em `ROSTER_EVIDENCE_DIR`, fora do Git.

Há um aviso de depreciação Starlette/httpx nos testes HTTP. Ele não é silenciado;
uma futura atualização do transporte de teste precisa manter compatibilidade
com FastAPI. Nenhum resultado aprovado é inferido somente de uma CI anterior.
