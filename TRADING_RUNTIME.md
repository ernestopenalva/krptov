# Monitor e Position multichain

O runtime novo e independente da inferencia social. O Ranker mantem uma unica
classificacao de mercado e publica cada chain somente nas watchlists habilitadas
em `chain_routing`. Cada frente consome somente a sua watchlist.

## Comandos

Windows local:

```bat
.\.venv\Scripts\python.exe -m src.modules.scheduler run
.\.venv\Scripts\python.exe -m src.modules.scheduler status
.\.venv\Scripts\python.exe -m src.modules.scheduler drain
.\.venv\Scripts\python.exe -m src.modules.scheduler stop_now
```

VPS Linux/Ubuntu:

```bash
source .venv/bin/activate
python -m src.modules.scheduler run
python -m src.modules.scheduler status
python -m src.modules.scheduler drain
python -m src.modules.scheduler stop_now
```

Em um tmux dedicado, o Scheduler tambem pode ser iniciado pelo mesmo padrao dos
demais processos:

```bash
./scripts/run_scheduler.sh
```

- `drain`: nao inicia novos Monitors. Monitors em andamento ainda podem abrir
  Positions; o runtime encerra quando todos terminarem.
- `stop_now`: cancela Monitors e Positions imediatamente. Positions canceladas
  ficam registradas como abortadas, sem PnL final inventado.
- `status`: mostra modo, capacidade, Monitors/tokens ativos, ultimo tick,
  cooldowns, FIFO social e Positions ativas.

## Configuracao

Os limites e tempos ficam em `config/config.yaml`, nas secoes `monitor`,
`position` e `market_data`. `chain_routing` habilita inferencia, Monitor e o
encaminhamento de alertas sociais por chain; `social_signal_routing` escolhe
Telegram e/ou Monitor para cada tipo de sinal. Valores iniciais: cinco Monitors simultaneos, no
maximo dois sociais dentro desses cinco, tres tentativas, 15 minutos por
tentativa e cooldown de 15 minutos.

Position usa somente ticks on-chain. Os endpoints HTTP, a chave da Alchemy
Prices e os enderecos StateView V4 sao lidos do `.env`; os nomes esperados
estao em `.env.example`.

O Pool Scanner permanece exclusivamente EVM. Solana e descoberta pelo
`token_scanner_solana`, via Dexscreener, somente para tokens Pump.fun ja
graduados em pools PumpSwap com quote SOL/WSOL. O scanner preserva as metricas
Jupiter para observacao, sem usa-las como filtro, e escreve apenas no ranking
buffer. O Ranker continua responsavel pelas duas watchlists.

## Subida conjunta na VPS

Com o KRPTO-V parado, iniciar em cinco tmux separados nesta ordem:

1. `./scripts/run_pool_scanner.sh`
2. `bash scripts/run_token_scanner_solana_loop.sh`
3. `./scripts/run_market_ranker_loop.sh`
4. `./scripts/run_social_inference_loop.sh`
5. `./scripts/run_scheduler.sh`

O Scheduler pode subir por ultimo: os dois scanners e o Ranker primeiro formam
as watchlists; depois a inferencia e o Monitor passam a consumi-las. Cada
chamada do scanner Solana executa um ciclo completo e o runner espera 60
segundos depois do termino antes de iniciar o proximo.

## Arquivos de runtime

- `data/monitor_watchlist.json`: ranking limpo e estado curto do Monitor.
- `data/monitor_campaign_index.json`: impede que campanha encerrada seja
  recriada pelo Ranker.
- `data/monitor/history/`: ticks Dexscreener por Monitor.
- `data/position/live/`: um JSON por Position viva.
- `data/position/history/`: ticks on-chain por Position.
- `data/trading_history.jsonl`: eventos finais de Monitor e Position.
- `data/trading_runtime/status.json`: fotografia operacional consultada por
  `status`.
- `data/token_scanner_solana/state.json`: deduplicacao compacta da descoberta.
- `data/token_scanner_solana/observations_YYYY-MM-DD.jsonl`: perfil, pool e
  observacao Jupiter de cada token emitido.

Em modo paper nao ha reconexao com Positions vivas de uma execucao anterior.
