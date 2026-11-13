# Fase 4.2 — PostgreSQL e migração verificável

Status: em desenvolvimento na feature/phase-4-production, base 5bfd27a.
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

A ferramenta ainda em implementação será aprovada separadamente, incluindo
transações reais, rollback, confirmação do destino e reconciliação pós-cópia.

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

## Gates ainda em implementação

Faltam ferramenta SQLite → PostgreSQL/dry-run, reconciliação, round trips de
migração, backup/restauração oficial, testes após transferência e relatório final.
Não considerar a fase concluída antes dessas verificações e da CI final aprovada.
