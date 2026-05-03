# TradingAgents → Production Trading Platform
**Стратегическа пътна карта за nsitnov/TradingAgents**

Дата: 2026-05-03
Базис: `nsitnov/TradingAgents` (fork на TauricResearch v0.2.4)

---

## 0. Редакция на приоритета след repo одит

Този roadmap е реалистичен, но първоначалната версия поставяше real-time инфраструктурата твърде рано. В текущия fork вече има dashboard, SQLite history, paper ledger, daily automation, weekly Resend report и Git upstream sync. Затова правилният първи production slice е:

1. **Paper OMS + deterministic risk gates + audit log**
2. **Paper performance analytics**, за да мерим дали стратегията печели виртуално и дали бие SPY/QQQ
3. **Cross-market scanner MVP без broker trading**
4. **Paper trading на scanner signals**
5. **Broker adapters остават read/test-only до изрично решение за paper account**
6. **Едва тогава Redis/Timescale/NATS и live capital**

Правило: **няма LIVE trading преди hard risk gates, audit trail, paper reconciliation, manual approval mode и поне 1 месец стабилен paper режим.**

## 1. Какво вече имаш (одит на репото)

| Компонент | Статус | Бележки |
|---|---|---|
| LangGraph multi-agent граф | Готов | 4 анализатора, bull/bear debate, trader, risk team, PM |
| Multi-LLM (OpenAI/Anthropic/Google/xAI/DeepSeek/Qwen/GLM/Ollama/Azure) | Готов | Добра diversification |
| Data: yfinance + alpha_vantage | Базово | EOD/delayed; не става за real-time |
| Decision log с alpha vs SPY | Готов | Хубав reflection loop |
| Checkpoint resume (LangGraph) | Готов | Per-ticker SQLite |
| Dashboard | Скелет | Не е production UI |
| Paper Portfolio + weekly email report | Готов | SQLite ledger history + Resend report |
| Daily automation | Готов | Watchlist + positions, OpenAI budget guard |
| Upstream sync | Готов | Fork + weekly upstream PR workflow |
| Docker | Готов | Single-shot run |
| Backtesting | Минимално | Date-fidelity, но няма walk-forward, Monte Carlo, transaction cost модел |
| **Execution / OMS** | **Paper-ready** | Local PaperLedger execution, orders/fills/risk/audit |
| **Real-time streaming** | **Липсва** | Single-shot `propagate()` |
| **Risk gates** (kill switch, DD, exposure) | **Базово готово** | deterministic max notional/position/trades/loss/forbidden tickers |
| **Crypto** | **Липсва** | Само US stocks |
| **Cross-market scanner** | **Липсва** | Това е твоят въпрос — виж секция 6 |

---

## 2. Какво да подобрим веднага (нискъв риск, висок ефект)

### 2.1 Архитектурна модернизация
- **Event-driven loop** вместо single-shot `propagate()`. Tick / news event → агент пайплайн → решение → OMS.
- **Message bus**: Redis Streams (евтино) или NATS JetStream (скейлира). Темите: `ticks.*`, `news.*`, `signals.*`, `orders.*`, `fills.*`.
- **Time-series DB**: TimescaleDB (Postgres extension — лесна интеграция) или QuestDB/ClickHouse за by-ticker storage на tick/bar data.
- **Postgres** за orders, positions, PnL, audit log.
- **Redis** за hot state (последна цена, позиции, лимити).
- **FastAPI** + WebSocket gateway за UI и API клиенти.
- **Observability**: Prometheus + Grafana, structured JSON логване (loguru/structlog), OpenTelemetry tracing.

### 2.2 LLM cost & latency оптимизация
- 3-tier модел: **screener** (small Haiku/gpt-mini) → **analyst** (mid) → **decision** (Opus/GPT-5.4) — не пускай големия модел на 500 тикъра.
- Async parallel execution на анализаторите вътре в LangGraph (вече е възможно — но проверете че е enabled).
- Prompt caching (Anthropic, OpenAI Responses API) за репетитивен context като brand/instrument metadata.
- **Local Ollama** за screening агенти (Llama 3.3 70B върху Mac M-чипове е достатъчно евтино).
- LLM observability: Langfuse или Helicone.

