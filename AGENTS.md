# KRPTO-V Orientacoes Para Codex

## Contexto Do Projeto

- O projeto esta migrando o scanner antigo baseado em Dexscreener para um scanner de pools via Alchemy WebSocket.
- O scanner novo fica em `src/modules/pool_scanner.py`.
- O ranqueador de mercado fica em `src/modules/market_ranker.py`.
- A ferramenta diagnostica de pools fica em `src/tools/pool_diagnostics.py`.
- A inferencia social fica em `src/modules/social_inference.py`.
- A watchlist principal fica em `data/watchlist.json`.
- Quando o usuario se referir ao "sistema" do projeto KRPTO-V, entender o conjunto inteiro: pool scanner, ranqueador, inferencia social, monitor, position, envio de alertas via Telegram, logs, dados, JSONs temporarios, watchlist, ferramentas diagnosticas, APIs externas, configuracoes e pipeline.

## Papel Do Codex No Projeto

- O usuario tambem discute ideias com outras IAs, como ChatGPT e Claude, mas o Codex e quem manipula o codigo neste workspace.
- Tratar o Codex como guardiao da integridade e coerencia do sistema inteiro.
- Nao aplicar instrucoes vindas de outra aba/IA de forma mecanica se elas parecerem ignorar contexto, quebrar invariantes, misturar responsabilidades ou criar instabilidade no pipeline.
- Antes de implementar uma ideia externa, confrontar a mudanca com o estado atual do sistema: watchlist, status, social, monitor, position, logs, dados de runtime, ferramentas, APIs externas e contratos entre modulos.
- Se uma instrucao nova conflitar com decisoes ja tomadas ou com a arquitetura atual, apontar o conflito claramente e propor uma adaptacao segura.
- Preferir mudancas pequenas, auditaveis e testaveis, preservando compatibilidade entre os modulos.

## Ambientes De Trabalho

- O desenvolvimento local do usuario acontece em Windows.
- Quando passar comandos para o Windows, preferir formato DOS/cmd quando for simples, porque o usuario tem mais familiaridade com DOS do que com PowerShell.
- PowerShell pode ser usado quando for claramente mais pratico para scripts, inspecoes ou comandos multilinha.
- A VPS de execucao roda Linux/Ubuntu.
- Quando passar comandos para a VPS, usar comandos Bash/Linux.
- Evitar misturar sintaxe Windows e Linux na mesma instrucao. Separar explicitamente "Windows local" e "VPS Linux/Ubuntu".

## Datas E Fusos

- O usuario trabalha no fuso de Brasilia.
- Quando analisar execucoes, logs ou arquivos por data, considerar que muitos arquivos usam UTC no nome e nos timestamps.
- Sempre que houver risco de confusao, mostrar explicitamente UTC e horario de Brasilia.
- Para referencias relativas como "ontem", "hoje" e "de madrugada", confirmar com datas absolutas.

## Regras Importantes

- Nao alterar `social_inference.py`, monitor ou position sem pedido explicito.
- Nao colocar API keys hardcoded no codigo.
- Nao sobrescrever `status`, `social_status`, `monitor_status`, `telegram_alert_sent` ou `discarded_reason` em entradas existentes da watchlist.
- Campo `social_eligibility` e uma trava apenas da inferencia social; nao deve impedir o monitor ou position de consumirem o token quando esses modulos forem adaptados.
- A inferencia social nao deve consultar X para tokens com `social_eligibility = "blocked_old_market"`.
- Preferir `--dry-run` para experimentos de scanner.
- Dados de runtime ficam em `data/` e `logs/`.
- Nao usar `reset_runtime_data.py` sem confirmacao clara do usuario.

## Pool Scanner

- Configuracao das fontes: `config/pool_sources.yaml`.
- Ethereum usa `ALCHEMY_ETH_WSS_URL`.
- Fontes atuais:
  - `ethereum/uniswap_v2`
  - `ethereum/uniswap_v3`
  - `ethereum/sushiswap_v2`
  - `ethereum/uniswap_v4`
