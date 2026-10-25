# Fluxos de conversa

Todos os passos são gravados em `conversation_sessions`. `MENU` volta ao menu e `CANCELAR` abandona a operação em andamento sem apagar inscrições confirmadas. O estado expira após `CONVERSATION_TTL_MINUTES`; uma resposta antiga recebe um menu novo. IDs de opções podem ser enviados como texto ou recebidos em respostas oficiais de botões/listas.

## Voluntário

`Oi` → menu. `1` → culto aberto → função autorizada → confirmação `SIM` → inscrição. `2` consulta as próprias participações futuras. `3` recebe período `AAAA-MM-DD AAAA-MM-DD`; conflitos com escala existente iniciam pedido de substituição. `4` escolhe uma participação futura e confirma o pedido. `5` conta dias distintos no mês. `FUNCOES` lista permissões. `PARAR` suspende convites e `INICIAR` registra consentimento. `CONVITES` mostra data, função e justificativa dos convites excepcionais, que exigem `ACEITAR <id>` ou `RECUSAR <id>`.

## Coordenador

`MENU` apresenta nove opções. Gerenciar diáconos: `NOVO` pede nome, E.164, IDs das funções e confirmação; `LISTAR` consulta; `EDITAR <id>` recebe `NOME`, `TELEFONE`, `FUNCOES`, `ATIVAR` ou `DESATIVAR` e pede confirmação. Configurar cultos pede data, horário e pares `função_id:vagas`. Configurar vagas pede ID da vaga e nova capacidade; não pode ficar abaixo das inscrições ativas. Inscrições usam `ABRIR AAAA-MM`, `REVISAR AAAA-MM` ou `FECHAR AAAA-MM`. Há consultas de escala, vagas e participantes. Exceção usa `EXCECAO <voluntário_id> <vaga_id>` e justificativa. Substituição manual usa `SUBSTITUIR <pedido_id> <voluntário_id>` e revalida o motor. Publicar gera versão e texto para encaminhar. `ESCALA NOVEMBRO 2026` ou `ESCALA 2026-11` consultam sem publicar.

## Administrador geral

Para editar data ou horário de um culto, entre em Configurar cultos e envie `EDITAR <culto_id>`, seguido por `DATA AAAA-MM-DD` ou `HORA HH:MM`. A confirmação só é aplicada se o culto não tiver participações ativas nem estiver publicado ou encerrado; isso evita mover compromissos já assumidos.

O número deve constar em `general_admins`. `ADMIN` abre comandos `MINISTERIO <nome>`, `RENOMEAR_MINISTERIO <id> <nome>`, `FUNCAO <ministério_id> <nome>`, `FUSO <zona IANA>`, `COORDENADOR <telefone> <nome> | <ids de ministérios>` e `EDITAR_COORD <id> NOME/TELEFONE/MINISTERIOS/ATIVAR/DESATIVAR`. Todos pedem confirmação. O primeiro administrador é criado localmente por operador confiável; digitar `ADMIN` sem cadastro nunca concede acesso.

## Limites de envio

Respostas a uma mensagem recente usam texto, botão ou lista. Proatividade respeita consentimento e a janela de 24 horas; fora dela, o bot usa somente um template configurado e aprovado. Sem template, grava `blocked_window` e o coordenador pode consultar pedidos quando abrir o menu. O simulador retorna os payloads que seriam enviados sem fazer chamadas externas.