### 2.3 Качество на агентите
- **Структурирани outputs** (вече има за PM/Trader; разшири го към Analysts) с Pydantic модели → елиминира parse errors и позволява guardrails.
- **Self-consistency**: пускай Trader 3 пъти с различни temperatures, медианна позиция.
- **Meta-Critic agent**: чете цялата chain и flag-ва когато bull research игнорира bearish data (или обратното) — намалява confirmation bias.

---

## 3. Execution слой — превръщането в реално приложение

Без това си само анализатор. Това е минималното за live trading:

### 3.1 Broker adapters (абстрактен интерфейс + 2 имплементации)
```
BrokerAdapter (interface)
├── AlpacaBroker      — US equities & options, paper + live
├── IBKRBroker        — Interactive Brokers (multi-asset, global)
├── TradierBroker     — алтернатива за US options
├── BinanceBroker     — crypto spot + futures
├── CoinbaseAdvanced  — crypto US-friendly
└── KrakenBroker      — crypto + EU
```
Започни с **Alpaca (paper)** + **Binance Testnet** — нула риск, истински API.

### 3.2 Order Management System (OMS)
- `Order` модел със state machine: `pending → submitted → partial → filled / cancelled / rejected`
- Idempotency keys (избягва дублирани поръчки при retry)
- Reconciliation worker (всеки 30s сравнява local state с broker)
- **Execution algos**: market, limit, TWAP, VWAP, iceberg, POV
- Slippage & commission модел за backtest точност

### 3.3 Pre-trade risk gates (HARD limits — не LLM)
Това НЕ са LLM решения. Това са deterministic checks преди да изпратиш поръчка:
- Max позиция per ticker (% от portfolio)
- Max сектор exposure
- Max gross + net exposure
- Daily loss limit → kill switch (auto disable)
- Trailing drawdown limit
- Max trades per day (anti-revenge-trading)
- Forbidden tickers list (penny stocks, halted, earnings днес и т.н.)
- Cool-down след loss
- Per-broker margin check

### 3.4 Modes
```
DEMO     — синтетична данни, нула API повиквания към broker
PAPER    — истински market data, paper account
LIVE_S   — live с малки суми (например cap $1k per trade)
LIVE     — full size
```
Mode се сменя само от UI с конфирмация + 2FA. Логва се в audit log.

---

## 4. Risk & Portfolio Management (отвъд "risk team" prompt-овете)

- **Position sizing**: Fractional Kelly (½–¼ Kelly), volatility targeting (target 15% annualized vol), risk parity между стратегии.
- **Stop loss / take profit**: ATR-based trailing stops, time stops (auto-close след X дни).
- **Portfolio метрики live**: VaR (95/99%), CVaR, beta to SPY, correlation matrix, sector heatmap.
- **Regime detection**: HMM или прост vol regime classifier — намалява leverage в high-vol periods.
- **Circuit breakers**: при -5% дневна загуба → halt новите trades; при -10% → flatten + alert.

---

## 5. Backtesting & Validation (research grade)

- **Walk-forward analysis** (rolling train/test windows)
- **Bootstrap & Monte Carlo** на equity curve
- **Out-of-sample holdout** (например последните 20% от данните)
- **Transaction cost модел** (spread + slippage + fees)
- **Survivorship-bias-free universe** (включи делистнати тикъри)
- **Метрики**: Sharpe, Sortino, Calmar, max DD, win rate, profit factor, average R:R, time in market
- **Benchmark comparison**: SPY (stocks), BTC (crypto), 60/40
- **Stress tests**: 2008, 2020-03, 2022 bear, COVID flash crash

---

## 6. Cross-market lead/lag scanner — отговор на твоя въпрос

> *"Може ли системата да върви непрекъснато, да сканира новини от Япония/Европа/Австралия/Близкия Изток и да открива неефективности преди US пазарите да се настроят?"*

**Кратък отговор: ДА, и това е една от най-документираните алфи в литературата.** Известна е като:
- **International Overnight Effect** (Lou, Polk & Skouras 2019)
- **Global Macro Lead-Lag**
- **Overnight Drift** на ETF (SPY/QQQ носят над 100% от историческия return overnight)
- **ADR arbitrage** (cross-listed: Sony, Toyota, ASML, SAP, Novo Nordisk, TSMC, BABA, Tencent ADRs…)

