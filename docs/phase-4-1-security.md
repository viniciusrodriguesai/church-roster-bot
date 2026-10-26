# Fase 4.1 — auditoria de segurança e preparação para produção

Status: em andamento em `feature/phase-4-production`, a partir de `d563eb2`.
As Fases 1–3 estão integradas à main. Esta etapa não implanta serviços, não
configura PostgreSQL definitivo e não conecta números, tokens ou templates reais.
Toda operação de coordenadores e voluntários continua pelo WhatsApp.

## Arquitetura e limites de confiança

| Fronteira | Responsabilidade | Garantia revisada |
| --- | --- | --- |
| FastAPI / `whatsapp.payloads` | Autenticar bytes originais e validar envelope/eventos | Nenhum evento de negócio sem HMAC; limites antes de armazenamento |
| `whatsapp.inbox` | Persistir lote e deduplicar identidade | Responder sucesso somente após commit; duplicata comprovada não é erro de integridade genérico |
| `conversations` / `administration` | Estado de conversa, confirmação e autorização atual | Número de origem autenticado pelo webhook; escopo reavaliado por ação |
| `scheduling.engine` / `lifecycle` | Elegibilidade, capacidade, dias mensais e troca | Bloqueios e revalidação antes da inscrição; terceiro dia exige justificativa e aceite |
| Planejador, ofertas, fila, substituições e emergências | Propor e convidar sem atribuição implícita | O aceite é uma transação separada; não contornar o motor |
| `notifications.worker` / outbox | Receber, processar, enviar e recuperar | Negócio e rascunho atômicos; claim antes do transporte |
| `audit` / relatórios | Histórico e leitura administrativa | Igreja/ministério e ator atuais; não inferir presença por confirmação |
| SQLAlchemy / Alembic | Sessões, integridade e evolução | Consultas vinculadas; PostgreSQL para concorrência; migrações descartáveis |

O banco e a posse dos segredos da aplicação são fronteiras confiáveis. Um invasor
sem segredo não deve gravar na inbox. Um usuário legítimo do WhatsApp continua
não confiável para texto, IDs, comandos e escopo. Assinatura válida não substitui
autorização. O SQLAlchemy não autoriza ações por si só.

O webhook utiliza uma thread de infraestrutura para o SQLAlchemy síncrono;
não bloqueia o loop assíncrono enquanto espera o banco. Sessões são locais à
transação e fechadas por context managers. O simulador usa o mesmo roteador e
worker, com transporte fictício. Não há nova fila externa ou painel administrativo.

## Registro de achados

| ID | Gravidade | Impacto | Situação |
| --- | --- | --- | --- |
| S01 | Alta | Corpo inteiro era lido antes do limite, permitindo consumo de memória e espera sem prazo | Corrigido: leitura incremental limitada e prazo de leitura |
| S02 | Média | Tokens/assinaturas não ASCII geravam erro 500; remetente com tipo inválido podia derrubar processamento | Corrigido: assinatura ASCII estrita, comparação de tokens em bytes, telefone tipado/ASCII |
| S03 | Alta | Qualquer IntegrityError era tratado como duplicata; SQLite podia liberar SAVEPOINT fora de BEGIN e confirmar parte do lote | Corrigido: duplicata comprovada e BEGIN IMMEDIATE em SQLite; demais erros retornam 503 com rollback |
| S04 | Média | Truncamento do identificador de status colidia entre recibos diferentes | Corrigido: hash da identidade completa quando excede a coluna; IDs curtos preservados |
| D01 | Alta no ambiente de instalação | pip 25.0.1 incluído na imagem de teste tem alertas de extração/instalação de pacotes | Bootstrap da CI exige pip >=26.2.2; repetir auditoria do ambiente atualizado |
| L01 | Média | Migrações programáticas desativavam loggers da aplicação, suprimindo diagnóstico de falhas | Corrigido: preservar loggers; opção de manter configuração de logging do chamador; engine descartado em finally |
| A01 | Em investigação | Campos globais de voluntário compartilhado podem afetar outro ministério | Revisão e regressões de autorização pendentes |
| C01 | Em investigação | Edição de culto e inscrição usam bloqueios diferentes | Regressão concorrente PostgreSQL pendente |
| E01 | Média | Exceções técnicas podem aparecer em respostas de comandos inválidos | Correção e testes pendentes |
| R01 | Média | Indisponibilidade do banco durante recuperação inicial termina o worker | Revisão de recuperação pendente |

