# Fluxos de conversa

Todos os passos são gravados em `conversation_sessions`. `MENU` volta ao menu e `CANCELAR` abandona a operação em andamento sem apagar inscrições confirmadas. O estado expira após `CONVERSATION_TTL_MINUTES`; uma resposta antiga recebe um menu novo. IDs de opções podem ser enviados como texto ou recebidos em respostas oficiais de botões/listas.

## Voluntário

`Oi` → menu. `1` → culto aberto → função autorizada → confirmação `SIM` → inscrição. `2` consulta as próprias participações futuras. `3` recebe período `AAAA-MM-DD AAAA-MM-DD`; conflitos com escala existente iniciam pedido de substituição. `4` escolhe uma participação futura e confirma o pedido. `5` conta dias distintos no mês. `FUNCOES` lista permissões. `PARAR` suspende convites e `INICIAR` registra consentimento. `CONVITES` mostra data, função e justificativa dos convites excepcionais, que exigem `ACEITAR <id>` ou `RECUSAR <id>`.

## Coordenador

`MENU` apresenta nove opções. Gerenciar diáconos: `NOVO` pede nome, E.164, IDs das funções e confirmação; `LISTAR` consulta; `EDITAR <id>` recebe `NOME`, `TELEFONE`, `FUNCOES`, `ATIVAR` ou `DESATIVAR` e pede confirmação. Configurar cultos pede data, horário e pares `função_id:vagas`. Configurar vagas pede ID da vaga e nova capacidade; não pode ficar abaixo das inscrições ativas. Inscrições usam `ABRIR AAAA-MM`, `REVISAR AAAA-MM` ou `FECHAR AAAA-MM`. Há consultas de escala, vagas e participantes. Exceção usa `EXCECAO <voluntário_id> <vaga_id>` e justificativa. Substituição manual usa `SUBSTITUIR <pedido_id> <voluntário_id>` e revalida o motor. Publicar gera versão e texto para encaminhar. `ESCALA NOVEMBRO 2026` ou `ESCALA 2026-11` consultam sem publicar.

## Prévia de distribuição (Fase 3)

No menu do coordenador ou administrador com ministério selecionado, envie
`PROPOR AAAA-MM`. O mês precisa ter inscrições abertas. A resposta mostra a
política de distribuição, as sugestões por vaga, a carga e o histórico usados
em cada escolha e as posições sem candidato elegível. Somente cultos futuros
no fuso da igreja entram no cálculo. Repetir o comando calcula uma nova prévia
com o estado atual; não cria reservas, inscrições ou convites.

`PLANEJAR AAAA-MM` salva o cálculo como um rascunho com ID. `VERPROPOSTA <id>`
mostra as escolhas e os motivos originais; `DESCARTAR <id>` pede `SIM` antes de
cancelar, mantendo o histórico. Os comandos exigem autorização atual para o
ministério selecionado. Salvar e revisar rascunhos ainda não envia convites nem
atribui participações.

`APROVAR <id>` → confirmação `SIM` → convites enviados pela outbox. A proposta
inteira é revalidada; se uma vaga ficou indisponível, nenhum convite da aprovação
é salvo. O voluntário responde `ACEITAR A<id>` ou `RECUSAR A<id>` e a coordenação
recebe o resultado automaticamente. O aceite verifica novamente todas as regras;
envio e leitura não confirmam participação. `A<id>` identifica convites normais
da automação e não interfere nas exceções `ACEITAR <id>` da Fase 2.

## Substituição automática

Após registrar indisponibilidade para uma participação ou confirmar um pedido
pelo menu `4`, o worker procura substitutos sem exigir comandos da coordenação.
O candidato recebe `A<id>` e responde `ACEITAR A<id>` ou `RECUSAR A<id>`.
O original permanece na escala até a troca aceita. Recusa ou expiração permite
chamar a próxima pessoa elegível. A coordenação recebe os estados dos convites
e um aviso quando não há candidato; o original recebe aviso após a confirmação.
Esses avisos respeitam consentimento e templates. A troca manual continua
disponível e uma disputa com aceite automático confirma apenas um substituto.