### 6.1 Защо работи
1. Asia/EU отварят преди US. Информация от Tokyo (00:00–06:00 ET), Frankfurt/London (03:00–11:30 ET) се отразява в local equities + ES/NQ futures, но single-name US stocks не се преоценяват до 09:30 ET premarket.
2. **ES/NQ futures** търгуват ~23/5, но bid-ask и liquidity са по-тънки overnight — там стои информацията.
3. Преводът от макро/секторно събитие към конкретен US тикър е бавен — точно тук агентската armия има edge.
4. Емисии на BoJ/ECB/PBoC/BoE/RBA се случват извън US часовете и често не се остойностяват веднага в US-listed peers.

### 6.2 Архитектура на скенера

```
┌─────────────────────────────────────────────────────────────┐
│ 24/7 INGEST WORKERS (по region)                             │
│ • JP: Reuters JP, Nikkei, Bloomberg JP RSS, TSE corp filings│
│ • EU: Reuters EU, FT, Handelsblatt, ECB feed, LSE RNS       │
│ • AU/NZ: ASX announcements, RBA, Sydney Morning Herald      │
│ • ME: Tadawul, ADX, DFM, Saudi Press, energy news (Argus)   │
│ • CN/HK: Cailianpress, HKEx, SSE/SZSE filings               │
│ • Global wire: Reuters, Bloomberg, AP, AFP                  │
│ • Macro: BoJ, ECB, BoE, RBA, PBoC, IMF                      │
└─────────────────┬───────────────────────────────────────────┘
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ TRANSLATION & NORMALIZATION                                 │
│ • Auto-translate (DeepL Pro / Anthropic) → EN               │
│ • Entity extraction (companies, ISINs, tickers, sectors)    │
│ • Event taxonomy (earnings, M&A, regulation, macro, geo)    │
└─────────────────┬───────────────────────────────────────────┘
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ CROSS-MARKET MAPPER                                         │
│ Foreign signal → US-listed counterpart(s)                   │
│ • Direct ADR (Toyota TM ↔ 7203.T)                            │
│ • Sector ETF (Saudi oil news → XLE, XOP)                    │
│ • Supply chain (TSMC TW → AAPL, AMD, NVDA)                  │
│ • Currency (BoJ hike → DXY, FXY, JPY-funded carry trades)   │
│ • Commodity (China steel → CLF, NUE, X)                     │
└─────────────────┬───────────────────────────────────────────┘
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ DISLOCATION DETECTOR                                        │
│ Compare:                                                    │
│ • Foreign-name move (last bar)                              │
│ • ES/NQ futures move (live)                                 │
│ • US single-stock pre-market quote (or yesterday close)     │
│ Z-score the gap vs historical co-movement.                  │
│ If gap > 2σ AND news event detected → SIGNAL.               │
└─────────────────┬───────────────────────────────────────────┘
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ AGENT CONFLUENCE                                            │
│ Multi-agent debate validates signal:                        │
│ • Geopolitical Agent confirms event materiality             │
│ • Macro Agent checks calendar (FOMC blackout? CPI?)         │
│ • Liquidity Agent verifies premarket book depth             │
│ • Risk Agent sizes position vs portfolio constraints        │
└─────────────────┬───────────────────────────────────────────┘
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ PORTFOLIO MANAGER → OMS → BROKER (paper / live)             │
└─────────────────────────────────────────────────────────────┘
```

### 6.3 Конкретни примери на стратегии
1. **ASML overnight gap**: ASML AS (Amsterdam) скача +4% на earnings → US-listed ASML ще отвори с gap; ако premarket е само +2%, → купи ASML преди open, излез на open или intraday.
2. **TSMC → semis**: Тайвански отчет на TSMC пред US open → AVGO, NVDA, AMD често следват. Ако NVDA premarket не reflektira → дълъг.
3. **BoJ изненада**: BoJ вдига rates 02:00 ET → carry trades unwind, JPY поскъпва, S&P futures падат, но Nikkei-exposed US stocks (CAT, DE, MMM) още не са преоценени.
4. **ECB hawkish**: EUR/USD скача → US multinationals с голям EU exposure (KO, PG, MCD) имат FX headwind — short basket.
5. **Saudi/OPEC решение** (петък, US отворен): WTI скача → XLE, XOP, OIH catch-up; US авиокомпании (AAL, DAL, UAL) → short basket.
6. **Hong Kong/China полит. събитие**: KWEB, FXI, BABA, JD, BIDU — често американските ADR-и недореагират overnight.

