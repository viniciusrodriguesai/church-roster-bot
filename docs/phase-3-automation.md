# Fase 3: arquitetura e entrega incremental

Status: distribuição com aprovação e aceite implementada; incrementos na branch
`feature/phase-3-automation`. A Fase 2 permanece compatível. Este documento
distingue decisões de arquitetura de funcionalidades já entregues.

## Incrementos entregues

- Contrato de arquitetura e baseline da Fase 2 integrada.
- `RosterEngine.check_assignment`: consulta as mesmas regras usadas por `assign`
  e considera uma sequência de `AssignmentIntent` apenas em memória. Não cria
  inscrições, não faz reservas e não envia mensagens. Projeções consomem
  capacidade, dias mensais e compatibilidade de funções para o cálculo. A
  inscrição real continua bloqueando e revalidando os dados atuais. Não há
  migração nem mudança no fluxo de exceção existente neste incremento.
- `DistributionPlanner.propose`: calcula sugestões determinísticas somente para
  ministérios autorizados e meses abertos. A política `balanced-v1` considera
  carga mensal global, histórico recente, escassez de candidatos, disponibilidade
  e sugestões anteriores. As pontuações e rejeições agregadas ficam no resultado;
  nenhuma participação, convite ou exceção é gravada. O relógio é injetável,
  instantes de geração são UTC e cultos já iniciados no fuso da igreja são excluídos.
- No menu do coordenador ou administrador, `PROPOR AAAA-MM` apresenta a prévia
  e as explicações. Repetir o comando recalcula o estado atual; não reserva vagas
  nem altera propostas salvas. A aprovação usa o ID de um rascunho persistido.
- `PLANEJAR AAAA-MM` salva um rascunho identificado com a versão da política,
  a ordem das sugestões, pontuações e falta de cobertura. `VERPROPOSTA <id>`
  recupera o resultado original sem recalcular as escolhas; `DESCARTAR <id>`
  pede confirmação e preserva os itens e a auditoria. A autorização atual é
  reavaliada a cada operação e limitada ao ministério selecionado. Cancelamentos
  repetidos ou concorrentes gravam uma única decisão. Nenhuma inscrição ou
  convite é criado por esses comandos.
- `APROVAR <id>` pede confirmação e revalida a proposta inteira sob bloqueios.
  Uma proposta obsoleta falha atomicamente. Aprovar grava convites normais e a
  outbox, sem confirmar participações. O voluntário responde `ACEITAR A<id>` ou
  `RECUSAR A<id>`, inclusive após a sessão expirar. O aceite revalida capacidade,
  permissão, disponibilidade, conflitos e limite mensal; a coordenação recebe
  a decisão. Repetir a aprovação ou o aceite não duplica convites, inscrições
  ou avisos. Uma impressão digital impede aceitar um culto movido depois do
  convite. O prazo termina no início do culto ou na validade configurada.

## Decisão: proposta, aprovação e confirmação são operações distintas

O `RosterEngine` continua sendo a autoridade para permissões, indisponibilidade,
conflitos, capacidade e limite mensal. Automação não ganha uma forma alternativa
de gravar participações nem uma autorização implícita de terceiro dia.

A distribuição usa três fronteiras:

1. **Planejamento:** leitura do estado atual e cálculo determinístico de uma
   proposta. Retorna candidatos, pontuação explicável e posições sem cobertura;
   não grava inscrições, reservas ou mensagens. Sugestões anteriores no mesmo
   cálculo contam na carga projetada, nos conflitos e na capacidade.
2. **Revisão pelo coordenador:** proposta persistida, identificada e auditável,
   vinculada ao ministério e ao mês. Aprovação exige autorização atual para esse
   ministério; reabrir a conversa ou repetir a aprovação não duplica efeitos.
3. **Aplicação:** revalidação das regras no estado atual, transação curta e
   bloqueios na ordem definida pelo motor. Uma proposta obsoleta não substitui
   inscrições existentes. A aplicação integral falha atomicamente se uma vaga
   deixou de estar disponível. Convites proativos exigem consentimento; aceite
   é obrigatório antes de confirmar uma participação atribuída pela automação.

Publicação de escala continua independente de aprovação de proposta. O mecanismo
de exceção existente (`ThirdDayRequest`, justificativa e aceite) é o único caminho
para um terceiro dia. O planejador normal deve apresentar a falta de cobertura
em vez de criar ou aceitar uma exceção automaticamente.

