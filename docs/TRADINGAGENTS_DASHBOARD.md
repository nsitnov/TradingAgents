# TradingAgents Dashboard, Paper Portfolio и Daily Automation

Този документ описва какво е добавено върху оригиналния TradingAgents проект, как работи системата и как се управлява operationally.

Не записвай API ключове, пароли или други secrets в този файл. Secrets са в локални `.env` файлове.

## Какво е инсталирано

Проектът е клониран и инсталиран в:

```bash
/home/nsitnov/tradingagents
```

Python средата е локална:

```bash
/home/nsitnov/tradingagents/.venv
```

Основният public dashboard е достъпен през Cloudflare:

```text
https://tradeagents.sitnov.work/
```

Dashboard-ът е зад Basic Auth. Данните за login са в локалния env файл, не в repo-то:

```bash
/home/nsitnov/.config/tradingagents-dashboard.env
```

## Основни компоненти

### FastAPI + React Dashboard

Backend:

```bash
tradingagents/dashboard/app.py
tradingagents/dashboard/monitor.py
tradingagents/dashboard/ledger.py
tradingagents/dashboard/storage.py
tradingagents/dashboard/automation.py
tradingagents/dashboard/costs.py
```

Frontend:

```bash
tradingagents/dashboard/static/index.html
```

Dashboard-ът е single FastAPI process, който сервира:

- API routes под `/api/...`
- static React UI от `/`
- Server-Sent Events за live run monitoring

### Systemd Services

Dashboard service:

```bash
tradingagents-dashboard.service
```

Инсталиран като user service:

```bash
/home/nsitnov/.config/systemd/user/tradingagents-dashboard.service
```

Слуша само локално:

```text
127.0.0.1:8790
```

Cloudflare tunnel service:

```bash
tradeagents-cloudflared.service
```

Инсталиран като:

```bash
/home/nsitnov/.config/systemd/user/tradeagents-cloudflared.service
```

Използва отделен tunnel config:

```bash
/home/nsitnov/.cloudflared/config-tradeagents.yml
```

Daily automation timer:

```bash
tradingagents-daily.timer
tradingagents-daily.service
```

Инсталирани като:

```bash
/home/nsitnov/.config/systemd/user/tradingagents-daily.timer
/home/nsitnov/.config/systemd/user/tradingagents-daily.service
```

Schedule:

```text
Mon..Fri 09:00:00 Europe/Sofia
```

Weekly portfolio report timer:

```bash
tradingagents-weekly-report.timer
tradingagents-weekly-report.service
```

Инсталират се като user units:

```bash
/home/nsitnov/.config/systemd/user/tradingagents-weekly-report.timer
/home/nsitnov/.config/systemd/user/tradingagents-weekly-report.service
```

Schedule:

```text
Sun 09:00:00 Europe/Sofia
```

## Как работи live monitoring

Когато стартираш analysis от dashboard-а:

1. UI прави `POST /api/runs`.
2. Backend създава `RunRecord`.
3. TradingAgents LangGraph workflow се стартира в background thread.
4. Dashboard-ът се закача към `GET /api/runs/{run_id}/events`.
5. Backend stream-ва live събития през Server-Sent Events.

Основни event типове:

- `run_started`
- `message`
- `tool_call`
- `agent_status`
- `report_section`
- `stats`
- `decision`
- `position_update`
- `pnl_update`
- `run_completed`
- `run_error`

Системата пази:

- agent messages
- tool calls
- report sections
- final decision
- token/tool stats
- resulting paper trade
- portfolio snapshot

Има глобален file lock:

```bash
~/.tradingagents/dashboard/run.lock
```

Той пречи manual run от dashboard-а и daily automation run да вървят едновременно.

## Paper Portfolio

Paper Portfolio е симулиран ledger. Не се свързва с реален broker и не прави реални сделки.

Ledger файл:

```bash
~/.tradingagents/dashboard/ledger.json
```

SQLite history:

```bash
~/.tradingagents/dashboard/dashboard.sqlite3
```

Текущата sizing логика:

| Decision | Paper action |
| --- | --- |
| `Buy` | купува с 20% от наличния cash |
| `Overweight` | купува с 10% от наличния cash |
| `Hold` | не прави сделка |
| `Underweight` | продава 50% от текущата позиция |
| `Sell` | продава 100% от текущата позиция |

Цената се взима от `yfinance` като последна налична close цена.

Dashboard tab `Paper Portfolio` показва:

