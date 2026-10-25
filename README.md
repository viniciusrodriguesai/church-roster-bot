# Church Roster Bot

Bot open source para escalas de voluntários pelo WhatsApp. **Estado: Fase 2 implementada no código e no simulador; nenhum número real da Meta foi conectado ou testado.** O Diaconato é o primeiro ministério. Não há painel web.

## O que funciona

- Conversas persistentes para inscrições, consulta de escala e funções, indisponibilidade, participações, pedidos de substituição e aceite ou recusa de terceiro dia.
- Conversas de coordenador para cadastrar e editar diáconos, configurar cultos e vagas, abrir e fechar inscrições, consultar vagas e publicar versões da escala.
- Permissões por ministério e administrador geral. Alterações relevantes exigem confirmação e são auditadas.
- Motor da Fase 1 reutilizado: dois dias distintos por mês, exceção justificada com aceite explícito, capacidade, permissões, indisponibilidade e conflitos.
- Webhook Meta com verificação, assinatura HMAC, eventos persistentes e deduplicados; worker separado; caixa de saída com status de entrega; cliente oficial Graph API para texto, listas, botões e templates.
- Simulador de terminal com oito voluntários fictícios e PostgreSQL ou SQLite local.

**Pendente da Fase 3:** distribuição automática, lista de espera, busca automática de substitutos, lembretes e relatórios avançados. O número oficial, os templates aprovados e a implantação contínua precisam de configuração externa. Veja [Arquitetura](docs/architecture.md) e [Fluxos](docs/conversation-flows.md).

## Testar sem número da Meta e sem Docker

No PowerShell, na pasta do projeto:

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -e '.[dev]'
$env:DATABASE_URL='sqlite+pysqlite:///demo.sqlite3'
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\python.exe -m church_roster_bot.scripts.seed_demo
.venv\Scripts\python.exe -m church_roster_bot.scripts.seed_simulation
.venv\Scripts\python.exe -m church_roster_bot.scripts.simulate --phone +15550100001
```

Digite `Oi`, depois os IDs exibidos no menu. O coordenador fictício usa `+15550100000`; o administrador geral fictício usa `+15550109999`; os oito voluntários usam `+15550100001` até `+15550100008`. Esses números são **dados sintéticos exclusivamente para a simulação**. Execute o simulador em outro terminal com `--phone +15550100000` para administrar cultos. Respostas de botões e listas podem ser digitadas pelo ID mostrado. A função `simulate(..., reply_kind="button_reply" | "list_reply")` também gera esses eventos em testes.

SQLite serve apenas para a simulação local rápida; o PostgreSQL é o banco de produção e do teste de concorrência. A CI exercita a migração e os testes no PostgreSQL.

Para testar convites excepcionais, envie `INICIAR` no terminal do voluntário fictício e inscreva-o em dois dias distintos do mês. No terminal do coordenador, escolha `7`, digite `EXCECAO <id_voluntario> <id_vaga>`, informe a justificativa e confirme com `SIM`. O terminal do voluntário mostra o convite automaticamente enquanto aguarda texto. Responda `ACEITAR <id>` ou `RECUSAR <id>`; o coordenador recebe o resultado automaticamente. `7` → `LISTAR` mostra o estado dos envios pendentes, bloqueados ou com falha.

Use um banco exclusivo de simulação. Os terminais executam o worker com transporte local e templates fictícios, sem chamar a Meta; não execute o worker real nesse mesmo banco. As mensagens são entregues mesmo sem novas entradas no terminal. O simulador não altera o consentimento padrão: cada voluntário precisa enviar `INICIAR` e pode desativar com `PARAR`.

## PostgreSQL e servidor

Configure a senha local em `.env` e no `DATABASE_URL` antes de usar o Compose. O arquivo `.env.example` contém somente exemplos. Com Docker Desktop ativo:

```powershell
Copy-Item .env.example .env
docker compose up -d db
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\python.exe -m church_roster_bot.scripts.seed_demo
.venv\Scripts\uvicorn.exe church_roster_bot.app:app --host 0.0.0.0 --port 8000
```

Em outro processo, após configurar a Meta, execute `python -m church_roster_bot.notifications.worker`. O webhook responde após gravar o evento no banco; o worker processa o fluxo e envia a resposta. Mantenha **uma instância do worker** nesta fase. O endpoint `/health` verifica somente o processo HTTP. Para iniciar uma igreja real, o operador confiável cria o primeiro administrador com `python -m church_roster_bot.scripts.bootstrap_admin --church-id <id>`; o script pede o número localmente. A partir daí, a gestão ocorre no WhatsApp.

## Verificação

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format --check .
```

`TEST_DATABASE_URL` ativa o teste de concorrência. Use **um banco PostgreSQL descartável**, pois esse teste cria e remove tabelas. Veja [Testes da Fase 2](docs/phase-2-testing.md).

## Meta e privacidade

As instruções estão em [Configuração do WhatsApp](docs/whatsapp-setup.md). O webhook exige `META_APP_SECRET`; respostas livres só saem dentro da janela de atendimento de 24 horas. Convites proativos exigem consentimento e, fora dessa janela, um template aprovado configurado. Sem template, a mensagem fica registrada como bloqueada. `PARAR` desativa convites; `INICIAR` reativa.

Não publique `.env`, tokens, números reais ou dados de membros. Use apenas a Cloud API oficial. Esta versão não envia mensagens a grupos comuns nem contrata serviços. Antes de uso real, configure backup, retenção de dados e hospedagem permanente. Licença [MIT](LICENSE); contribuições em [CONTRIBUTING.md](CONTRIBUTING.md).