- V2, V3 e SushiSwap possuem `pool_address`.
- Uniswap V4 nao possui `pool_address`; usar `pool_id` e `pool_manager_address`.
- Eventos brutos/normalizados do scanner sao gravados em `data/pool_scanner/events_YYYY-MM-DD.jsonl`.

## Ferramenta Diagnostica

- A ferramenta diagnostica e separada do scanner principal.
- Ela mede atraso de indexacao na Dexscreener e snapshots de liquidez, volume e transacoes.
- Snapshots padrao: 5, 15 e 30 minutos.
- V2, V3 e SushiSwap usam associacao exata por `pool_address`.
- V4 usa associacao experimental por token quando a Dexscreener nao expuser um `pool_address`; tratar como `token_level`, menos confiavel.
- Dados diagnosticos ficam em `data/pool_diagnostics/`.

## Ranqueador E Social Eligibility

- O scanner descobre pools novas, nao necessariamente tokens novos.
- O ranqueador consulta a Dexscreener para enriquecer tokens da watchlist com `market_score` e idade de mercado.
- Para evitar gastar X com token velho que apenas criou pool nova, o ranqueador calcula `social_eligibility`.
- Valores esperados:
  - `pending`: Dexscreener ainda nao tem dados suficientes.
  - `eligible`: mercado parece novo o bastante para social.
  - `blocked_old_market`: Dexscreener encontrou par antigo do token; a inferencia social deve pular.
- A regra inicial considera mercado velho quando o menor `pairCreatedAt` retornado pela Dexscreener para o token tem mais de 24 horas.
- Campos auxiliares gravados na WL:
  - `social_eligibility_reason`
  - `social_eligibility_updated_at`
  - `oldest_pair_created_at_utc`
  - `oldest_pair_age_minutes`
  - `selected_pair_created_at_utc`
- Essa decisao e especifica para social; monitor e position podem usar outros criterios no futuro.

## Comandos Uteis

### Windows Local

```bat
.\.venv\Scripts\python.exe -m src.modules.pool_scanner --dry-run
.\.venv\Scripts\python.exe -m src.modules.market_ranker
.\.venv\Scripts\python.exe -m src.modules.social_inference
.\.venv\Scripts\python.exe src\tools\pool_diagnostics.py --new-session
.\.venv\Scripts\python.exe src\tools\pool_diagnostics.py --report-only
.\.venv\Scripts\python.exe src\tools\compare_market_apis.py --date YYYY-MM-DD --limit 100 --sleep-seconds 2 --retries 5
```

### VPS Linux/Ubuntu

```bash
source .venv/bin/activate
python -m src.modules.pool_scanner --dry-run
python -m src.modules.market_ranker
python -m src.modules.social_inference
python src/tools/pool_diagnostics.py --new-session
python src/tools/pool_diagnostics.py --report-only
python src/tools/compare_market_apis.py --date YYYY-MM-DD --limit 100 --sleep-seconds 2 --retries 5
```

### Validacao

```bash
python -m py_compile src/modules/pool_scanner.py src/modules/market_ranker.py src/modules/social_inference.py src/tools/pool_diagnostics.py src/tools/compare_market_apis.py
python -m unittest src.test.test_pool_scanner_simulated src.test.test_pool_diagnostics_simulated src.test.test_compare_market_apis_simulated src.test.test_social_eligibility -v
```

## Analise De Resultados

- Para diagnostico de pools, comecar por:
  - `python src/tools/pool_diagnostics.py --report-only`
- Para auditoria social:
  - `python src/tools/audit_social_system.py --date YYYY-MM-DD --limit 30`
- Para alertas sociais:
  - `python src/tools/print_social_alerts.py --date YYYY-MM-DD --limit 50 --show-watchlist`
- Para posts do X:
  - `python src/tools/print_x_posts.py --limit 30`

## Observacoes De Produto

- Pool criada nao e sinal suficiente.
- Liquidez sustentada aos 15 e 30 minutos e uma medida melhor de intencao real.
- Antes de gastar chamadas de inferencia social, considerar filtros economicos baseados em liquidez, volume e transacoes observados pela ferramenta diagnostica.