## Política inicial de equilíbrio

Heurística determinística, sem dependência de serviços externos ou modelos de IA:

- Considerar somente membros ativos da igreja, com função ativa e permissão.
- Respeitar indisponibilidade, pares de funções incompatíveis, capacidade e dois
  dias distintos por mês, incluindo participações existentes e sugestões.
- Priorizar posições com menos candidatos elegíveis; desempatar por data,
  horário e ID de vaga. Recalcular elegibilidade após cada sugestão.
- Entre candidatos elegíveis, priorizar menor carga projetada no mês, menor
  quantidade de participações no mês e menor histórico recente de dias servidos.
  Desempatar pela participação mais antiga e finalmente pelo ID estável.
- Contar histórico de todos os ministérios da igreja para evitar que o mesmo
  voluntário fique sobrecarregado por planejamentos independentes.
- Registrar os valores que motivaram cada escolha e a versão da política.

A janela histórica inicial será de 90 dias anteriores ao mês planejado.
Cultos futuros nessa janela não são contados como histórico prestado.
Participações canceladas, recusadas, substituídas e ausências não contam como
serviço prestado. Reservas e pedidos de substituição ainda ocupam capacidade no
mês alvo. O limite mensal é global para o voluntário, como na Fase 2.

Não há garantia de solução ótima: funções raras e restrições combinadas podem
exigir revisão humana. Métricas e posições descobertas fazem parte da proposta;
o sistema não oculta falta de candidatos. Datas de cultos são civis no fuso da
igreja; instantes de criação, expiração, envio e lembrete são UTC.

## Fronteiras dos próximos módulos

| Responsabilidade | Fronteira prevista | Invariante |
| --- | --- | --- |
| Abertura mensal automática | Política optativa por ministério e tarefa periódica | Só abrir escalas configuradas; nunca reabrir escala encerrada |
| Propostas e seleção | `scheduling` | Calcular não confirma nem envia |
| Lista de espera | Fila persistida por vaga, FIFO por entrada e ID | Notificar não atribui; aceite revalida vaga e elegibilidade |
| Substituição automática | Convite persistido ligado a `ReplacementRequest` | O original ocupa a vaga até troca aceita e atômica |
| Lembretes | Política configurável e tarefa periódica persistente | Uma chave lógica por participação e antecedência |
| Emergências | Mesma seleção e convites, com prioridade temporal | Urgência não contorna limites nem aceite |
| Relatórios | Consultas com escopo de ministério | Autorização atual e histórico auditável |
| Entrega | Outbox e worker existentes | Efeitos de negócio e saída na mesma transação |

Uma fila de espera não será um segundo cadastro de capacidade. Convites pendentes
não equivalem a inscrições. Candidatos que perderam permissão, consentimento ou
disponibilidade entre seleção e envio/aceite devem ser revalidados em cada fronteira.

O transporte mantém a política da Fase 2: janela de atendimento de 24 horas,
template aprovado fora da janela, `blocked_window` sem template e nenhuma
repetição automática de envios ambíguos. Novos tipos de mensagem terão chaves de
deduplicação persistidas e identificação do objeto de negócio. Reiniciar um
worker não deve recriar decisões nem interpretar entrega como aceite.

## Concorrência e migrações

- PostgreSQL é a referência para concorrência real; SQLite em arquivo atende o
  simulador com `BEGIN IMMEDIATE`, sem prometer bloqueios de linha.
- Aplicações usam a ordem de bloqueio do motor: voluntário antes de vaga.
  Operações com vários voluntários/vagas precisam de ordem total estável.
- Capacidade é reavaliada sob bloqueio. Índices únicos protegem identidade de
  convites, aprovações, posições de espera e saídas deduplicadas.
- Migrações serão aditivas, reversíveis e testadas sobre dados sintéticos da
  versão anterior. Nenhuma migração será executada nos bancos locais existentes.
- Testes destrutivos de concorrência usam exclusivamente PostgreSQL descartável.
  Testes não devem herdar uma URL desconhecida de banco real.

## Sequência de incrementos e critérios de conclusão

1. Arquitetura e baseline: este contrato e validação da Fase 2 na main integrada.
2. Planejador sem efeitos colaterais: equilíbrio, histórico, elegibilidade,
   conflitos e carga projetada; testes de determinismo e de leitura sem escrita.
