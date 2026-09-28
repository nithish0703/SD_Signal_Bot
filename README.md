# S&D Signal Bot (Binance Futures → Telegram)

"3 Step A+ Supply & Demand" strategy-a vechu, Binance Futures-la list aana, **24h volume adhigama irukkira top 50 coins**-a scan panni **Telegram-ku signal mattum** anuppum.
**Auto trade illa.** Neenga chart paathu manual-a trade pannanum. GitHub Actions-la **free-a** run aagum, VPS thevai illa.

---

## Bot enna pannum

Ovvoru 5 nimishathukkum, volume-la top USDT coins-la **Binance Futures-la trade aagura top 50**-a automatic-a edukkum (stablecoins, UP/DOWN tokens, Futures-la illadha coins skip). PEPE maadhiri coins Futures-la `1000PEPEUSDT`-nu irundha, signal-la adha kaatum. Apram **1h chart + 4h trend**-la check pannum (backtest-la 1h, 15m-a vida better; fees paadhippu kammi):

1. **Step 1:** Base candle-ku apram 3+ big candles impulse (≥ 2 ATR) + Fair Value Gap → Demand zone (LONG) / Supply zone (SHORT)
2. **Step 2:** Trend confirm: 15m EMA50 correct direction-la pogudhu + 1h price EMA50-ku correct side-la
3. **Step 3:** Price zone-a first time tap pannanum, slow momentum, zone-ku veliya close aagakoodaadhu, apram confirmation candle
4. **6 keys score:** Fresh zone, Close/wick, Confluence (EMA / old S-R), Lowest demand, Discount (<50% fib), Break of structure

Score **5/6 (A) or 6/6 (A+)** vandhaa mattum signal varum. Signal-la: Entry, Stop Loss, **exit plan (trailing stop 3×ATR)**, simple option TP 2R.

### Trade tracking (pudhusu)
Signal anuppina apram, bot adha track pannum:
- 🔁 **SL update:** Price correct direction-la poi SL move panna vendiya neram vandhaa, "move SL to X"-nu message varum.
- 🏁 **Exit:** Trailing stop hit aanadhum, result R-la varum. Simple 2R plan-la evvalavu vandhirukkum-nu-um kaatum.
- 📒 **Paper results:** Dhinamum kaalai 9 mani message-la, last 30 days + all-time results (fees kazhichu) varum.
  Real money podradhukku munnaadi, idha 1–2 maasam paathu backtest-oda match aagudhaa-nu confirm pannunga.
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
4. **mode: account** run pannu: $100 account, $5 × 10x per trade, last 30 days-la evvalavu aagirukkum-nu kaatum (numbers maathalaam).
5. Apram **mode: backtest** run pannu (30 coins-ku 3–5 nimisham aagum). Kadandha ~30 naal-la strategy eppadi perform pannuchu-nu result Telegram-kum Actions summary-kum varum.

Adhukkapram automatic-a ovvoru 5 min-kum scan aagum. Onnum panna vendaam.

---

## Settings maatha (code edit panna vendaam)

GitHub repo → **Settings → Secrets and variables → Actions → Variables tab → New repository variable**.
Variable add panninaa adutha run-la irundhu apply aagum. Delete panninaa default-ku thirumbum.

| Variable | Default | Meaning |
|---|---|---|
| `TOP_N` | `50` | Futures-la list aana top evvalavu coins scan pannanum |
| `SYMBOLS` | (auto) | `BTCUSDT,ETHUSDT` maadhiri kudutha, indha coins mattum dhaan |
| `MIN_SCORE` | `5` | `6` = A+ mattum (kammi signals, strong), `4` = adhiga signals |
| `MIN_SL_PCT` | `1` | SL idha vida close-a irundha trade skip (fees profit-a saapidum) |
| `FEE_PCT` | `0.10` | Round-trip fee %. Market order = `0.10`, limit order = `0.04` |
| `TIMEFRAME` | `1h` | Entry timeframe (`15m`, `1h`, `4h`) |
| `HTF` | `4h` | Trend timeframe (`4h`, `1d`) |
| `TRAIL_ATR` | `3` | Trailing stop distance = ATR × idhu |
| `MARGIN_USD` | `5,8` | Neenga podra margin options ($, Isolated). Ovvonnukkum leverage kaatum |
| `RISK_USD` | `1` | SL hit aana max loss ($). Unga total account-la ~1% vechukkonga |
| `MAX_LEVERAGE` | `20` | Idha thaandi leverage suggest pannaadhu |

