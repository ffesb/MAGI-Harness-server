# MAGI Harness — Agent Guidelines

## Environment & Commands
- **Toolchain:** Python 3.14 managed via `uv`. Always run commands via `uv run <cmd>`.
- **Workspace Path:** Path contains spaces (`/home/fesb/Projects/Evangelion MAGI Harness`). Systemd and scripts rely on symlink `~/.local/share/magi`. Always quote paths in shell commands.
- **Commands:**
  - Foreground daemon: `uv run magi-daemon` (or `uv run magi`)
  - TUI monitor: `uv run magi-tui` (connects read-only to `data/magi.sock`; closing it does not affect daemon)
  - Tests: `uv run pytest` or `uv run pytest tests/test_<name>.py -k <test_name>`
  - Syntax check: `uv run python -m compileall src/magi`
- **Daemon Lifecycle (systemd user service):**
  - Status/Logs: `systemctl --user status magi.service` | `journalctl --user -u magi.service -f`
  - Restart: `systemctl --user restart magi.service`
  - Unit file: repo `systemd/magi.service` mirrors `~/.config/systemd/user/magi.service`. If edited, run `systemctl --user daemon-reload && systemctl --user restart magi.service`.

## Architecture & Subsystems
- `src/magi/daemon.py`: Main lifecycle coordinator (wires DB, IPC, audit, scheduler, watcher, executor, and Telegram).
- `src/magi/channel/telegram.py`: Bot adapter (`@naoko_magi_bot`). Hard-restricted to `allowed_user_ids` (`7223503347`). Unauthorized IDs are silently dropped and logged as Tier 3 security events.
- `src/magi/council/`: Deliberation engine (`minds.py`, `voter.py`, `policy.py`). Melchior, Balthasar, and Casper deliberate concurrently using read-only system tools before voting.
- `src/magi/executor/`: Reasoner (`agent.py`) and tool catalog (`tools/readonly.py`, `tools/search.py`, `tools/mutant.py`). Unlisted tools default to Tier 2. `write_config_file` auto-creates `.bak` backups.
- `src/magi/session/manager.py`: Session coordinator. Freezes mind configs and prompts into `prompts_snapshot` at session creation time (`/new`).
- `src/magi/scheduler/`: Internal cron scheduler (`cron.py` via `croniter`) and anomaly/heartbeat log watcher (`watcher.py`, 09:00 daily pulse).
- `src/magi/core/`: Core primitives: `config.py` (settings loader & YAML writers), `db.py` (aiosqlite WAL), `audit.py` (dual-write), `ipc.py` (Unix socket NDJSON server), `provider.py` (OpenRouter API client).
- `src/magi/tui/app.py`: Evangelion-styled Textual monitor.

## Secrets & Config Persistence
- **Credentials:** Kept in `config/magi.env` (`chmod 600`), never commit. Reference: `config/magi.env.example`.
- **Dynamic Config Writes:**
  - Changing models (`/model_*`) or personalities (`/personality-*`) permanently rewrites YAML in `config/minds/{executor,melchior,balthasar,casper}.yaml`.
  - Updating provider key (`/provider <key>`) rewrites `config/magi.env`.
- **Risk Tiers (`config/risk_tiers.yaml`):**
  - Tier 0: Direct execution (read-only inspection, web search).
  - Tier 1: Low impact / reversible; requires simple MAGI majority (2/3).
  - Tier 2 & 3: Critical or irreversible; requires unanimous MAGI (3/3) + explicit human confirmation (`CONFIRMO`).

## Code Conventions & Gotchas
- **Telegram Bot Handlers:** `python-telegram-bot` throws `ValueError` on hyphens in `CommandHandler` (e.g., `CommandHandler("personality-casper")` crashes). All `CommandHandler` registrations MUST use underscores (`personality_casper`). Hyphenated commands (`/personality-*`, `/model-*`, `/cancel`) MUST be intercepted in `_handle_text_message`.
- **Command Language:** Telegram command names are strictly English (`/sessions`, `/audit`, `/cancel`, `/cronjobs`), with aliases handled where configured.
- **Database Concurrency:** SQLite (`data/magi.db`) operates in WAL mode via `aiosqlite`. Always query through the `Database` wrapper (`src/magi/core/db.py`).
- **Audit Dual-Write:** Every mutating action or state change must log via `AuditLogger.log_event` in `src/magi/core/audit.py` (writes both to SQLite `audit_log` table and `logs/audit-YYYY-MM-DD.jsonl`).
- **IPC Protocol:** Unix domain socket at `data/magi.sock` broadcasts NDJSON lines (`{"type": "...", "data": {...}}`). TUI receives `minds_sync` on connect and on any model change.
