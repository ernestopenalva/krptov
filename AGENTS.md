# KRPTO-V Orientacoes Para Codex

## Contexto Do Projeto

- O projeto esta migrando o scanner antigo baseado em Dexscreener para um scanner de pools via Alchemy WebSocket.
- O scanner novo fica em `src/modules/pool_scanner.py`.
- A ferramenta diagnostica de pools fica em `src/tools/pool_diagnostics.py`.
- A inferencia social fica em `src/modules/social_inference.py`.
- A watchlist principal fica em `data/watchlist.json`.

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

## Comandos Uteis

### Windows Local

```bat
.\.venv\Scripts\python.exe -m src.modules.pool_scanner --dry-run
.\.venv\Scripts\python.exe src\tools\pool_diagnostics.py --new-session
.\.venv\Scripts\python.exe src\tools\pool_diagnostics.py --report-only
```

### VPS Linux/Ubuntu

```bash
source .venv/bin/activate
python -m src.modules.pool_scanner --dry-run
python src/tools/pool_diagnostics.py --new-session
python src/tools/pool_diagnostics.py --report-only
```

### Validacao

```bash
python -m py_compile src/modules/pool_scanner.py src/tools/pool_diagnostics.py
python -m unittest src.test.test_pool_scanner_simulated src.test.test_pool_diagnostics_simulated -v
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
