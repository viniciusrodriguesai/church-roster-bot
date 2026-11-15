# Execução da Fase 4.3 — preparação HostHatch

## Estado operacional em 15/11/2026

**Ensaio privado implantado e saudável na VPS, somente com dados fictícios.
Backup externo e endurecimento SSH ainda precisam ser concluídos.**

O proprietário escolheu não aplicar o endurecimento SSH neste momento
(resposta: “Ainda não aplicar”). Posteriormente, o proprietário autorizou
explicitamente instalar Docker Engine e Compose. Essa instalação foi executada
e validada, como registrado abaixo; SSH, regras UFW, bancos e dados existentes
foram preservados. A sessão root temporária de recuperação foi encerrada após
validar novo acesso administrativo. Depois da CI aprovada, o proprietário
autorizou o ensaio privado na VPS e a configuração de backups cifrados no Drive.
Exposição pública continua sem autorização.

## Inspeção inicial do destino (antes da instalação autorizada)

Inspeção via OpenSSH existente, chave Ed25519 no agente do Windows, sem abrir
ou copiar chave privada. A autenticação de `rosteradmin` foi verificada em
conexões independentes. Root também aceita a chave existente; inicialmente foi
utilizado apenas para leituras privilegiadas. `sudo -n` pede senha para rosteradmin;
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

## Docker instalado com autorização

O proprietário respondeu “Autorizo instalar Docker e Compose”. Mantida uma
sessão root de recuperação e preservados iptables, ip6tables e `/etc/ufw` em
`/root/roster-docker-recovery.y1nPlUyd` (root-only). O instalador verificou
Ubuntu/arquitetura, ausência de instalação existente, espaço, chave pública do
repositório Docker e assinatura APT; nenhum pacote foi removido ou atualizado.

- Docker Engine/CLI 29.9.3; Compose 5.7.1; containerd 2.4.2;
  Buildx 0.39.0. Serviços Docker e containerd habilitados e ativos.
- Driver de logs local com limite 10 MB × 3 e live-restore configurados antes
  do primeiro início. rosteradmin não foi adicionado ao grupo docker.
- `hello-world` amd64 executado em contêiner descartável sem rede, sem volume,
  com filesystem somente leitura e limites de memória/PIDs.
- Novo SSH como rosteradmin passou; UFW continua somente 22/tcp IPv4/IPv6;
  somente SSH público em `ss -lnt`; nenhum serviço falho.
- Aproximadamente 2,5 GiB usados e 15 GiB livres após instalação. Sem contêineres
  ou volumes de aplicação; imagem de teste hello-world conservada.
- Sem reboot, restart de SSH, abertura de portas ou alteração da política SSH.
  APT indicou que nenhum serviço precisava de reinício.

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
  Docker foi aplicado após autorização; SSH inclui rollback de cinco minutos
  e permanece adiado.
- Backup restic cifrado e timers systemd preparados. Destino escolhido pelo
  proprietário: Google Drive existente, via rclone e escopo drive.file.
  OAuth, quota, envio, recuperação da senha e ativação continuam pendentes.
- Verificações de saúde, espaço, heartbeat, expiração TLS e idade do backup
  preparadas, sem habilitar serviços nem enviar alertas.

## Ensaio privado implantado após CI aprovada

Release `9062092`, arquivo de fonte gerado por git archive e SHA-256 conferido
após transferência, sem `.env`, bancos locais, chaves ou dados não rastreados.
Instalação nova em `/opt/church-roster-bot`, configuração root-only em
`/etc/church-roster`, sem sobrescrever diretório ou volume preexistente.

- Imagem construída na VPS com dependências fixadas; segredos gerados no próprio
  servidor sem exibi-los. TLS do PostgreSQL verificado para o hostname `db`.
- Migrações até `431d9e6a8102`; `alembic check` sem operações pendentes.
- Seeds exclusivamente fictícios: oito voluntários e 13 cultos em dezembro/2026.
- PostgreSQL, FastAPI e worker saudáveis. Caddy responde HTTPS com CA local
  validada; `/health/ready` retorna status ok e `/openapi.json` retorna 404.
- Conexão real do runtime usa papel roster_runtime e SSL; sem CREATE no esquema.
  Transporte Meta desativado e credenciais Meta vazias, confirmados no processo.
- Somente Caddy publica 127.0.0.1:80 e 127.0.0.1:443. Testes TCP do Windows:
  22 acessível; 80, 443, 8000 e 5432 inacessíveis externamente. UFW preservado.
- Aproximadamente 3,7 GiB usados e 14 GiB livres; 3146 MiB de RAM disponíveis.
  Amostra ociosa dos contêineres: Caddy 13 MiB, API 62 MiB, worker 56 MiB e
  PostgreSQL 38 MiB. A amostra não representa teste de carga.

HTTPS é privado e usa CA interna. Não é um endpoint público nem uma conexão
com WhatsApp. Reinício automático dos contêineres e serviços Docker está
configurado; nenhum reboot da VPS foi realizado.

## Preparação do backup externo autorizada

Restic 0.19.1 e rclone 1.76.0 instalados dos projetos oficiais, com checksums
e assinaturas PGP verificados contra fingerprints fixados. Não iniciam daemons
nem abrem portas públicas. Instalador recusa binários preexistentes.

O cliente compartilhado Google do rclone será descontinuado em 2026; foi criado
o projeto exclusivo `Church Roster Backups` para configurar cliente OAuth próprio,
sem vincular faturamento ou criar VM/bucket/serviço pago. Somente a API do Drive
foi habilitada. O formulário de autorização está preenchido e aguarda aceite da
política de dados do Google pelo proprietário; credencial OAuth ainda não foi
criada. OAuth, senha de recuperação, quota e restauração externa permanecem
pendentes. Nenhum arquivo foi enviado ao Drive até este ponto; o timer não foi
habilitado. O proprietário confirmou que deseja concluir o backup no Drive.

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
A [CI de 9062092](https://github.com/viniciusrodriguesai/church-roster-bot/actions/runs/43767867759)
passou: 828 testes, um Docker optativo ignorado no job geral, 30 testes após
importação e um ensaio completo no job production-stack. Dependências fixadas
exigem atualização/auditoria periódica; não houve
varredura de vulnerabilidades dos pacotes do sistema operacional da imagem.

## Pendências para operar 24 horas

1. Autorizar/aplicar endurecimento SSH com console e rollback; atualmente adiado.
2. Instalação Docker concluída; manter acompanhamento de versões e espaço.
3. Ensaio privado autorizado e implantado em loopback; endereço público e
   exposição de portas continuam dependentes de decisão/autorização separada.
4. Autorizar OAuth do Drive, verificar quota, inicializar repositório cifrado,
   guardar recuperação fora da VPS e restaurar backup externo de verdade.
5. Autorizar monitoramento/agendamento e política de retenção; escolher canal
   de alerta. Aprovar separadamente qualquer exposição de 80/443.

Não foram usados números reais, chamadas de envio Meta, serviços contratados,
mudanças de faturamento, PRs, merges ou alterações na main. A infraestrutura
proposta depende apenas da VPS existente e do espaço já disponível no Drive;
nenhuma garantia de quota ou de preço futuro é inferida.