As gravidades descrevem impacto e condições de exploração; não são pontuações
CVSS ou certificação. As correções são verificadas com casos que falham antes
do ajuste. Não concluir a etapa enquanto houver achados críticos/altos no escopo
sem solução ou sem verificação obrigatória.

## Webhook: comportamento após o primeiro incremento

- Assinatura ausente, malformada ou incorreta: 403, sem escrita. O digest é
  calculado sobre os bytes recebidos, sem reserializar JSON.
- `WEBHOOK_MAX_BYTES` (262144): limite do corpo durante streaming, inclusive
  sem Content-Length. Content-Length não substitui contagem dos bytes reais.
- `WEBHOOK_READ_TIMEOUT_SECONDS` (5): prazo para leitura, com resposta 408.
- `WEBHOOK_MAX_EVENTS` (1000): limite agregado por lote, antes de abrir transação.
  Excesso de corpo/lote retorna 413. Ajustes precisam respeitar o envelope real
  do provedor; a conexão real à Meta não foi executada nesta fase.
- JSON inválido, não finito, remetente inválido e formatos incorretos: 400
  genérico. Tipos de mensagem não implementados podem ser armazenados e ignorados
  pelo worker, sem criar participação. Campos extras legítimos são preservados.
- Evento duplicado: resposta 200 com `accepted: 0`. Falha de integridade que
  não corresponde a identidade já persistida: rollback do lote, 503 e log
  `webhook_storage_failed` com classe da exceção, sem payload, SQL ou credenciais.
- Recibos longos não são truncados. Identidades curtas e o hash de IDs longos de
  mensagem seguem o formato anterior. Recibos antigos cujo ID já foi truncado
  continuam preservados; as saídas de negócio permanecem protegidas por dedupe.

O rate limit por telefone continua persistido em ConversationSession, sob
bloqueio, antes de executar comandos. Isso não é um limite global de conexões
HTTP: limites de conexão, cabeçalhos, TLS e rate limit na borda são requisitos
da futura implantação, sem criar infraestrutura nesta etapa.

O primeiro pip-audit encontrou 12 registros relativos ao pip 25.0.1 da imagem
de teste, representando seis IDs de advisory distintos, com entradas duplicadas
na resposta da base. Nenhuma dependência de runtime do aplicativo recebeu alerta
nessa execução. Isso não é uma garantia de segurança. O instalador faz parte da
cadeia de fornecimento: a CI passa a atualizar pip para `>=26.2.2,<27` antes de
instalar o projeto. A auditoria será repetida após essa atualização, sem ignorar
IDs ou desligar a falha de segurança. Bandit não encontrou achados no src nessa
primeira execução. Guardar as versões e os JSONs no relatório final.

## Falhas, segredos e riscos residuais

- Resultado de transporte `unknown` não é reenviado automaticamente: a Meta pode
  já ter aceitado a mensagem. Repetir indiscriminadamente geraria duplicidade.
- Produção continua com uma instância do worker conforme contrato atual;
  concorrência transacional testada não implica suporte irrestrito a várias
  instâncias executando recuperação de startup simultânea.
- Banco indisponível e transação rejeitada não podem produzir confirmação
  parcial. PostgreSQL descartável é obrigatório para validar bloqueios reais.
- Um status que chega antes de gravar o ID de saída ainda exige revisão específica
  nesta auditoria. Não prometer reconciliação automática sem testá-la.
- Não registrar corpos, telefones, tokens, URLs de banco ou str(exc) de banco.
  Logs devem usar identificador interno e tipo do erro. O servidor HTTP externo
  também deve omitir/redigir a query de verificação Meta: ela contém verify_token.
  Na execução local com Uvicorn, `--no-access-log` evita registrar essa query;
  a política de logs da futura borda precisa ser verificada antes de implantação.
- Varreduras de dependências e padrões de segredo têm cobertura limitada;
  ausência de alertas não prova ausência de vulnerabilidades. Datas, versões,
  resultados e limitações devem acompanhar o relatório final.

