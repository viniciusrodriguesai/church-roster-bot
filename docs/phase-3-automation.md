# Fase 3: arquitetura e entrega incremental

Status: arquitetura definida; implementação em incrementos na branch
`feature/phase-3-automation`. A Fase 2 permanece compatível. Este documento
distingue decisões de arquitetura de funcionalidades já entregues.

## Decisão: proposta, aprovação e confirmação são operações distintas

O `RosterEngine` continua sendo a autoridade para permissões, indisponibilidade,
conflitos, capacidade e limite mensal. Automação não ganha uma forma alternativa
de gravar participações nem uma autorização implícita de terceiro dia.

A distribuição terá três fronteiras:

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
4. Espera FIFO e oferta de vaga, com revalidação no aceite.
5. Substituição por convite, notificações às partes e progressão após recusa.
6. Lembretes configuráveis, relógio testável, UTC e fusos da igreja.
7. Emergências usando os mesmos mecanismos e escalonamento à coordenação.
8. Relatórios autorizados de participação, equilíbrio, vagas e substituições.

Cada incremento deve ter teste de comportamento, documentação atualizada,
`pytest`, `ruff check`, `ruff format --check` e revisão do diff antes do commit.
Migrações e mudanças transacionais exigem testes de downgrade/upgrade e
concorrência aplicáveis. CI verde para o SHA publicado é condição para seguir.
Não criar PR, não fazer merge, não reescrever histórico e não publicar artefatos
temporários ou bancos. Nenhuma conexão à Meta ou infraestrutura paga faz parte
do desenvolvimento.
