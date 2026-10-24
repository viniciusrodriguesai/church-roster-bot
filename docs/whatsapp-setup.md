# Configurar a WhatsApp Cloud API oficial

**Não há número real conectado a este repositório.** A configuração da Meta depende da conta e da aprovação do usuário. Não use número pessoal se o objetivo é um número exclusivo do bot.

1. Crie ou use uma aplicação Meta com o produto WhatsApp Business Platform e um número elegível para a Cloud API. A Meta pode exigir dados e verificação da organização; não preencha dados fictícios da igreja.
2. Obtenha `WHATSAPP_PHONE_NUMBER_ID`, um token com permissão de mensagens, `META_APP_SECRET` e um `WHATSAPP_VERIFY_TOKEN` escolhido por você. Guarde-os somente nas variáveis de ambiente ou gerenciador de segredos da hospedagem. Defina `WHATSAPP_API_VERSION` para uma versão Graph API suportada pela conta.
3. Disponibilize `https://<seu-domínio>/webhooks/whatsapp` por HTTPS. A verificação GET devolve o `hub.challenge` somente quando o token confere. Inscreva o campo `messages` da conta WhatsApp para receber mensagens e status.
4. Configure templates aprovados para convites iniciados pelo bot fora da janela de 24 horas. `WHATSAPP_INVITE_TEMPLATE` é opcional durante a simulação; sem ele convites proativos fora da janela ficam bloqueados. Configure `WHATSAPP_TEMPLATE_LANGUAGE` de acordo com a aprovação.
5. Aplique `alembic upgrade head`, execute FastAPI e um worker persistente (`python -m church_roster_bot.notifications.worker`). Teste com números autorizados antes de operar uma escala real. Não há suporte para publicar diretamente em grupos comuns.

O webhook usa os bytes originais e `X-Hub-Signature-256`. Rejeita assinatura inválida, payload fora do formato e tamanho excessivo. A caixa de saída registra falhas sem gravar token nos logs. O servidor precisa de conectividade com `graph.facebook.com`.

Referências oficiais da Meta: [coleção Cloud API](https://www.postman.com/meta/whatsapp-business-platform/overview), [envio de mensagens](https://www.postman.com/meta/whatsapp-business-platform/folder/o48mro7/messages), [botões](https://www.postman.com/meta/whatsapp-business-platform/request/x0kd1at/send-reply-button), [listas](https://www.postman.com/meta/whatsapp-business-platform/request/0v36tbh/send-reply-to-list-message), [templates](https://www.postman.com/meta/whatsapp-business-platform/request/lwtlz1k/send-message-template-interactive) e [webhooks](https://www.postman.com/meta/whatsapp-business-platform/folder/lboq68h/webhooks). Confirme limites e elegibilidade nessas páginas antes de ativar o número.
