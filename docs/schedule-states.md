# Estados da escala e das participações

Os valores persistidos são minúsculos em `snake_case` para preservar `confirmed` e `cancelled` da Fase 1; a interface mostra as formas em maiúsculas.

| Escala | Próximos estados permitidos |
|---|---|
| DRAFT | OPEN, CANCELLED |
| OPEN | UNDER_REVIEW, PUBLISHED, CLOSED, CANCELLED |
| UNDER_REVIEW | OPEN, PUBLISHED, CLOSED, CANCELLED |
| PUBLISHED | CLOSED, CANCELLED |
| CLOSED, CANCELLED | Nenhum |

Inscrições normais pelo WhatsApp exigem `OPEN`. Substituições manuais podem atualizar `PUBLISHED`; `CLOSED` e `CANCELLED` não aceitam alterações. Publicar novamente após alteração cria uma versão com texto e resumo das mudanças.

| Participação | Próximos estados permitidos |
|---|---|
| PENDING_CONFIRMATION | CONFIRMED, DECLINED, CANCELLED |
| CONFIRMED | REPLACEMENT_REQUESTED, CANCELLED, COMPLETED, ABSENT |
| REPLACEMENT_REQUESTED | REPLACED, CONFIRMED, CANCELLED |
| REPLACED, CANCELLED, COMPLETED, ABSENT, DECLINED | Nenhum |

O pedido de substituição mantém a participação antiga ocupando a vaga. A troca manual marca a antiga como `REPLACED` e cria a nova `CONFIRMED` na mesma transação; falhas revertem ambas. `PENDING_CONFIRMATION`, `CONFIRMED`, `REPLACEMENT_REQUESTED` e `COMPLETED` ocupam vaga. Dias confirmados, com substituição solicitada ou concluídos contam no histórico mensal. Toda nova confirmação passa pelo motor da Fase 1.

O estado de vaga é **derivado**: `AVAILABLE`, `RESERVED`, `FILLED`, `REPLACEMENT_PENDING`, `COMPLETED`, `UNCOVERED` ou `CANCELLED`. Não há estado persistido separado da vaga que possa divergir da participação.
