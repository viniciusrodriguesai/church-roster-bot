# Testar a Fase 2

## Sem Meta e sem Docker

Use os comandos da seção de simulação no [README](../README.md). `seed_demo` cria oito pessoas e telefones sintéticos; `seed_simulation` cria cultos fictícios do mês seguinte às quartas, sábados e domingos, com duas funções e uma vaga por função. A simulação não usa dados reais nem faz chamadas de rede. Para percorrer a inscrição: abra o simulador como `+15550100001`, envie `Oi`, `1`, ID do culto, ID da função e `SIM`. Para administração, abra outro terminal com `+15550100000`.

## Testes automatizados

`python -m pytest -q` cobre regras da Fase 1, inscrição por conversa, permissões, cancelamento, expiração, substituição, exceção, versões, webhook assinado e duplicado, parsing interativo e cliente Meta com mocks. `ruff check .` e `ruff format --check .` são gates da CI. Nenhum teste manda mensagem real.

`TEST_DATABASE_URL=postgresql+psycopg://...` ativa o teste de concorrência real. **Use banco descartável**, pois o teste cria e remove tabelas. A GitHub Actions provisiona PostgreSQL, executa `alembic upgrade head` e a suíte completa. Sem esse banco, somente o teste de concorrência é ignorado localmente.

O fluxo de convites excepcionais tem testes de transação e envio sem nova mensagem do voluntário, conteúdo e botões, janela de 24 horas, parâmetros de template, ausência de consentimento e `INICIAR`/`PARAR`, falhas HTTP e de rede, interrupção do worker, deduplicação e notificações de aceite/recusa após a sessão expirar. Esses mesmos cenários rodam em SQLite e PostgreSQL quando `TEST_DATABASE_URL` está definido. Um teste do terminal mantém a leitura de texto bloqueada e verifica a chegada automática de um convite iniciado pelo coordenador. Todos os transportes usados nos testes são locais.

## Regressão: menus repetidos após o aceite

A inspeção somente de leitura do banco de demonstração confirmou o convite #1 `accepted` e a inscrição #3 `confirmed`, ambos com `volunteer_id=5` e `slot_id=5`. Os 76 eventos estavam `processed`, cada um com uma tentativa; as 79 mensagens estavam `sent`, também com uma tentativa. Havia apenas um aviso `invitation:1:aceito`. Após o aceite, os eventos 49–63 e 65–76 continham linhas de uma transcrição colada: comando PowerShell, instruções do simulador, prompts, cabeçalho administrativo e opções de menu. Cada linha era anteriormente aceita como nova mensagem do voluntário, cujo roteador respondia com o menu. Não havia evidência de reprocessamento ou reenvio nesse incidente.

O teste `test_two_terminals_accept_once_and_ignore_pasted_transcript` executa dois terminais e seus workers simultaneamente, confirma o convite pelo coordenador, aceita pelo voluntário e reproduz as linhas coladas. Verifica que a transcrição não cria eventos, saídas ou apresentações, que ciclos ociosos não mudam recibos e que uma nova tentativa explícita de aceitar não duplica a inscrição nem o aviso. Roda em SQLite em arquivo e PostgreSQL. `test_concurrent_acceptance_claims_are_idempotent` disputa um evento entre dois workers e também processa duas respostas de aceite simultâneas: cada evento termina com uma tentativa e há uma única inscrição e um único envio do aviso de aceite. Os testes não usam nem alteram o banco de demonstração.

## Pontos ainda não testados externamente

A Meta não foi conectada a um número real. O comportamento em uma conta específica, a aprovação de templates, a entrega fora da simulação, custos e limites da conta exigem teste posterior autorizado. Antes de produção, exercite backup, recuperação, retenção de dados e monitoramento do worker.