- cash
- equity
- market value
- realized P&L
- unrealized P&L
- total P&L
- total return
- win rate
- profit factor
- average closed trade P&L
- Sharpe-like score over stored equity snapshots
- equity curve
- max drawdown
- позиции по тикер
- trade ledger

## Paper OMS и hard risk gates

Agent decision вече не пише директно trade в ledger-а. Backend-ът създава `OrderIntent`, минава през deterministic risk gates и чак след това Paper OMS изпълнява paper order-а.

Това пази invariant-а: **няма live broker path в текущата версия**.

Поддържани trading modes:

```bash
TRADINGAGENTS_TRADING_MODE=PAPER          # default: risk gate + auto paper execution
TRADINGAGENTS_TRADING_MODE=DEMO           # risk gate + log only, без ledger mutation
TRADINGAGENTS_TRADING_MODE=LIVE_DISABLED  # execution blocked; използва се като safety stop
```

Risk env променливи:

```bash
TRADINGAGENTS_REQUIRE_ORDER_APPROVAL=false
TRADINGAGENTS_MAX_POSITION_PCT=0.25
TRADINGAGENTS_MAX_TRADE_NOTIONAL=25000
TRADINGAGENTS_MAX_DAILY_TRADES=20
TRADINGAGENTS_DAILY_LOSS_LIMIT=5000
TRADINGAGENTS_FORBIDDEN_TICKERS=""
```

Broker env променливи:

