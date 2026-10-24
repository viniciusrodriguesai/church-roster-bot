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

O pedido de substituição mantém a participação antiga ocupando a vaga. A troca manual marca a antiga como `REPLACED` e cria a nova `CONFIRMED` na mesma transação; falhas revertem ambas. `PENDING_CONFIRMATION`, `CONFIRMED`, `REPLACEMENT_REQUESTED` e `COMPLETED` ocupam vaga e contam no limite de dias do mês. Reservas só podem ser confirmadas por `RosterEngine.confirm_reservation`, que revalida as mesmas regras da inscrição; a função genérica de transição não permite contornar essa validação. O menu normal confirma diretamente após o aceite do usuário, sem criar uma reserva intermediária.

O estado de vaga é **derivado**: `AVAILABLE`, `RESERVED`, `FILLED`, `REPLACEMENT_PENDING`, `COMPLETED`, `UNCOVERED` ou `CANCELLED`. Não há estado persistido separado da vaga que possa divergir da participação.
