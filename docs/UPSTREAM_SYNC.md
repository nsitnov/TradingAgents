# Upstream sync workflow

This fork keeps local TradingAgents customizations while tracking the original
project at `upstream`.

## Remotes

- `origin`: `https://github.com/nsitnov/TradingAgents.git`
- `upstream`: `https://github.com/tauricresearch/tradingagents.git`

`upstream` should be treated as read-only. Keep local and production changes in
this fork.

## Manual update process

GitHub Actions runs the `Upstream Sync` workflow every Monday at 05:17 UTC. It
checks `upstream/main`, creates or updates a `sync/upstream-YYYY-MM-DD` branch,
and opens a pull request into `main` when there are upstream changes. It never
auto-merges.

Use a short-lived integration branch for each upstream update:

```bash
git fetch upstream
git switch main
git pull origin main
git switch -c sync/upstream-YYYY-MM-DD
git merge upstream/main
```

Resolve conflicts in favor of preserving the local dashboard behavior unless the
upstream change intentionally replaces the same functionality.

After resolving conflicts, run the validation gate:

```bash
uv sync
uv run pytest
uv run pytest tests/test_dashboard_*.py
```

Open a pull request from the `sync/upstream-YYYY-MM-DD` branch into `main`.
Merge only after local tests and the GitHub Actions test workflow pass, and
dashboard behavior has been checked.

## Conflict guidance

- Keep `pyproject.toml` and `uv.lock` changes consistent.
- Re-run `uv sync` after dependency conflicts.
- Give extra attention to shared agent, dataflow, model provider, checkpoint,
  and ticker handling code because dashboard behavior depends on those areas.
- Do not push local changes directly to `upstream`.