```bash
TRADINGAGENTS_BROKER=paper_ledger     # default local PaperLedger execution
# TRADINGAGENTS_BROKER=alpaca_paper   # read/test adapter only; not wired into execution
ALPACA_API_KEY_ID=...
ALPACA_API_SECRET_KEY=...
ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

Засега execution path-ът е заключен към local PaperLedger. Alpaca adapter-ът може да чете paper positions/orders за бъдеща интеграция, но OMS не изпраща broker orders.

SQLite вече пази:

- orders
- fills
- risk decisions
- audit events

Основни API endpoints:

```text
GET /api/orders
GET /api/orders/fills
POST /api/orders/{order_id}/approve
POST /api/orders/{order_id}/reject
GET /api/risk/config
GET /api/risk/decisions
GET /api/audit/events
GET /api/broker/config
GET /api/broker/positions
GET /api/broker/orders
```

## Daily Automation

Automation config:

```bash
~/.tradingagents/dashboard/automation.json
```

Default config:

```json
{
  "enabled": true,
  "watchlist": ["AAPL", "MSFT", "NVDA", "QQQ", "SPY"],
  "include_positions": true,
  "weekdays_only": true,
  "daily_openai_budget_usd": 5.0,
  "require_openai_admin_key": true,
  "run_request": {
    "analysts": ["market", "social", "news", "fundamentals"],
    "research_depth": 1,
    "llm_provider": "openai",
    "shallow_thinker": "gpt-5.4-mini",
    "deep_thinker": "gpt-5.4",
    "output_language": "English"
  }
}
```

Как избира тикери всяка сутрин:

1. Взима тикерите от `watchlist`.
2. Добавя всички текущи open positions от Paper Portfolio.
3. Маха duplicates.
4. Стартира анализите един по един.
5. Преди всеки тикер проверява OpenAI daily spend.
6. Ако daily spend е над лимита, не стартира следващи run-ове.

Важно: automation е paper-only. Няма реални broker orders.

## OpenAI Spend Tracking

OpenAI spend tracking се прави през official Costs API.

Admin key е в:

```bash
/home/nsitnov/.config/tradingagents-dashboard.env
```

Променлива:

```bash
OPENAI_ADMIN_KEY=...
```

Dashboard tab `OpenAI Spend` показва:

- дали key е configured
- official cost rows
- total за заредения период
- project/line item, когато OpenAI ги връща

Ако Costs API не работи, daily automation не трябва да харчи автоматично. Guardrail-ът ще маркира job-а като skipped, вместо да пуска run-ове без бюджетен контрол.

## Weekly Portfolio Email Report

Всяка неделя в 09:00 локално време systemd timer-ът изпраща седмичен report за Paper Portfolio през Resend.

Report-ът включва:

- start equity и end equity за седмицата
- net P&L и процентна промяна
- gross gains и gross losses по equity curve snapshots
- realized и unrealized P&L промяна
- cash, market value и текущи позиции
- брой buy/sell/hold paper trades и notional за buys/sells

Env променливи в `/home/nsitnov/.config/tradingagents-dashboard.env`:

```bash
RESEND_API_KEY=...
TRADINGAGENTS_REPORT_FROM="TradingAgents <reports@your-verified-domain.com>"
TRADINGAGENTS_REPORT_TO="you@example.com"
TRADINGAGENTS_REPORT_TIMEZONE=Europe/Sofia
TRADINGAGENTS_REPORT_SUBJECT_PREFIX="TradingAgents weekly portfolio report"
```

`TRADINGAGENTS_REPORT_TO` може да съдържа няколко имейла, разделени със запетая.

Ръчен dry run без изпращане:

```bash
cd /home/nsitnov/tradingagents
/home/nsitnov/tradingagents/.venv/bin/python -m tradingagents.dashboard.weekly_report --dry-run
```

Ръчно изпращане:

```bash
systemctl --user start tradingagents-weekly-report.service
```

## Къде се пазят анализите

Пълните анализи се пазят като markdown/json файлове:

```bash
~/.tradingagents/dashboard/analyses/YYYY-MM-DD/TICKER/RUN_ID/
```

Вътре има:

- `run.json`
- `complete_report.md`
- отделни `.md` файлове за report sections

SQLite пази query-friendly history за dashboard-а:

- runs
- run events
- analysis sections
- trades
- portfolio snapshots
- daily jobs
- OpenAI costs

## Основни API endpoints

Live runs:

```text
POST /api/runs
GET /api/runs
GET /api/runs/{run_id}
GET /api/runs/{run_id}/events
POST /api/runs/{run_id}/cancel
```

Portfolio:

```text
GET /api/portfolio
GET /api/portfolio/history
GET /api/portfolio/trades
GET /api/portfolio/performance
```

Orders / Risk / Audit:

```text
GET /api/orders
GET /api/orders/fills
POST /api/orders/{order_id}/approve
POST /api/orders/{order_id}/reject
GET /api/risk/config
GET /api/risk/decisions
GET /api/audit/events
GET /api/broker/config
GET /api/broker/positions
GET /api/broker/orders
```

Automation:

```text
GET /api/automation/config
PUT /api/automation/config
POST /api/automation/run-now
GET /api/automation/history
```

Costs:

```text
GET /api/costs/openai
```

Health:

```text
GET /api/health
```

`/api/health` е public за service/tunnel checks. Останалите routes са зад Basic Auth.

## Operational команди

Проверка на dashboard:

```bash
systemctl --user status tradingagents-dashboard.service --no-pager
curl -sS http://127.0.0.1:8790/api/health
```

Проверка на Cloudflare tunnel:

```bash
systemctl --user status tradeagents-cloudflared.service --no-pager
cloudflared tunnel info d36abb89-b2e9-400f-afc4-642cd40aee4d
```

Проверка на daily timer:

```bash
systemctl --user status tradingagents-daily.timer --no-pager
systemctl --user list-timers --all
```

Проверка на weekly report timer:

```bash
systemctl --user status tradingagents-weekly-report.timer --no-pager
systemctl --user list-timers --all
```

Ръчно пускане на daily automation:

```bash
systemctl --user start tradingagents-daily.service
```

Логове:

```bash
journalctl --user -u tradingagents-dashboard.service -n 100 --no-pager
journalctl --user -u tradingagents-daily.service -n 100 --no-pager
journalctl --user -u tradingagents-weekly-report.service -n 100 --no-pager
journalctl --user -u tradeagents-cloudflared.service -n 100 --no-pager
```

Рестарт на dashboard:

```bash
systemctl --user restart tradingagents-dashboard.service
```

Спиране на daily automation timer:

```bash
systemctl --user disable --now tradingagents-daily.timer
```

## Тестове

Пусни всички тестове:

```bash
cd /home/nsitnov/tradingagents
uv run --with pytest pytest
```

Последна проверка след имплементацията:

```text
122 passed
```

## Какво не прави системата

- Не прави реални сделки.
- Не се свързва с broker.
- Не гарантира печалба.
- Не трябва да се ползва като финансов съвет.
- Не трябва да се оставя без OpenAI spend guardrail, ако automation е включена.

## Важни файлове

Repo файлове:

```bash
tradingagents/dashboard/
deploy/
tests/test_dashboard_*.py
docs/TRADINGAGENTS_DASHBOARD.md
```

Runtime state:

```bash
~/.tradingagents/dashboard/ledger.json
~/.tradingagents/dashboard/dashboard.sqlite3
~/.tradingagents/dashboard/automation.json
~/.tradingagents/dashboard/analyses/
```

Secrets:

```bash
/home/nsitnov/.config/tradingagents-dashboard.env
/home/nsitnov/tradingagents/.env
```

Secrets не трябва да се commit-ват.
