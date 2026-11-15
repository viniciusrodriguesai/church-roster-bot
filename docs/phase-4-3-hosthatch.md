# Fase 4.3 — HostHatch x86_64

## Situação e autorização

Destino informado: Ubuntu 24.04 LTS amd64, 2 vCPUs compartilhadas, 4 GB RAM,
20 GB NVMe. Branch `feature/phase-4-production`. Sem dependências da Oracle,
ARM, serviços contratados ou alterações de faturamento.

Esta documentação e os arquivos em `deploy/` são preparação revisável. Sua
existência não significa implantação ou backup externo ativo. Consultar o
relatório de execução antes de aprovar o corte. Não usar números reais, tokens
Meta ou dados de membros no ensaio. O Compose mantém o transporte desligado,
credenciais Meta vazias e API/worker em rede interna sem saída para a internet.

## Arquitetura

- Imagem Python 3.12, usuário 10001 sem privilégios, sistema de arquivos somente
  leitura, `/tmp` limitado e capabilities removidas. FastAPI com um processo.
- Worker separado, único por nome fixo de contêiner, parada por SIGTERM ao final
  do ciclo. Nunca escalar esse serviço; recuperação de `sending` pressupõe exclusividade.
- PostgreSQL 17, volume persistente próprio, sem publicação da porta 5432.
  Rede privada, TLS com CA própria e `sslmode=verify-full` usando nome `db`.
  Credenciais distintas para superusuário, migração e runtime; runtime não recebe
  DDL, propriedade, criação de papéis/bancos ou bypass de integridade.
- Caddy mantém certificados em volumes próprios, permite somente health e
  webhook e não registra queries de verificação. Começa vinculado a 127.0.0.1.
  Um hostname autorizado e DNS correto são o caminho habitual para HTTPS
  público. Um IP/hostname local pode usar CA interna, que não é automaticamente
  confiável no computador do usuário ou para integração Meta.
- Limites somados: aproximadamente 2,5 GiB para os serviços contínuos; migração
  adiciona 384 MiB temporários. Pools de duas conexões por processo, sem overflow;
  PostgreSQL limitado a 30 conexões. Sondagens e manutenção também contam.
- Logs Docker `local`: 3 arquivos de 10 MB por serviço. Não guardar payloads,
  tokens ou URLs de conexão em logs. Sem Docker socket nos contêineres.

## Inspeção obrigatória antes de mudanças na VPS

Usar a chave já configurada pelo cliente OpenSSH, sem abrir/copiar material
privado. Executar `deploy/audit-host.sh` via stdin do SSH, sem instalar arquivos.
Inspecionar espaço/inodes, portas IPv4/IPv6, política efetiva SSH (incluindo
`Match`), UFW, sudo sem interação, Docker/volumes existentes, sincronização de
horário, serviços falhos e necessidade de reboot. Não ler `.env`, ambientes de
contêineres, chaves privadas ou logs com dados pessoais.

Antes de instalar Docker, revisar conflitos com pacotes existentes, diretório
de dados e espaço. Usar repositório APT oficial com assinatura conferida;
não usar `curl | sh`, não substituir daemon.json existente e não adicionar
`rosteradmin` ao grupo docker (equivalente a root). Operar Compose via sudo.
A instalação pode iniciar docker/containerd e alterar regras de rede: obter
autorização específica depois da inspeção. Nenhum reboot automático.

## SSH: plano de mudança crítica, ainda não aplicado

1. Confirmar duas conexões independentes como `rosteradmin`, sudo funcional e
   console de recuperação HostHatch disponível. Acesso ao painel sozinho não
   comprova que o console de recuperação funciona; confirmar com o proprietário.
2. Ler somente a configuração SSH, revisar includes e precedência. Preparar um
   drop-in antecipado com `PermitRootLogin no`, `PasswordAuthentication no`,
   `KbdInteractiveAuthentication no`, `PubkeyAuthentication yes`,
   `MaxAuthTries 3`, `ClientAliveInterval 60`, `ClientAliveCountMax 3`.
   Não trocar porta, chaves, usuários ou firewall nessa operação.
3. Apresentar diff e pedir autorização expressa. Fazer backup root-only da
   configuração e agendar rollback automático em poucos minutos com systemd.
   Rollback remove apenas o novo drop-in, valida e recarrega SSH.
4. Validar `sshd -t` e `sshd -T -C` para administrador e root. Usar reload de SSH
   somente após autorização; manter ambas as sessões existentes abertas.
5. Abrir uma terceira conexão e verificar sudo e root bloqueado. Cancelar o
   rollback apenas após sucesso. Se falhar, aguardar rollback ou usar console.

## Preparação de uma instalação nova (pendente de autorização)

Separar checkout imutável em `/opt/church-roster-bot`, root como proprietário;
configuração em `/etc/church-roster` com permissão 0700. Não sobrescrever diretório
ou volume preexistente. A configuração Compose contém apenas RELEASE_TAG,
SITE_ADDRESS e BIND_ADDRESS; senhas ficam fora do Git, em arquivos exclusivos.
Nunca imprimir `docker compose config` com segredos resolvidos; usar `--quiet`.

`deploy/prepare-secrets.sh` exige root e recusa material preexistente, inclusive
execuções parciais. Gera senhas aleatórias e CA/certificado do PostgreSQL sem
exibir chaves. A chave do servidor é root:999 0640 (imagem Debian do PostgreSQL).
Verificar o GID real ao atualizar a imagem. Montar somente CA pública no app.
Certificado do banco vence em 397 dias: alertar 30 dias antes, preparar nova
assinatura com o mesmo SAN e aprovar reload/rotação antes do vencimento.
Guardar senha do repositório restic e recuperação da CA fora da VPS.

