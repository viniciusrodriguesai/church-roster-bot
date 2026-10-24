# Arquitetura — Fase 1

O projeto é um monólito modular. `ministries` define igreja, ministério, funções e pares incompatíveis. `members` guarda voluntários, coordenadores e permissões. `services` guarda cultos e vagas. `availability` guarda períodos de indisponibilidade. `scheduling` concentra as regras e transações. `core` contém configuração e banco.

## Invariantes

- Um voluntário só entra numa função para a qual tem permissão e numa igreja à qual pertence.
- A contagem mensal usa **datas distintas de culto**. Duas funções no mesmo dia contam uma vez.
- A terceira data exige uma solicitação de coordenador com justificativa e aceite explícito do voluntário. A solicitação pendente não cria inscrição.
- Pares de funções incompatíveis são configurados em `role_conflicts`; a configuração inicial separa ofertas de abrir, fechar e abrir+fechar.
- Uma vaga não ultrapassa sua capacidade. Registros confirmados são únicos para o par vaga/voluntário.
- Indisponibilidade impede novas inscrições; se ela cruzar inscrição existente, a operação retorna os IDs conflitantes para o fluxo de substituição da Fase 3.

## Concorrência

Cada alteração usa uma transação curta. Ao inscrever, o motor bloqueia primeiro a linha do voluntário, depois a da vaga (`SELECT ... FOR UPDATE` no PostgreSQL), sempre nessa ordem. Assim, inscrições simultâneas para a última vaga e para o limite mensal são serializadas. O índice parcial impede inscrição confirmada duplicada do mesmo voluntário na mesma vaga. O SQLite usado nos testes rápidos ignora bloqueios; o teste de concorrência em PostgreSQL deve ser executado antes de colocar o bot em produção.

## Limites desta fase

O relatório de capacidade mostra o total de vagas e calcula um limite inferior de dias de voluntário para funções incompatíveis. Funções compatíveis no mesmo culto podem ser cobertas pela mesma pessoa; entre cultos no mesmo dia, o cálculo usa o maior limite. Também alerta quando uma função tem menos pessoas habilitadas do que vagas. Ainda não executa uma busca completa de cobertura considerando todas as permissões, indisponibilidades e preferências. O fluxo de aprovação no WhatsApp, auditoria, substituições, lista de espera, mensagens, confirmação de presença e publicação de escalas estão pendentes. O endpoint de saúde só confirma que o processo HTTP responde; ainda não verifica o banco.
