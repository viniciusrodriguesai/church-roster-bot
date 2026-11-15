# Execução da Fase 4.3 — preparação HostHatch

## Estado operacional em 15/11/2026

**Preparação implementada e ensaiada localmente. A aplicação ainda não foi
instalada/publicada na VPS. A fase operacional permanece incompleta.**

O proprietário escolheu não aplicar o endurecimento SSH neste momento
(resposta: “Ainda não aplicar”). Essa decisão não autoriza instalação de
Docker, exposição de portas ou publicação. Nenhuma alteração de configuração,
pacotes, serviços, firewall, banco ou volume foi realizada na VPS nesta execução.
O acesso SSH foi usado somente para inspeções/validações e a sessão root
temporária de recuperação foi encerrada.

## Inspeção do destino

Inspeção via OpenSSH existente, chave Ed25519 no agente do Windows, sem abrir
ou copiar chave privada. A autenticação de `rosteradmin` foi verificada em
conexões independentes. Root também aceita a chave existente; foi utilizado
apenas para leituras privilegiadas. `sudo -n` pede senha para rosteradmin;
`sudo -l -U rosteradmin` confirmou sua permissão administrativa.

| Item | Evidência |
| --- | --- |
| Sistema | Ubuntu 24.04.5 LTS, x86_64 |
| RAM | 3915 MiB totais, aproximadamente 3500 MiB disponíveis; sem swap |
| Disco | Aproximadamente 18 GiB de filesystem, 2,1 GiB usados e 15 GiB livres |
| Portas TCP públicas | Somente SSH 22, IPv4 e IPv6 |
| UFW | Ativo; entrada negada por padrão; somente 22/tcp permitido |
| SSH | Root permitido; chave habilitada; senha e keyboard-interactive desativados |
| Override do provedor | `01-hosthatch.conf` permite root; novo drop-in deverá precedê-lo |
| Docker/containerd | Não instalados; nenhum volume/contêiner para a aplicação identificado |
| Destino da aplicação | `/opt/church-roster-bot` e `/srv/church-roster-bot` ausentes |
| Atualizações automáticas | unattended-upgrades ativo e habilitado |
| Horário/reboot | NTP sincronizado; UTC; nenhum reboot obrigatório indicado |
| Serviços falhos | Nenhum |
| Recuperação | Console HostHatch aberto; login rosteradmin e `sudo -v` confirmados pelo proprietário |

`sshd -t` passou. O drop-in proposto também foi validado via stdin com
`sshd -t -f /dev/stdin`, sem gravar configuração. A política resultante de
`sshd -T` mostra root bloqueado, autenticação por chave preservada, senha e
keyboard-interactive desativados, MaxAuthTries 3 e keepalive 60/3.

## Implementação preparada

- Dockerfile x86_64 compatível, Python 3.12, UID 10001, dependências runtime
  fixadas e imagens-base/PostgreSQL/Caddy fixadas por digest.
- Compose separado de produção: PostgreSQL 17 com TLS, papéis distintos,
  volume persistente, API e worker únicos, Caddy, job explícito de migração,
  limites de memória/CPU/PIDs, filesystem somente leitura para aplicação,
  logs limitados e redes internas sem saída para API/worker.
- Desligamento do worker termina o ciclo em andamento; heartbeat detecta
  paralisação. Transporte Meta desligado explicitamente no Compose, mesmo
  se credenciais forem introduzidas por engano.
- Preparação de segredos/CA recusa diretórios existentes e não imprime valores.
- Scripts revisáveis de instalação Docker e SSH com bloqueio por aprovação;
  SSH inclui rollback de cinco minutos. Nenhum deles foi aplicado na VPS.
- Backup restic cifrado e timers systemd preparados. Destino escolhido pelo
  proprietário: Google Drive existente, via rclone e escopo drive.file.
  OAuth, quota, envio, recuperação da senha e ativação continuam pendentes.
- Verificações de saúde, espaço, heartbeat, expiração TLS e idade do backup
  preparadas, sem habilitar serviços nem enviar alertas.

## Validação executada

1. Testes direcionados de transporte desligado, parada do worker, recuperação
   e política do banco: 35 passaram, 18 PostgreSQL ficaram para ambientes próprios.
2. Regressão SQLite: falha real de limpeza no Windows corrigida fechando
   explicitamente a conexão de backup; os nove testes da origem passaram.
3. Bateria completa Windows após correções: **457 passaram, 372 ignorados**,
   um aviso de depreciação Starlette/httpx. Ignorados incluem PostgreSQL e
   ensaio Docker optativo; não representam validação desses ambientes.
4. CI de `337bc6f`: **827 testes passaram**, mais **30 após importação**;
   migrações, concorrência, backup/restauração e verificações de segurança
   aprovados. [Execução](https://github.com/viniciusrodriguesai/church-roster-bot/actions/runs/43765961766).
   Essa execução precede o commit do Compose; não certifica sozinha o deploy.
5. Imagem Linux da aplicação construída, pip check aprovado; Compose renderizado
   com `config --quiet`, scripts validados com bash -n, Caddy validado pelo binário.
6. Ensaio optativo do stack: **1 teste de integração passou em 127 segundos**.
   Projeto Docker exclusivo e descartável, sem portas publicadas ou dados reais:
   geração de credenciais/TLS, PostgreSQL 17, Alembic, oito voluntários fictícios,
   runtime sem CREATE no esquema, conexão SSL efetiva, API e heartbeat saudáveis,
   HTTPS com validação de CA/hostname, dump custom restaurado em banco novo,
   oito voluntários restaurados e persistência após restart de banco/API/worker.
   O ensaio removeu somente seus próprios recursos de nomes aleatórios.
7. Ruff, formatação e diff sem erros; Bandit sem achados. pip-audit no ambiente
   local e nas dependências runtime fixadas sem vulnerabilidades conhecidas.
   A auditoria inicial detectou opentelemetry-api 1.45.1; ambiente e lock usam
   a versão corrigida 1.45.2. Pacote local não publicado é ignorado pelo PyPI audit.

O novo job `production-stack` repete build, auditoria runtime e ensaio na CI.
A conclusão deve considerar a CI do commit publicado, não somente a execução
anterior. Dependências fixadas exigem atualização/auditoria periódica; não houve
varredura de vulnerabilidades dos pacotes do sistema operacional da imagem.

## Pendências para operar 24 horas

1. Autorizar/aplicar endurecimento SSH com console e rollback; atualmente adiado.
2. Autorizar instalação Docker (inicia daemon e altera rede de contêineres),
   revisar versões/espaço e verificar SSH/UFW após a instalação.
3. Definir endereço HTTPS e autorizar ensaio permanente com dados fictícios;
   inicialmente em loopback, sem abrir firewall e sem Meta.
4. Autorizar OAuth do Drive, verificar quota, inicializar repositório cifrado,
   guardar recuperação fora da VPS e restaurar backup externo de verdade.
5. Autorizar monitoramento/agendamento e política de retenção; escolher canal
   de alerta. Aprovar separadamente qualquer exposição de 80/443.

Não foram usados números reais, chamadas de envio Meta, serviços contratados,
mudanças de faturamento, PRs, merges ou alterações na main. A infraestrutura
proposta depende apenas da VPS existente e do espaço já disponível no Drive;
nenhuma garantia de quota ou de preço futuro é inferida.
