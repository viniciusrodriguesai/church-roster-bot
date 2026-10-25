# Arquitetura

A Fase 2 está integrada. A distribuição da Fase 3 separa planejamento, aprovação
e confirmação; veja [Arquitetura e incrementos da automação](phase-3-automation.md)
para o estado de cada módulo e o roteiro das funcionalidades futuras.

Monólito modular com FastAPI, SQLAlchemy 2, Alembic e PostgreSQL. O módulo `scheduling.engine` permanece como fonte das regras de inscrição. `scheduling.lifecycle` define transições, substituições manuais e versões. `administration` aplica o escopo do coordenador. `conversations` mantém as etapas no banco. `whatsapp` contém o webhook, parser e cliente Graph API. `notifications.worker` consome eventos e envia a caixa de saída. `audit` registra mudanças sem copiar números ou tokens para os detalhes.

## Caminho de uma mensagem

1. FastAPI verifica HMAC-SHA256 sobre os bytes originais, valida o envelope e grava cada evento em `inbound_events` com ID único. Responde só depois do commit. Falha de banco retorna 503 para permitir nova entrega.
2. O worker bloqueia um evento pendente com `SELECT ... FOR UPDATE SKIP LOCKED` no PostgreSQL. Roteamento, estado da conversa, regras de escala, auditoria e mensagens de resposta são gravados **na mesma transação**. Se ela falhar, o evento continua pendente.
3. A caixa `outbound_messages` é enviada separadamente pela Cloud API. IDs da Meta e eventos de entrega são persistidos. Respostas livres são bloqueadas depois de 24 horas da última mensagem do destinatário; templates configurados podem ser usados para convites proativos.