3. Revisão persistente e aplicação segura: migração, autorização, aprovação,
   aceite, rollback, idempotência e concorrência PostgreSQL; comandos do simulador.
4. Abertura mensal automática configurável, com recuperação após reinício.
5. Espera FIFO e oferta de vaga, com revalidação no aceite.
6. Substituição por convite, notificações às partes e progressão após recusa.
7. Lembretes configuráveis, relógio testável, UTC e fusos da igreja.
8. Emergências usando os mesmos mecanismos e escalonamento à coordenação.
9. Relatórios autorizados de participação, equilíbrio, vagas e substituições.

Cada incremento deve ter teste de comportamento, documentação atualizada,
`pytest`, `ruff check`, `ruff format --check` e revisão do diff antes do commit.
Migrações e mudanças transacionais exigem testes de downgrade/upgrade e
concorrência aplicáveis. CI verde para o SHA publicado é condição para seguir.
Não criar PR, não fazer merge, não reescrever histórico e não publicar artefatos
temporários ou bancos. Nenhuma conexão à Meta ou infraestrutura paga faz parte
do desenvolvimento.

## Validação da distribuição

`tests/test_assignment_preview.py` cobre a equivalência entre consulta e
inscrição e os efeitos das sugestões em memória. `tests/test_distribution.py`
cobre equilíbrio, desempate pelo histórico, janela histórica, exclusão de
cancelamentos e ausências, escassez de funções, limite global entre ministérios,
capacidade, conflitos, autorização atual, fusos e roteamento real dos comandos.
Ambos rodam em SQLite e PostgreSQL quando `TEST_DATABASE_URL` está definido.
Um listener SQL verifica que o planejamento em si executa somente `SELECT`.
As mensagens de resposta à consulta são gravadas pela conversa na outbox normal,
sem criar inscrições nem convites proativos.

O planejador consulta a matriz de elegibilidade atual uma vez e atualiza carga
e compatibilidade projetadas em memória. Cada sugestão selecionada passa de novo
pelo motor compartilhado com todas as projeções anteriores. Um teste limita o
número de consultas em um mês sintético para evitar a regressão de recomputar
toda a matriz SQL após cada escolha.

## Migração de propostas

`b31a0c4e6721` acrescenta `roster_proposals` e `roster_proposal_items` sem alterar
as tabelas de participações ou mensagens. Os testes executam upgrade, downgrade,
novo upgrade e comparação de metadados em SQLite temporário e em um schema
PostgreSQL descartável exclusivo de cada teste. Dados legados da outbox são
verificados antes e depois. O downgrade remove somente as novas tabelas de
propostas; rascunhos da Fase 3 devem ser exportados antes de um downgrade caso
precisem ser conservados. Os bancos locais existentes não são migrados pelo
desenvolvimento.

## Convites normais e relógio

`c31a2e6b8904` acrescenta `automation_invitations` e um vínculo opcional único na
outbox. Convites excepcionais numéricos da Fase 2 continuam separados. Para
automação normal, `A<id>` evita ambiguidade com os IDs de exceção.

`WHATSAPP_AUTOMATION_TEMPLATE` é um template separado, com um parâmetro de corpo
e dois botões de resposta rápida para os comandos de aceite/recusa. Sem janela
de 24 horas e sem esse template aprovado, o envio fica `blocked_window`.
`INICIAR` libera somente convites bloqueados ainda sem tentativa. Consentimento
é revalidado antes do envio. Falhas e resultados ambíguos seguem a política
existente de não reenviar automaticamente; a coordenação recebe avisos deduplicados.
O simulador usa um nome de template fictício e transporte local.

`AUTOMATION_INVITE_TTL_HOURS` controla a validade (48 horas por padrão, de 1 a
168). O worker expira convites sem depender de uma nova mensagem. Instantes
internos são UTC; horários civis usam o fuso IANA da igreja. Horários inexistentes
na mudança de horário de verão são rejeitados; horários ambíguos usam a primeira
ocorrência. A aprovação não substitui o aceite e nunca autoriza um terceiro dia.

A validação com dois processos reais encontrou contenção de SQLite durante uma
operação longa de proposta. A regressão `tests/test_sqlite_busy.py` mantém um
writer aberto e verifica que os terminais preservam a entrada, que eventos
pendentes não consomem tentativas e que um bloqueio no recibo após o transporte
não repete o envio. Somente códigos de contenção SQLite são tratados assim;
outras falhas operacionais propagam normalmente.
