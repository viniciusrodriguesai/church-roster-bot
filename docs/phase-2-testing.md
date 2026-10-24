# Testar a Fase 2

## Sem Meta e sem Docker

Use os comandos da seção de simulação no [README](../README.md). `seed_demo` cria oito pessoas e telefones sintéticos; `seed_simulation` cria cultos fictícios do mês seguinte às quartas, sábados e domingos, com duas funções e uma vaga por função. A simulação não usa dados reais nem faz chamadas de rede. Para percorrer a inscrição: abra o simulador como `+15550100001`, envie `Oi`, `1`, ID do culto, ID da função e `SIM`. Para administração, abra outro terminal com `+15550100000`.

## Testes automatizados

`python -m pytest -q` cobre regras da Fase 1, inscrição por conversa, permissões, cancelamento, expiração, substituição, exceção, versões, webhook assinado e duplicado, parsing interativo e cliente Meta com mocks. `ruff check .` e `ruff format --check .` são gates da CI. Nenhum teste manda mensagem real.

`TEST_DATABASE_URL=postgresql+psycopg://...` ativa o teste de concorrência real. **Use banco descartável**, pois o teste cria e remove tabelas. A GitHub Actions provisiona PostgreSQL, executa `alembic upgrade head` e a suíte completa. Sem esse banco, somente o teste de concorrência é ignorado localmente.

## Pontos ainda não testados externamente

A Meta não foi conectada a um número real. O comportamento em uma conta específica, a aprovação de templates, a entrega fora da simulação, custos e limites da conta exigem teste posterior autorizado. Antes de produção, exercite backup, recuperação, retenção de dados e monitoramento do worker.