## Verificação reproduzível

Usar SQLite novo e PostgreSQL descartável explicitamente configurado em
DATABASE_URL/TEST_DATABASE_URL. Nunca apontar testes a demo.sqlite3 ou a um
banco existente: fixtures de concorrência criam/removem tabelas próprias.

```text
pytest -q tests/test_webhook_security.py tests/test_phase2.py
ruff check .
ruff format --check .
alembic upgrade head
alembic check
pytest -q
```

As regressões cobrem corpo fragmentado com cauda que não deve ser lida, prazo
de leitura, assinatura/token inválidos, tipos maliciosos, rollback de lote e
recibos longos. O erro de persistência é inserido no limite real do flush;
a transação e as restrições são exercitadas em SQLite e PostgreSQL.

## Health e gates de conclusão

`GET /health/live` e `/health` respondem 200 com `{"status":"ok"}` sem acessar
o banco. `GET /health/ready` responde 200/ok ou 503/unavailable, sem detalhes
de conexão e com Cache-Control: no-store. O timeout HTTP é
`HEALTH_READY_TIMEOUT_SECONDS` (2 segundos, configurável entre 0.05 e 10).

A CI do primeiro commit de health detectou supressão de logs após migrações
programáticas (617 testes passaram e um falhou). A configuração do Alembic foi
corrigida e recebeu regressões específicas; não se removeu a asserção de log.
Quem incorpora Alembic e já configura logging pode usar
`config.attributes["configure_logger"] = False` para preservar os handlers.

A sondagem usa conexão independente e NullPool, sem alterar o pool da aplicação.
PostgreSQL tem connect_timeout, statement_timeout, lock_timeout e transação
somente leitura. SQLite em arquivo é aberto com mode=ro: não cria um arquivo
ausente e lê schema_version para detectar corrupção. A conexão é fechada pelo
context manager e o pool de sondagem é descartado no encerramento do lifespan.

Um timeout HTTP não pode interromper com segurança um driver síncrono: existe
no máximo uma thread e um Future em andamento por aplicação. Pedidos simultâneos
compartilham esse Future, sem abandoná-lo/cancelá-lo ao devolver 503. Os timeouts
nativos limitam a operação subjacente; ela fecha recursos ao terminar. DNS do
sistema e DSNs com múltiplos hosts podem demorar além do prazo HTTP, mas não
criam uma fila crescente de sondagens. Testar a configuração efetiva antes da
implantação. O endpoint verifica conectividade, não comprova permissões de
escrita, estado das migrações, funcionamento da Meta ou do worker.

SQLite privado em memória não é elegível para readiness de implantação e retorna
503: uma conexão independente não alcança esse banco; compartilhar o StaticPool
poderia reverter uma transação de negócio. Liveness e conversas continuam
compatíveis. Para simular readiness, usar SQLite novo em arquivo. Sem inicializar
o lifespan ASGI, readiness também retorna 503; TestClient deve ser usado com `with`.

Antes de concluir esta etapa: resolver os achados no escopo, testar autorização,
health/falhas, rodar a suíte completa PostgreSQL e migrações, Ruff/formatação,
auditoria de dependências, revisar diff, confirmar hashes dos bancos originais
e aprovar CI do SHA publicado na branch. Não fazer merge na main.

Prontidão para implantação ainda exige decisões posteriores de credenciais,
templates, TLS, backups/restauração, supervisão, retenção e observabilidade.
Essas atividades não são implementadas na Fase 4.1. A Fase 4.2 depende de nova
solicitação; nenhuma etapa seguinte é iniciada automaticamente.

## Referências primárias

- [Starlette: leitura de Request.stream](https://www.starlette.io/requests/).
- [Psycopg: conexões e parâmetros](https://www.psycopg.org/psycopg3/docs/basic/usage.html).
- [PyPA pip-audit: cobertura e limitações](https://github.com/pypa/pip-audit).
- [pip 26.2.2, distribuição oficial](https://pypi.org/project/pip/26.2.2/).
- [Advisory PyPA para pip, PYSEC-2026-3721](https://github.com/pypa/advisory-database/blob/main/vulns/pip/PYSEC-2026-3721.yaml).