### 6.4 Какво ще ти трябва (минимален stack)
| Слой | Инструмент | Цена/мес (start) |
|---|---|---|
| News firehose | Benzinga + Tiingo + RSS | $200–500 |
| Premium Asia/EU news | RavenPack или Bloomberg API (по-късно) | $1k+ |
| Translation | DeepL Pro API | $30 |
| Foreign quotes | Tiingo + IEX Cloud + Yahoo intl | $100 |
| US tick/L2 | Polygon.io Stocks Advanced | $200 |
| Crypto | Binance + Coinbase WS (free) | 0 |
| Futures (ES/NQ snapshots) | CQG / Databento / TradeStation | $50–200 |
| Calendar | Trading Economics API | $50 |
| LLM ($/day) | Claude + Haiku mix | $5–20 |

### 6.5 Implementation план (по фази)
- **Фаза 1** (1–2 седмици): RSS/API ingest + DeepL + entity extraction + cross-listed map + log в DB. Без trading. Цел: 100+ опростени signals/ден за наблюдение.
- **Фаза 2** (2–3 седмици): Dislocation detector + back-test срещу 1 година от news. Калибрирай Z-score thresholds.
- **Фаза 3** (2 седмици): Agent confluence + paper trading. Тествай на Alpaca paper.
- **Фаза 4** (1 месец paper): Стартира паралелно с твоя existing analysis loop. Сравнявай PnL, hit rate, slippage.
- **Фаза 5**: Малък live capital (например $5k–$10k) с hard cap $500/trade. Постепенно scale-вай.

---

## 7. Армия от агенти — нов roster

Твоят repo има 7 агента. За пълнокръвна платформа предлагам да разшириш до ~25, групирани в "екипи":

### 7.1 Intelligence екип (continuous, 24/7)
- **News Firehose Agent** — ingest, дедупликация, entity extraction
- **Translation Agent** — multi-language → EN с context
- **Macro Agent** — CPI, PCE, NFP, FOMC, ECB, BoJ календар + interpretation
- **Geopolitical Agent** — wars, sanctions, elections impact mapping
- **Earnings Agent** — pre/post earnings analysis, guidance vs consensus
- **Insider Agent** — Form 4, 13F, 13D/G filings monitoring
- **Whale/On-chain Agent** (crypto) — Glassnode/Nansen integration
- **Social Sentiment Agent** — X, Reddit (WSB), StockTwits, weighted by author credibility

### 7.2 Analysis екип (on-demand per signal)
- Fundamentals, Technical, News, Sentiment (вече имаш)
- **Options Flow Agent** — unusual options activity (UOA), gamma exposure
- **Sector Rotation Agent** — relative strength rankings, RRG
- **Correlation Regime Agent** — alert когато correlations spike (риск-on/off shift)
- **Liquidity Agent** — book depth, spread, VWAP analysis

### 7.3 Decision екип
- Bull / Bear Researchers (имаш)
- **Devil's Advocate Agent** — задължителен contra-thesis
- **Trader** (имаш) — тимингова и размерна препоръка
- **Quantitative Validator** — проверява че signal-ът е статистически значим (не fitting)

### 7.4 Risk & Portfolio екип
- **Hard Risk Gate** (deterministic, не LLM!)
- **Portfolio Manager** (имаш) — final approver
- **Position Sizer** — Kelly/vol-target изчисление
- **Hedger Agent** — auto-suggest hedges при concentration

### 7.5 Operations екип
- **Compliance Agent** — Reg-T margin, PDT rule, wash sale, restricted lists
- **Reconciliation Agent** — broker vs local state diff
- **Postmortem Agent** — за всеки closed trade пише reflection в decision log
- **Meta-Critic Agent** — седмичен audit на цялата система

---

## 8. UI / UX (превръщане в продукт)

- **Web dashboard** (Next.js + shadcn/ui + Tailwind):
  - Live PnL & positions
  - Open signals queue (с "Approve / Reject" бутони за semi-auto mode)
  - Agent reasoning timeline (drill-down към prompt + response)
  - Risk dashboard (exposure heatmap, VaR, DD curve)
  - Backtest playground
- **Mobile alerts**: Telegram bot (най-лесно), Pushover, или native iOS app по-късно
- **Voice alerts** за критични събития (kill switch hit, large fill)
- **Daily morning briefing** (06:00 ET): какво се случи overnight, какви signals има, какво ще се търгува
- **Weekly review**: PnL, alpha attribution, кои агенти добавят/унищожават стойност

