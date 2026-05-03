# TradingAgents Agent Trading Platform

Този документ описва текущата платформа в този fork на
`tauricresearch/tradingagents`: какво представлява, как работи автономно, какво
е paper-only, как се следят LLM разходите и как се поддържа operationally.

Не записвай API keys, пароли, Resend keys, OpenAI keys или други secrets в repo-то.
Secrets стоят локално в:

```bash
/home/nsitnov/.config/tradingagents-dashboard.env
```

## Цел

Платформата е агентна paper-trading система върху оригиналния TradingAgents
multi-agent research flow. Целта не е да заменим TradingAgents с чист quant bot,
а да запазим идеята:

- analysts събират market/news/social/fundamental context;
- bull/bear researchers спорят;
- trader взима решение;
- risk/portfolio слоевете ограничават изпълнението;
- platform wrappers добавят replay, scanner, OMS, audit, reports и automation.

Системата е paper-only по default. Реална broker търговия е изключена, докато
няма доказана paper/backtest alpha, стабилен drawdown профил и отделно ръчно
решение за broker activation.

## Високо Ниво

Основните слоеве са:

| Слой | Статус | Основни файлове |
| --- | --- | --- |
| TradingAgents core graph | Запазен | `tradingagents/graph/*`, `tradingagents/agents/*` |
| Dashboard API/UI | Работи | `tradingagents/dashboard/app.py`, `tradingagents/dashboard/static/index.html` |
| Paper ledger | Работи | `tradingagents/dashboard/ledger.py` |
| Paper OMS + risk gates | Работи | `tradingagents/dashboard/oms.py` |
| Backtest/replay | Работи като platform wrapper | `tradingagents/dashboard/backtest.py`, `tradingagents/dashboard/agent_replay.py` |
| Cross-market scanner | Работи като MVP | `tradingagents/dashboard/scanner.py` |
| Scanner confluence + paper execution | Работи | `scanner_confluence.py`, `scanner_execution.py` |
| Autopilot | Работи | `tradingagents/dashboard/autopilot.py` |
| Daily automation | Работи | `tradingagents/dashboard/automation.py` |
| Weekly portfolio report | Работи | `tradingagents/dashboard/weekly_report.py` |
| OpenAI cost tracking + alerts | Работи | `tradingagents/dashboard/costs.py`, `cost_alerts.py` |
| LLM evaluation harness | Работи | `tradingagents/dashboard/llm_eval.py` |
| Upstream sync process | Работи чрез GitHub Actions | `.github/workflows/upstream-sync.yml`, `docs/UPSTREAM_SYNC.md` |

## Runtime Локации

Repo:

```bash
/home/nsitnov/tradingagents
```

Python environment:

```bash
/home/nsitnov/tradingagents/.venv
```

Dashboard env:

```bash
/home/nsitnov/.config/tradingagents-dashboard.env
```

Dashboard data:

```bash
~/.tradingagents/dashboard/dashboard.sqlite3
~/.tradingagents/dashboard/ledger.json
~/.tradingagents/dashboard/automation.json
~/.tradingagents/dashboard/autopilot.json
~/.tradingagents/dashboard/openai_cost_baseline.json
```

## Автономност

Нормалната работа не изисква ръчно натискане на бутони в dashboard-а.
Dashboard-ът е за наблюдение, настройки и debug.

Активните user-level systemd timers са:

| Timer | Schedule | Роля |
| --- | --- | --- |
| `tradingagents-autopilot.timer` | на всеки 30 минути | scanner/confluence/paper execution цикъл |
| `tradingagents-cost-alert.timer` | на всеки 30 минути | OpenAI daily spend alert |
| `tradingagents-daily.timer` | Mon-Fri 09:00 Europe/Sofia | daily TradingAgents analysis |
| `tradingagents-weekly-report.timer` | Sun 09:00 Europe/Sofia | weekly portfolio email report |

Полезни operational команди:

```bash
systemctl --user list-timers --all | rg tradingagents
systemctl --user status tradingagents-dashboard.service
systemctl --user status tradingagents-autopilot.timer
systemctl --user status tradingagents-cost-alert.timer
journalctl --user -u tradingagents-autopilot.service -n 100 --no-pager
journalctl --user -u tradingagents-cost-alert.service -n 100 --no-pager
```

## Dashboard

Dashboard service:

```bash
tradingagents-dashboard.service
```

Локален адрес:

```text
http://127.0.0.1:8790
```

Public access е през Cloudflare tunnel и Basic Auth. Login данните са само в
локалния env файл.

Dashboard tabs:

- `Progress`: weekly progress scorecard;
- `Autopilot`: автономен paper-autopilot status/config/jobs;
- `LLM Scorecard`: локален LLM eval harness;
- `Live Runs`: manual/debug TradingAgents run;
- `Paper Portfolio`: ledger, P&L, positions, benchmarks;
- `Backtest Lab`: локални replay/backtest runs;
- `Replay Lab`: TradingAgents replay върху исторически периоди;
- `Scanner Lab`: cross-market events/signals/confluence;
- `Automation`: daily automation config/history;
- `Readiness`: production readiness gates;
- `Analysis History`: historical agent runs;
- `OpenAI Spend`: OpenAI organization/project spend, adjusted by local baseline.

Refresh бутоните са само UI refresh. Те не стартират търговия.

Manual бутони като `Run Now`, `Run Backtest`, `Start Replay`, `Scan Event` са
debug/lab controls. Нормалната автономна работа не трябва да разчита на тях.

## Paper-Only Execution

Системата не прави реални broker сделки.

Paper execution flow:

