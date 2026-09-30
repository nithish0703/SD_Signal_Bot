# FX Signal Bot – USD/JPY London breakout (NSE USDJPY futures)

Signals only – no auto trading. Runs free on GitHub Actions every 15 minutes (weekdays) and sends
Telegram + ntfy phone alerts.

## Strategy
- **Range:** the 08:00–09:00 London hourly candle (≈ 12:30–13:30 IST in summer, 13:30–14:30 IST in winter).
- **Entry:** the first later hourly candle that *closes* above the range → BUY, below → SELL.
  One trade a day, only candles that close by 6:30 PM IST.
- **Stop:** the other side of the range.
- **Exit:** 7:15 PM IST (before the NSE 7:30 PM close). No fixed target.
- Every signal is tracked as a paper trade (`fx_state.json`).

Backtest note: passed a 2-year hourly test (Yahoo data, ~+5 pips/trade after 1 pip cost). Not proven
over the long term – paper trade first. Not financial advice.

## Legal (India)
Trade only exchange-traded contracts through a SEBI-registered broker (NSE/BSE currency derivatives:
USDINR, EURUSD, GBPUSD, USDJPY). Offshore forex/CFD apps are not permitted for Indian residents (FEMA,
RBI Alert List).

## Files
- `fx_bot.py` – strategy + signals + paper tracking
- `notify.py` – Telegram and ntfy helpers
- `.github/workflows/bot.yml` – 15-minute loop on GitHub Actions (`mode: test` sends a status message)

## Secrets (GitHub → Settings → Secrets → Actions)
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `NTFY_TOPIC`