---

## 9. ML/RL слой (опционално, късна фаза)

- **Feature store** (Feast) — версионирани features за агенти + RL
- **Online learning**: decision log → fine-tuning датасет → дистилиран модел за screening
- **RL agent за position sizing** (PPO/SAC) — стартира paper-only, проверявай дали бие fractional Kelly baseline
- **Embedding-based news similarity** (за дедупликация и historical analog matching: "това събитие прилича на X от 2019, тогава SPY направи Y")

---

## 10. Compliance & Safety (НЕ Е OPTIONAL за live)

- **Mode flag** записан в DB + audit log; смяна изисква 2FA + ръчна конфирмация
- **Kill switch** — physical bookmark в UI + Telegram команда `/halt`
- **Daily loss limit** auto-shutdown с email alert
- **Forbidden tickers list** (penny stocks, OTC, halted, earnings днес ако такава политика)
- **Audit log immutable** (append-only, ежедневен hash chain или WORM storage)
- **Disclaimers**: ясно бележ, че DEMO/PAPER не е гаранция за LIVE поведение
- **Tax reporting** (US): Form 8949 export, wash sale tracking
- **Личен compliance**: ако започнеш да управляваш чужди пари → SEC RIA или CTA registration (за commodities/crypto futures)

---

## 11. Препоръчан 90-дневен roadmap

### Месец 1 — Foundation
- Седмица 1: Paper OMS, normalized orders/fills, hard risk gates, audit log, manual approval endpoints.
- Седмица 2: Alpaca paper adapter върху същия `BrokerAdapter` interface, reconciliation worker, broker-vs-local diff report.
- Седмица 3: Backtest engine с transaction cost модел; replay 2024-2025 на текущите агенти.
- Седмица 4: Dashboard order/risk/audit UI + Telegram/email alerts for rejected orders and daily loss limit.

### Месец 2 — Cross-market scanner (твоят use case)
- Седмица 5: News firehose + translation + entity extraction
- Седмица 6: Cross-listed mapping + dislocation detector
- Седмица 7: Agent confluence layer + paper trading on signals
- Седмица 8: Калибрация, A/B vs single-shot analyzer

### Месец 3 — Production hardening
- Седмица 9: Redis Streams/TimescaleDB only if SQLite + in-process workers are the bottleneck.
- Седмица 10: Observability (Prometheus/Grafana), error budgets
- Седмица 11: Postmortem agent + weekly review automation
- Седмица 12: Решение за малък live capital само ако paper metrics, reconciliation и risk gates са стабилни.

---

## 12. Конкретни next steps за теб

1. **Веднага днес**: Commit-ни този roadmap в repo-то, защото в момента е жив документ извън Git history.
2. **Тази седмица**: Имплементирай Paper OMS + hard risk gates + audit trail върху текущия dashboard/SQLite stack.
3. **След това**: Мери virtual performance: total return, max DD, win rate, profit factor, average closed trade P&L, alpha vs SPY/QQQ.
4. **По-късно**: Alpaca paper остава read/test adapter; не го включвай в execution, докато не решим да тестваме broker paper account.
5. **Седмица 3**: Започни cross-market ingest — RSS/API + translation + entity extraction, но без trading.

---

## Приложение: Известни pitfalls

- **Lookahead bias** в backtesting — внимавай с timezone alignment (особено за overnight стратегии).
- **Slippage underestimation** — реалният ти slippage на $1M notional ≠ slippage на $10k. Моделирай non-linear impact.
- **News API throttling** — много vendor-и капират при 10–60 req/sec.
- **LLM cost runaway** — 1000 ticker scan × 10 calls × $0.05 = $500/ден. Сложи hard budget cap.
- **"Beautiful demo, broken live"** — paper Alpaca има по-добър fill отколкото live; не вярвай на paper PnL над $50k notional.
- **Earnings windows** — около earnings vol expansion прави предишни stop-loss-и безсмислени; auto-disable агентите 24h преди earnings.
- **Cross-market regime breaks** — корелациите между Asia и US се чупят при macro shocks (COVID, war). Имай regime-detection guard.

---

*Този документ е персонализиран за nsitnov/TradingAgents. Препоръчвам да го третираш като живо growing документ — копирай го в Notion и го итерирай.*