Após autorização, construir imagem com tag do commit e registrar digest,
executar auditoria da imagem e validar Compose. Primeiro iniciar apenas banco;
migrar com `--profile maintenance run --rm migrate`, verificar Alembic head e
privilégios. Seeds fictícios somente em banco novo autorizado, por job de
manutenção com a credencial migradora; nunca rodar seeds em banco preexistente.
Iniciar API/worker e testar readiness, restart, parada e persistência antes de
expor Caddy. Não executar testes destrutivos no banco do ensaio permanente.

## Firewall e publicação

Docker pode contornar regras UFW para portas publicadas. A proteção principal
é não publicar banco/API e manter Caddy em loopback até autorização. Revisar
IPv6, regras de encaminhamento Docker e firewall do provedor antes de mudar
BIND_ADDRESS. Não desativar iptables do Docker. Abrir 80/443 e expor Caddy são
mudanças críticas separadas, com autorização e plano de retorno a loopback.
Não abrir 8000/5432/2019. Certificado HTTPS não autoriza conectar a Meta.

## Backup externo sem contratar serviço

O proprietário escolheu usar seu Google Drive existente. A conexão, a quota,
a pasta exclusiva e a autorização de envio ainda precisam ser validadas.
Uma cópia no mesmo disco/volume não protege contra perda da VPS.
Não assumir que os US$ 6 incluem snapshots ou armazenamento adicional.

Preparados `deploy/backup.sh` e unidades systemd, sem habilitar timer. O job
exige repositório restic externo previamente inicializado e autorizado;
configuração root-only em `/etc/church-roster/backup.env` contém
RESTIC_REPOSITORY e RESTIC_PASSWORD_FILE, mais autenticação do backend via
arquivo/ambiente protegido. Nunca inserir senha na linha de comando.
Para Drive, preparar remote rclone `roster-drive` com escopo `drive.file`
(somente arquivos criados/abertos pelo aplicativo) e repositório
`rclone:roster-drive:church-roster-backups`. A autenticação usa navegador do
proprietário e túnel SSH de loopback na porta 53682; rclone recebe e salva OAuth
diretamente, sem copiar tokens para o chat ou imprimir a configuração.
Não abrir 53682 no firewall, executar `rclone config show` ou guardar tokens no
Git. Usar `RCLONE_CONFIG=/etc/church-roster/rclone.conf` root-only e arquivo
de senha restic separado. O OAuth é uma autorização posterior e explícita.
Consultar [configuração remota rclone](https://rclone.org/remote_setup/),
[escopos Drive](https://rclone.org/drive/) e
[backend restic/rclone](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html#other-services-via-rclone).
Instalar restic de fonte confiável, criar diretórios de staging/cache e validar
acesso ao destino antes de habilitar qualquer agendamento. Credencial deve
restringir o escopo ao diretório/bucket do backup; não usar credencial pessoal
ampla sem revisar permissões.

O dump custom usa cliente 17 dentro do contêiner, exclui ownership/ACLs e é
validado com pg_restore --list; restic cifra no cliente. O job também guarda
configuração e credenciais de recuperação, verifica o repositório e marca a
última conclusão. Mantém staging em caso de falha; apaga somente o dump criado
nessa execução após sucesso externo. Precisa de pelo menos 2 GiB livres e
espaço adicional suficiente para o tamanho do banco. Não ativa prune/forget.
Política proposta para revisão: 7 diários e 4 semanais, limitada pela quota do
destino; remoção somente após autorização explícita e teste de recuperação.

Antes de considerar backups operacionais, restaurar o último dump em outro
banco PostgreSQL 17 descartável, reaplicar propriedade/grants por papel,
validar Alembic, fingerprints/sequences e cadastro fictício. Executar o ensaio
fora da VPS pequena quando possível. Nunca usar `--clean` sobre dados existentes.
RPO proposto 24 horas; RTO só pode ser informado após medir restauração.

## Espaço, operação e recuperação

20 GB exigem monitorar `df`, uso de volumes e `docker system df`. Avisar abaixo
de 4 GiB livres; impedir atualização/build abaixo de 2 GiB ou se não houver
margem para a nova imagem e rollback. Conservar a imagem anterior identificada.
Não usar `docker system prune`, `volume prune`, `compose down -v`, truncar WAL,
apagar backups antigos ou remover volumes sem análise/autorização.

`restart: unless-stopped` recupera processos após falhas/reboot com Docker
habilitado; healthcheck sozinho não reinicia contêiner unhealthy. Monitorar
readiness local, estado e progresso do worker, idade do backup, expiração TLS,
RAM/disco e serviços falhos. Alertas externos continuam pendentes até escolher
um canal autorizado. Uma VPS única não fornece alta disponibilidade.

Rollback de código conserva banco e volumes, volta à imagem anterior e exige
compatibilidade das migrações. Não executar downgrade automático. Migração
futura: dump consistente e cifrado, registro de versões/digests, configurações
e CA, restauração isolada e conferência antes de trocar DNS. Nunca executar
dois workers contra o mesmo banco durante a mudança.

Fontes oficiais: [Docker no Ubuntu](https://docs.docker.com/engine/install/ubuntu/),
[Docker e firewall](https://docs.docker.com/engine/network/packet-filtering-firewalls/),
[TLS PostgreSQL 17](https://www.postgresql.org/docs/17/ssl-tcp.html),
[HTTPS Caddy](https://caddyserver.com/docs/automatic-https).
