# Church Roster Bot

Bot open source para escalas de voluntários de igrejas pelo WhatsApp. **Estado atual: Fase 1 (núcleo funcional).** O webhook, os menus, as notificações e a integração com a Meta ainda não estão implementados. Não use esta versão para operar uma escala real.

## O que já funciona

- Cadastro de voluntários com número E.164, funções permitidas e consentimento para convites.
- Ministérios, funções, cultos, horários e quantidade de vagas configuráveis no banco.
- Inscrição transacional com limite de dois dias distintos por mês, autorização registrada para um terceiro dia, verificação de vagas, indisponibilidade e incompatibilidade de funções.
- Relatório preliminar de capacidade para detectar falta evidente de voluntários.
- Migração inicial do PostgreSQL, testes automatizados e endpoint `/health`.

O Diaconato é o primeiro ministério. Um exemplo fictício em `scripts/seed_demo.py` cria funções de abrir, fechar, ofertas e abrir e fechar, além de oito voluntários fictícios. Nenhum horário ou número de vagas é presumido para a igreja real.

## Executar localmente

Requisitos: Python 3.12+ e PostgreSQL 15+. No Windows, substitua `python` pelo executável do ambiente virtual quando necessário.

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e '.[dev]'
cp .env.example .env  # no Windows: Copy-Item .env.example .env
docker compose up -d db
alembic upgrade head
python -m church_roster_bot.scripts.seed_demo
uvicorn church_roster_bot.app:app --host 127.0.0.1 --port 8000
```

O arquivo `.env` deve ter uma senha própria; o valor em `.env.example` serve apenas de exemplo local. Acesse `http://127.0.0.1:8000/health`. A API não possui painel web nem endpoints de administração nesta fase.

## Testes

```bash
pytest -q
ruff check .
ruff format --check .
```

`TEST_DATABASE_URL` habilita o teste de concorrência real no PostgreSQL. Esse banco deve ser exclusivo para testes, pois o teste cria e remove suas tabelas.

## Organização

Veja [Arquitetura](docs/architecture.md) para as regras, decisões de concorrência e limites atuais. As fases seguintes implementarão o fluxo completo por WhatsApp, a Cloud API, notificações persistentes, substituições, lista de espera, auditoria, relatórios completos e implantação contínua.

## Segurança e privacidade

Nunca publique `.env`, tokens, números reais ou dados privados. O banco de demonstração contém somente dados fictícios. A integração futura deverá verificar a assinatura dos webhooks, respeitar consentimento, permitir descadastro e seguir as regras atuais da Meta e da LGPD.

Licença: MIT. Contribuições: [CONTRIBUTING.md](CONTRIBUTING.md).
