# Configurar a WhatsApp Cloud API oficial

**Não há número real conectado a este repositório.** A configuração da Meta depende da conta e da aprovação do usuário. Não use número pessoal se o objetivo é um número exclusivo do bot.

1. Crie ou use uma aplicação Meta com o produto WhatsApp Business Platform e um número elegível para a Cloud API. A Meta pode exigir dados e verificação da organização; não preencha dados fictícios da igreja.
2. Obtenha `WHATSAPP_PHONE_NUMBER_ID`, um token com permissão de mensagens, `META_APP_SECRET` e um `WHATSAPP_VERIFY_TOKEN` escolhido por você. Guarde-os somente nas variáveis de ambiente ou gerenciador de segredos da hospedagem. Defina `WHATSAPP_API_VERSION` para uma versão Graph API suportada pela conta.
3. Disponibilize `https://<seu-domínio>/webhooks/whatsapp` por HTTPS. A verificação GET devolve o `hub.challenge` somente quando o token confere. Inscreva o campo `messages` da conta WhatsApp para receber mensagens e status.
4. Configure templates aprovados para convites iniciados pelo bot fora da janela de 24 horas. `WHATSAPP_INVITE_TEMPLATE` é opcional durante a simulação; sem ele convites proativos fora da janela ficam bloqueados. Configure `WHATSAPP_TEMPLATE_LANGUAGE` de acordo com a aprovação.
5. Aplique `alembic upgrade head`, execute FastAPI e um worker persistente (`python -m church_roster_bot.notifications.worker`). Teste com números autorizados antes de operar uma escala real. Não há suporte para publicar diretamente em grupos comuns.

O webhook usa os bytes originais e `X-Hub-Signature-256`. Rejeita assinatura inválida, payload fora do formato e tamanho excessivo. A caixa de saída registra falhas sem gravar token nos logs. O servidor precisa de conectividade com `graph.facebook.com`.

## Contrato dos templates aprovados

Para convites normais da Fase 3, `WHATSAPP_AUTOMATION_TEMPLATE` usa **um parâmetro
de texto no corpo** e dois botões de resposta rápida: ACEITAR (índice 0) e RECUSAR
(índice 1). O worker preenche os payloads `ACEITAR A<id>` e `RECUSAR A<id>`. O corpo
inclui data, horário, função, motivo e os comandos. É um template separado dos
convites excepcionais. Sem ele, fora da janela, o convite normal fica bloqueado.
Nenhum template é solicitado ou aprovado automaticamente por esta configuração.

`WHATSAPP_INVITE_TEMPLATE` deve apontar para um template já aprovado na Meta, no idioma de `WHATSAPP_TEMPLATE_LANGUAGE`, com cinco parâmetros posicionais no corpo: **ID do convite, data (dd/mm/aaaa), horário (HH:MM), função e justificativa**. Por exemplo: `Convite excepcional #{{1}} para {{2}} às {{3}}, função {{4}}. Justificativa: {{5}}. Escolha ACEITAR ou RECUSAR.` Configure dois botões de resposta rápida, nessa ordem: **ACEITAR** (índice 0) e **RECUSAR** (índice 1). O worker preenche os payloads com `ACEITAR <id>` e `RECUSAR <id>`; o webhook aceita as respostas de template e de botões interativos. A configuração local não solicita nem presume aprovação: rejeições da Meta são registradas como falha e notificadas à coordenação.

`WHATSAPP_NOTIFICATION_TEMPLATE` é um template aprovado separado, com **um parâmetro de texto no corpo**, para os avisos ao coordenador (por exemplo, `Atualização da escala: {{1}}`). Ele carrega os dados do convite e o resultado: pendente, bloqueado, falhou, incerto, aceito ou recusado. Sem esse template e fora da janela, o aviso fica registrado como `blocked_window`, disponível para consulta em **Gerenciar exceções → LISTAR**. O template de convite nunca é reutilizado para avisos administrativos.

O convite e sua saída são gravados na mesma transação. O consentimento é verificado ao enfileirar e antes de enviar. `INICIAR` ativa somente o próprio voluntário e libera seus convites ainda pendentes que nunca foram tentados; `PARAR` impede o envio de convites enfileirados. Usuários novos, reais ou fictícios, continuam com consentimento desativado por padrão. Mensagens enviadas, entregues ou lidas não confirmam inscrições: apenas o aceite explícito passa pelas validações do motor.

Cada convite tem uma única saída, e eventos e avisos possuem deduplicação. O worker confirma `sending` no banco antes de chamar a Meta. Falhas de rede ou respostas ambíguas ficam `unknown`, sem reenvio automático; ao reiniciar **a única instância do worker**, tentativas interrompidas também ficam incertas e geram aviso ao coordenador. Isso evita duplicidade caso a Meta tenha aceitado a mensagem antes de uma interrupção. Somente rejeições HTTP 429 têm até três tentativas; falhas definitivas não são repetidas automaticamente.

Aplique a migração `a27b6c1d4e90` com `alembic upgrade head`. Ela acrescenta o vínculo único com o convite e a chave única de aviso à outbox, preservando as linhas existentes. Mensagens antigas não são reenviadas nem associadas automaticamente a convites anteriores.

Referências oficiais da Meta: [coleção Cloud API](https://www.postman.com/meta/whatsapp-business-platform/overview), [envio de mensagens](https://www.postman.com/meta/whatsapp-business-platform/folder/o48mro7/messages), [botões](https://www.postman.com/meta/whatsapp-business-platform/request/x0kd1at/send-reply-button), [listas](https://www.postman.com/meta/whatsapp-business-platform/request/0v36tbh/send-reply-to-list-message), [templates](https://www.postman.com/meta/whatsapp-business-platform/request/lwtlz1k/send-message-template-interactive) e [webhooks](https://www.postman.com/meta/whatsapp-business-platform/folder/lboq68h/webhooks). Confirme limites e elegibilidade nessas páginas antes de ativar o número.
