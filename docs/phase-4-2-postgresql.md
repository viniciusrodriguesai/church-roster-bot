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

## Gates ainda em implementação

## Reconciliação canônica

database.canonical verifica tipos reais, limites INTEGER/VARCHAR, booleanos
estritos e precisão JSON com Decimal. Não transforma números em strings nem
silencia chaves JSON duplicadas, NaN, NUL ou Unicode inválido. Timestamp SQLite
sem offset segue o contrato UTC do projeto; datas e horários de cultos continuam
civis. SQL NULL e JSON null têm fingerprints distintos. A prova é SHA256 por
tabela, por linha em ordem de PK, com nomes de colunas explícitos. A mesma
contagem com conteúdo diferente é uma divergência, não uma migração aprovada.

## Gates ainda em implementação

Faltam ferramenta SQLite → PostgreSQL/dry-run, reconciliação, round trips de
migração, backup/restauração oficial, testes após transferência e relatório final.
Não considerar a fase concluída antes dessas verificações e da CI final aprovada.
