# Monitor e Position multichain

O runtime novo e independente da inferencia social. O Ranker grava a mesma
classificacao de mercado em `data/watchlist.json` e
`data/monitor_watchlist.json`; cada frente consome somente a sua watchlist.

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

- `drain`: nao inicia novos Monitors. Monitors em andamento ainda podem abrir
  Positions; o runtime encerra quando todos terminarem.
- `stop_now`: cancela Monitors e Positions imediatamente. Positions canceladas
  ficam registradas como abortadas, sem PnL final inventado.
- `status`: mostra modo, capacidade, Monitors/tokens ativos, ultimo tick,
  cooldowns, FIFO social e Positions ativas.

## Configuracao

Os limites e tempos ficam em `config/config.yaml`, nas secoes `monitor`,
`position` e `market_data`. Valores iniciais: cinco Monitors simultaneos, no
maximo dois sociais dentro desses cinco, tres tentativas, 15 minutos por
tentativa e cooldown de 15 minutos.

Position usa somente ticks on-chain. Os endpoints HTTP, a chave da Alchemy
Prices e os enderecos StateView V4 sao lidos do `.env`; os nomes esperados
estao em `.env.example`. O adapter PumpSwap/Solana ja existe, mas somente
recebera candidatos quando Solana for habilitada no Pool Scanner.

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

Em modo paper nao ha reconexao com Positions vivas de uma execucao anterior.