Na confirmação de uma exceção, `notifications.invitations` grava uma saída única vinculada ao convite, dentro da transação da conversa. O worker revalida consentimento e janela, preenche o template aprovado quando necessário e registra a tentativa em uma transação anterior à chamada externa. Falhas definitivas, bloqueios e resultados incertos geram avisos deduplicados ao coordenador. A resposta ACEITAR/RECUSAR também gera aviso na transação da decisão e funciona após a expiração da sessão. Status de transporte nunca criam inscrições. Veja o [contrato dos templates](whatsapp-setup.md#contrato-dos-templates-aprovados).

`scheduling.distribution` calcula a proposta sem escrita; `scheduling.proposals`
preserva os itens e motivos e verifica o escopo atual; `scheduling.offers` aprova
sob bloqueios ordenados e cria convites normais. `notifications.automation`
prepara a entrega, respeita consentimento e templates e avisa a coordenação.
O aceite `A<id>` passa de novo pelo motor antes de criar a inscrição. Convites
não reservam capacidade. Uma impressão digital vincula o aceite ao culto
oferecido, incluindo seu fuso; alterações cancelam o convite. A manutenção do
worker expira convites de forma transacional e idempotente, sem exigir entrada
de um usuário. Exceções numéricas da Fase 2 mantêm seu fluxo independente.

`scheduling.automation` executa a abertura mensal optativa com políticas por
ministério. O worker verifica as tarefas periodicamente, sem depender de mensagens
recebidas. `notifications.coordinators` centraliza os avisos deduplicados e seu
escopo de igreja/ministério. A política começa desabilitada; a configuração
exige confirmação de um coordenador ou administrador autorizado.

`scheduling.waitlist` mantém as posições FIFO e oferece capacidade pela mesma
outbox de convites normais. Cada posição ativa é única por voluntário/vaga;
aceite, recusa e expiração atualizam a fila na transação da decisão. A manutenção
executa expiração, abertura e seleção em transações separadas para preservar a
ordem de bloqueios. A fila não substitui a validação de capacidade no aceite.

`scheduling.replacements` acompanha pedidos abertos de substituição e usa
`scheduling.candidates` para priorizar elegíveis por carga e histórico. Mantém
um convite pendente por pedido e tenta outra pessoa após recusa, expiração ou
perda de elegibilidade. A troca só libera o original na transação do aceite.
`notifications.members` vincula avisos ao voluntário, com consentimento e telefone
revalidados antes do transporte. `services.timing` centraliza identidade e horário
UTC dos cultos; um horário civil inexistente exige revisão da coordenação.

O simulador escreve o mesmo evento de entrada e usa o mesmo worker e roteador. Ele **não chama a Meta**. Os testes rápidos usam SQLite; a CI usa PostgreSQL para migração e concorrência. Em produção, use somente PostgreSQL.

No terminal, uma thread processa e entrega mensagens com transporte local e consulta a caixa recebida enquanto a entrada de texto aguarda no processo principal. Assim, um convite iniciado em outro terminal aparece sem nova entrada. Transações de processamento e reserva de envio usam `BEGIN IMMEDIATE` no SQLite para serializar os terminais locais, enquanto o PostgreSQL mantém os bloqueios de linha existentes.

Contenção temporária `SQLITE_BUSY`/`SQLITE_LOCKED` não encerra o simulador.
Reivindicações que sofreram rollback aguardam outro ciclo, sem consumir uma
tentativa. Entradas digitadas são conservadas até a gravação. Se o transporte
já retornou, apenas a transação do recibo é repetida, nunca o envio externo.
Outros erros de banco continuam sendo reportados. Essa política é específica
do SQLite local e não mascara falhas de PostgreSQL.

A fronteira de entrada do terminal filtra transcrições antes de criar eventos: linhas marcadas com `[bot]`/`[sim]`, prompts, opções renderizadas e blocos com várias linhas não são comandos. Cabeçalhos do formato antigo também são reconhecidos, inclusive com acentos corrompidos no console. Toda linha de saída recebe uma marca, inclusive parâmetros de templates. Essa proteção é exclusiva da interface local; o webhook e `receive()` preservam texto livre. A apresentação consulta somente saídas enviadas ao telefone daquele terminal e registra seus IDs em memória, incluindo mensagens antigas que estavam pendentes. Mensagens já enviadas antes da abertura do terminal não são reapresentadas; uma mudança posterior de `sent` para `delivered`/`read` também não repete a apresentação. Nenhum ciclo de consulta cria eventos de entrada.

## Regras preservadas

- Limite normal de dois **dias distintos** por mês; múltiplas funções compatíveis no mesmo dia contam uma vez.
- Terceiro dia só com solicitação de coordenador autorizado, justificativa e aceite do voluntário. O aceite revalida regras e vagas.
- Abertura/fechamento e ofertas são incompatíveis no mesmo culto; pares de conflitos são configuráveis.
- Inscrição bloqueia voluntário e vaga nessa ordem. Capacidade da vaga e índice parcial protegem contra duplicidade.
- Indisponibilidade impede novas inscrições; se atingir uma inscrição, abre pedido de substituição sem apagar o responsável.

## Estados e versões

Veja [Estados da escala](schedule-states.md). Estados lógicos de vaga são derivados das participações, sem coluna de vaga que possa ficar desatualizada. Cada publicação grava conteúdo e uma impressão digital dos IDs e estados de participação; alterações criam nova versão mesmo se dois responsáveis tiverem o mesmo nome. A atualização para encaminhar é gerada por diferença de linhas. Histórico anterior não é apagado.

## Controle de acesso

O número recebido de um webhook assinado é procurado no cadastro. Voluntários veem os próprios dados e vagas de funções autorizadas. Coordenadores operam somente ministérios ligados em `coordinator_ministries`; o administrador geral gerencia a igreja. O primeiro administrador precisa de bootstrap local por um operador confiável. Uma instalação usa um número oficial para uma igreja nesta fase; roteamento de múltiplas igrejas por número de destino ainda não foi implementado.

## Limites atuais

- O processo HTTP e o worker devem permanecer ativos em hospedagem própria. O endpoint `/health` não testa o banco.
- Uma perda de rede após a Meta aceitar um envio tem resultado ambíguo. O worker marca `unknown` e não repete automaticamente para evitar mensagem duplicada. HTTP 429 tem até três tentativas. Monitore e resolva `failed`, `unknown` e `blocked_window`.
- Rode uma instância do worker nesta fase. Eventos de status que chegarem antes do registro do ID de saída podem exigir reconciliação manual.
- Um template aprovado e consentimento são necessários para convites fora da janela de 24 horas; sem isso, o convite continua registrado, mas a notificação fica bloqueada.
- Lembretes, emergência e relatórios avançados permanecem nos próximos incrementos da Fase 3.
- Configure retenção/eliminação de eventos com dados pessoais, backup PostgreSQL e monitoramento antes do uso real. Não existe rotina automática de limpeza nesta fase.