### Choppy / sideways market filters
| Variable | Default | Meaning |
|---|---|---|
| `FILTER_HTF_ADX` | `0` (off) | e.g. `20`: 4h ADX 20-ku keezha (trend illa) irundha signal skip |
| `FILTER_ADX` | `0` (off) | Adhe, entry timeframe (1h) ADX-ku |
| `FILTER_CHOP` | `0` (off) | e.g. `50`: Choppiness Index 50-ku mela (sideways) irundha skip |
| `FILTER_SLOPE` | `0` (off) | e.g. `1`: EMA50 20 candles-la 1 ATR alavu move aagala-na skip |

**Backtest** run panna, "Market filters" table-la ovvoru filter-um mudhal paadhi, rendaam paadhi, last 30 days moonulayum
evvalavu-nu kaatum. ✅ vandha filter mattum on pannunga (Telegram-la endha variable set pannanum-nu-um solli kudukkum).
Ellaa period-layum positive-a illadha filter-a on panna vendaam: adhu luck-a irukkalaam.

### Position size & leverage
Ovvoru signal-lum bot calculate pannum: `Leverage = RISK_USD ÷ (MARGIN_USD × SL%)`.
Example: SL 2% → $5 margin-ku **10x**, $8 margin-ku **6x**. Rendulayum position ~$50, SL hit aana loss **~$1**.
Liquidation eppavume SL-a vida kammiyaa 2 madangu dhooram irukkura maadhiri leverage cap aagum.
Binance-la **Isolated** margin select panni, bot sonna leverage-a set pannunga.

Code-la mattum irukkira settings (`bot.py` mela): `IMPULSE_ATR_MULT`, `ZONE_MAX_AGE`, `FALLBACK_SYMBOLS`.

### Fees pathi

Ovvoru signal-lum `💸 Fees ~0.16R` maadhiri kaatum: andha trade-la fees unga risk-la evvalavu-nu.
Backtest ippo fees-a kazhichu **net result** kaatum, market order vs limit order rendaiyum compare panni.
Actions → backtest run → summary-la 8 exit methods (fixed R, ATR TP, ATR trailing) compare aagum. Telegram-la top 3 exits varum.

---

## Theriyanja vendiya limitations (honest-a)

- **GitHub timing:** Every 5 min-nu sonnaalum GitHub sila samayam 5–20 min late-a run pannum, rare-a skip-um pannum. Adhanaala signal konjam late-a varalaam. Stale signal (SL/TP1 already hit aanadhu) bot anuppaadhu. Innum accurate timing venumnaa, free **cron-job.org** use panni GitHub API moolama workflow-a trigger pannalaam.
- **Data source:** GitHub servers US-la irukku, anga Binance Futures API block. So bot adhe pair-oda **Binance Spot** price-a use pannum. Futures price kitta thatta same dhaan, aana konjam difference irukkalaam. Message-la endha source-nu kaatum. Futures API block-naala, coin Futures-la irukkaa-nu Binance-oda public data site (data.binance.vision) moolama check pannum. Adhuvum fail aanaa, check illama spot list use pannum (log-la theriyum). PEPE, SHIB maadhiri coins Futures-la `1000PEPEUSDT` maadhiri vera name-la, 1000x price-la irukkum; signal-la ⚠️ note varum.
- **60-day rule:** Repo-la 60 naal activity illana GitHub schedule-a off pannidum. Bot ovvoru signal/heartbeat-kum `state.json` commit pannum, so idhu normally prachanai illa. Off aanaa Actions tab-la enable pannunga.
- **Backtest:** Fees include aagum, aana slippage, funding include illa. Past result future-a guarantee pannaadhu.

---

## Trade pannumbodhu

- Signal vandhaa, Binance-la chart open panni zone-a neengale confirm pannunga.
- Oru trade-ku unga capital-la **1–2%-ku mela risk** pannaadheenga. SL kandippa podunga.
- High leverage use pannaadheenga. Futures-la liquidation risk romba adhigam.
- Mudhalla 2–3 vaaram signals-a demo / paper trade-la track pannunga.

_Idhu educational tool mattum. Financial advice illa._
