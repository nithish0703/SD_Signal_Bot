# S&D Signal Bot (Binance Futures → Telegram)

"3 Step A+ Supply & Demand" strategy-a vechu, Binance-la **24h volume adhigama irukkira top 30 coins**-a scan panni **Telegram-ku signal mattum** anuppum.
**Auto trade illa.** Neenga chart paathu manual-a trade pannanum. GitHub Actions-la **free-a** run aagum, VPS thevai illa.

---

## Bot enna pannum

Ovvoru 5 nimishathukkum, andha nerathula volume-la top 30 USDT coins-a automatic-a edukkum (stablecoins, UP/DOWN tokens skip). Apram 15m chart + 1h trend-la check pannum:

1. **Step 1:** Base candle-ku apram 3+ big candles impulse (≥ 2 ATR) + Fair Value Gap → Demand zone (LONG) / Supply zone (SHORT)
2. **Step 2:** Trend confirm: 15m EMA50 correct direction-la pogudhu + 1h price EMA50-ku correct side-la
3. **Step 3:** Price zone-a first time tap pannanum, slow momentum, zone-ku veliya close aagakoodaadhu, apram confirmation candle
4. **6 keys score:** Fresh zone, Close/wick, Confluence (EMA / old S-R), Lowest demand, Discount (<50% fib), Break of structure

Score **5/6 (A) or 6/6 (A+)** vandhaa mattum signal varum: Entry, Stop Loss, TP1 (1R), TP2 (1.5R), TP3.
Daily kaalai 9 mani-ku "bot running" message varum, so bot uyiroda irukkaa-nu theriyum.

---

## Setup (10 nimisham)

### 1. Telegram bot create pannu
1. Telegram-la **@BotFather** open panni `/newbot` anuppu. Name kudu.
2. Adhu kudukkura **token**-a copy pannu (`123456:ABC...` maadhiri irukkum).
3. Unga pudhu bot-a open panni **Start** press pannu (idhu mukkiyam, illana message varaadhu).
4. **@userinfobot**-a open panni Start press pannu. Adhu kaatura **Id** number dhaan unga **chat ID**.

### 2. GitHub repo create pannu
1. github.com-la free account create pannu.
2. **New repository** → name: `sd-signal-bot` → **Public** select pannu → Create.
   > Public yen? Public repo-ku Actions minutes unlimited free. Private-na maasam 2000 min mattum dhaan, 5-min bot-ku adhu podhaadhu. Unga token secret-a dhaan irukkum, yaarum paaka mudiyaadhu.
3. **Add file → Upload files** → indha zip-la irukkira ellaa files-um upload pannu.
   `.github/workflows/bot.yml` file kandippa andha folder path-la irukkanum. Upload-la folder varalana:
   **Add file → Create new file** → name box-la `.github/workflows/bot.yml` type pannu → content paste pannu → Commit.

### 3. Secrets add pannu
Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather kudutha token |
| `TELEGRAM_CHAT_ID` | userinfobot kudutha Id |

(Optional) Adhe page-la **Variables** tab-la:
- `TOP_N` = `50` → top 50 coins scan pannum (default 30)
- `SYMBOLS` = `BTCUSDT,ETHUSDT,SOLUSDT` → auto list-ku badhula indha coins mattum dhaan scan pannum

### 4. Test pannu
1. Repo → **Actions** tab → "I understand... enable" irundha click pannu.
2. **S&D Signal Bot → Run workflow → mode: test** → Run.
3. Telegram-la "S&D bot connected!" vandhaa setup correct.
4. Apram **mode: backtest** run pannu (30 coins-ku 3–5 nimisham aagum). Kadandha ~30 naal-la strategy eppadi perform pannuchu-nu result Telegram-kum Actions summary-kum varum.

Adhukkapram automatic-a ovvoru 5 min-kum scan aagum. Onnum panna vendaam.

---

## Settings maatha (bot.py mela irukku)

| Setting | Default | Meaning |
|---|---|---|
| `TOP_N` | `30` | Volume-la top evvalavu coins scan pannanum |
| `FALLBACK_SYMBOLS` | 30 big coins | Top list fetch aagalana idhu use aagum |
| `TIMEFRAME` | `15m` | Entry timeframe (`5m`, `15m`, `1h`) |
| `HTF` | `1h` | Trend timeframe (`1h`, `4h`) |
| `MIN_SCORE` | `5` | `6` = A+ mattum (kammi signals, strong), `4` = adhiga signals |
| `IMPULSE_ATR_MULT` | `2.0` | Adhigama vechaa strong impulse mattum |

---

## Theriyanja vendiya limitations (honest-a)

- **GitHub timing:** Every 5 min-nu sonnaalum GitHub sila samayam 5–20 min late-a run pannum, rare-a skip-um pannum. Adhanaala signal konjam late-a varalaam. Stale signal (SL/TP1 already hit aanadhu) bot anuppaadhu. Innum accurate timing venumnaa, free **cron-job.org** use panni GitHub API moolama workflow-a trigger pannalaam.
- **Data source:** GitHub servers US-la irukku, anga Binance Futures API block. So bot adhe pair-oda **Binance Spot** price-a use pannum. Futures price kitta thatta same dhaan, aana konjam difference irukkalaam. Message-la endha source-nu kaatum. Top 30 list-um spot volume vechu dhaan varum. Adhanaala list-la vara coin Binance Futures-la illama irukkalaam (rare). Signal vandhaa, andha coin Futures-la irukkaa-nu check pannitu trade pannunga. PEPE, SHIB maadhiri coins Futures-la `1000PEPEUSDT` maadhiri vera name-la irukkum; price-um 1000x-a irukkum.
- **60-day rule:** Repo-la 60 naal activity illana GitHub schedule-a off pannidum. Bot ovvoru signal/heartbeat-kum `state.json` commit pannum, so idhu normally prachanai illa. Off aanaa Actions tab-la enable pannunga.
- **Backtest:** Fees, slippage, funding include illa. Past result future-a guarantee pannaadhu.

---

## Trade pannumbodhu

- Signal vandhaa, Binance-la chart open panni zone-a neengale confirm pannunga.
- Oru trade-ku unga capital-la **1–2%-ku mela risk** pannaadheenga. SL kandippa podunga.
- High leverage use pannaadheenga. Futures-la liquidation risk romba adhigam.
- Mudhalla 2–3 vaaram signals-a demo / paper trade-la track pannunga.

_Idhu educational tool mattum. Financial advice illa._