## Lista de espera

O voluntário pode enviar `ESPERA <vaga_id>` para entrar, `MINHAESPERA` para
consultar somente as próprias posições e `SAIR_ESPERA <id>` para sair. A fila
segue a ordem de entrada entre elegíveis com convites ativados por `INICIAR`.
Quando há vaga, chega um convite `A<id>` sem pedir `CONVITES`. Recusa e expiração
permitem chamar o próximo; aceitar confirma somente após revalidar as regras.
Sair e entrar de novo coloca a pessoa no fim da fila. A lista não reserva vagas.

## Abertura mensal automática

No menu do coordenador ou administrador com ministério selecionado, envie
`ABERTURA 25 09:00`, por exemplo, e confirme com `SIM`. O mês seguinte será
aberto no dia 25 às 09:00 do mês anterior, no fuso da igreja, se houver cultos
cadastrados. O worker também recupera aberturas vencidas do mês atual quando
ainda existem cultos futuros; escalas encerradas não são reabertas.
`ABERTURA DESATIVAR` pede confirmação para desabilitar a rotina.

## Emergências

`EMERGENCIAS 6 15` pede confirmação para buscar cobertura nas seis horas antes
do culto, com quinze minutos de resposta por convite. Aceita horizonte de 1 a
72 horas e prazo de 1 a 120 minutos. `EMERGENCIAS DESATIVAR` impede novas ofertas,
também após confirmação; convites já emitidos mantêm sua validade original.
A política começa desativada. A fila elegível tem prioridade e pedidos de
substituição são atendidos pelo culto mais próximo. O voluntário recebe um
convite urgente `A<id>` e precisa aceitar. A coordenação é avisada quando faltam
candidatos ou a cobertura depende de convites pendentes. Não há confirmação
por silêncio nem autorização automática de terceiro dia.

## Lembretes automáticos

No menu da coordenação, `LEMBRETES 1 09:00` pede confirmação para avisar um dia
antes às 09:00, no fuso da igreja. A antecedência aceita 0 a 30 dias; zero permite
um horário no próprio dia, desde que anterior ao culto. `LEMBRETES DESATIVAR`
também exige `SIM`. A política começa desativada. O worker recupera lembretes
vencidos somente enquanto o culto ainda não começou. Cada participação confirmada
recebe no máximo um lembrete; pedidos de substituição não são lembrados como
presença confirmada. Fora da janela de conversa, é necessário o template aprovado
`WHATSAPP_REMINDER_TEMPLATE`; sem consentimento ou template, a entrega fica bloqueada.

## Administrador geral

Para editar data ou horário de um culto, entre em Configurar cultos e envie `EDITAR <culto_id>`, seguido por `DATA AAAA-MM-DD` ou `HORA HH:MM`. A confirmação só é aplicada se o culto não tiver participações ativas nem estiver publicado ou encerrado; isso evita mover compromissos já assumidos.

O número deve constar em `general_admins`. `ADMIN` abre comandos `MINISTERIO <nome>`, `RENOMEAR_MINISTERIO <id> <nome>`, `FUNCAO <ministério_id> <nome>`, `FUSO <zona IANA>`, `COORDENADOR <telefone> <nome> | <ids de ministérios>` e `EDITAR_COORD <id> NOME/TELEFONE/MINISTERIOS/ATIVAR/DESATIVAR`. Todos pedem confirmação. O primeiro administrador é criado localmente por operador confiável; digitar `ADMIN` sem cadastro nunca concede acesso.

## Limites de envio

Respostas a uma mensagem recente usam texto, botão ou lista. Proatividade respeita consentimento e a janela de 24 horas; fora dela, o bot usa somente um template configurado e aprovado. Sem template, grava `blocked_window` e o coordenador pode consultar pedidos quando abrir o menu. O simulador retorna os payloads que seriam enviados sem fazer chamadas externas.