1. Agent/scanner генерира signal или decision.
2. Paper OMS създава order candidate.
3. Risk gates проверяват exposure, concentration, drawdown, cooldown и kill-switch условия.
4. Ако order-ът мине, ledger-ът симулира fill.
5. Всичко се записва в SQLite audit/order/fill/risk tables.

Реален broker layer е нарочно disabled by default. Broker activation изисква
отделна фаза: reconciliation worker, broker-vs-local diff, manual approval mode
и поне един месец стабилна paper performance.

## LLM Routing

Текущата практична стратегия е:

- локален Ollama модел за евтини/прости quick задачи;
- OpenAI само за важни reasoning/critical tasks;
- fallback към OpenAI при локален LLM failure;
- LLM eval harness преди promotion на нов локален модел.

Текущо предпочитан локален модел:

```text
gpt-oss:20b
```

Причина: последният eval показа, че `gpt-oss:20b` се справя по-добре от
`qwen3.6:27b` за нашите JSON/decision eval prompts. Изтеглените candidate модели
могат да стоят на disk, но не заемат RAM, докато не бъдат заредени.

Ollama runtime policy:

- context window за eval: `8192`, за да не се зарежда огромен default context;
- runtime cap policy: 50 GiB;
- при тест на друг модел: първо се спира текущият Ollama модел, после се зарежда новият;
- не се пипат модели, които се използват от други услуги на сървъра.

## LLM Model Evaluation Harness

Файл:

```bash
tradingagents/dashboard/llm_eval.py
```

Цел:

- сравнява candidate локален модел срещу baseline;
- мери schema success rate, invalid JSON rate, fallback rate, judge score,
  latency и runtime GiB;
- не promotion-ва модел, ако е по-слаб от baseline или нарушава resource limits;
- може да update-не automation/autopilot config само след pass.

Пример:

```bash
uv run python -m tradingagents.dashboard.llm_eval \
  --models qwen3.6:27b \
  --baseline gpt-oss:20b \
  --max-runtime-gib 50 \
  --memory-reserve-gib 32 \
  --context-window 8192
```

## OpenAI Разходи И Alerts

Локалният Ollama модел не прави OpenAI API разход.

OpenAI spend се следи през OpenAI Costs API. Това е organization/project spend,
не гарантирано само заявки от тази платформа, ако същият OpenAI project/key се
използва от други процеси.

Daily alert:

```bash
tradingagents-cost-alert.timer
tradingagents-cost-alert.service
```

Прагът е env:

```bash
TRADINGAGENTS_DAILY_OPENAI_ALERT_USD=5
```

Когато effective daily spend мине прага, системата праща email през Resend.
Има dedup: максимум един email на ден за същия праг.

Baseline reset:

```bash
python -m tradingagents.dashboard.cost_alerts --reset-baseline
```

Това не изтрива реалния OpenAI invoice history. То записва текущия raw spend в:

```bash
~/.tradingagents/dashboard/openai_cost_baseline.json
```

След reset dashboard/alerts/budget guard смятат effective spend като:

```text
effective spend = max(raw OpenAI spend today - baseline, 0)
```

## Weekly Report

Weekly portfolio report се праща всяка неделя в 09:00 Europe/Sofia през Resend.

Файлове:

```bash
tradingagents/dashboard/weekly_report.py
deploy/tradingagents-weekly-report.service
deploy/tradingagents-weekly-report.timer
```

Email настройките са в локалния env:

```bash
RESEND_API_KEY
TRADINGAGENTS_REPORT_FROM
TRADINGAGENTS_REPORT_TO
TRADINGAGENTS_REPORT_TIMEZONE
```

Report-ът включва:

- start/end equity;
- net P&L;
- gross gains/losses;
- realized/unrealized P&L;
- trades summary;
- open positions;
- progress scorecard;
- OpenAI cost signal.

## Risk И Readiness

Risk/readiness слоевете са deterministic guardrails около agent decisions:

- exposure caps;
- concentration limits;
- realized/unrealized P&L tracking;
- drawdown checks;
- cooldown after loss;
- kill switch;
- rejected order audit trail;
- readiness gate за production/live trading.

Live trading status трябва да остава disabled, докато readiness gate не е готов
и няма отделно ръчно решение.

## Git И Upstream

Remotes:

```text
origin   https://github.com/nsitnov/TradingAgents.git
upstream https://github.com/tauricresearch/tradingagents.git
```

Правило:

- `origin` е нашият fork и production repo;
- `upstream` е read-only оригиналният проект;
- upstream changes минават през PR/sync branch;
- никога не push-ваме към `upstream`.

Документ:

```bash
docs/UPSTREAM_SYNC.md
```

## Проверки

Focused tests за последните platform промени:

```bash
uv run --with pytest pytest \
  tests/test_dashboard_costs.py \
  tests/test_dashboard_cost_alerts.py \
  tests/test_dashboard_automation_guard.py
```

Dashboard smoke:

```bash
curl -fsS http://127.0.0.1:8790/api/health
```

Ollama status:

```bash
ollama ps
```

## Operational Правила

- Не записвай secrets в git.
- Не активирай real broker execution без отделен implementation/review.
- Не promotion-вай нов локален LLM само защото е по-нов; минава през eval harness.
- При LLM model test спирай текущия модел, преди да заредиш друг.
- OpenAI spend в dashboard-а е invoice/API cost signal; за чист platform-only
  accounting трябва отделно per-run usage ledger.
- Dashboard бутоните са manual controls; автономната работа е през timers.
- Ако timer не работи, гледай systemd journal преди да стартираш ръчно run.
