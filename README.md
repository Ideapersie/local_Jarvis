# Jarvis

A locally-hosted personal dashboard at `localhost:8000`. Habits, tasks, career
tracking, a morning brief, and a chat panel with real memory of all of it.

Not a chatbot with extras bolted on: a database with a Claude agent that reads
and writes it. Full design rationale is in `JARVIS_SPEC.md`.

**Localhost only. Never expose this on a public port.**

---

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then fill in ANTHROPIC_API_KEY
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`.

Requires **Python 3.12+** and **Node on PATH** — the Agent SDK spawns a bundled
Claude Code CLI, and a missing Node is the most likely first-run failure.

---

## Environment

Only `ANTHROPIC_API_KEY` is needed to start. Everything else unlocks a feature.

| Variable | Needed for | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Everything | Quick tier always uses this |
| `AGENT_AUTH` | Billing choice | `subscription` (default) or `api_key` |
| `AGENT_AUTH_FALLBACK` | Resilience | `true` (default): use the key if plan credit runs out |
| `PLAN` | Dashboard spend bar | `pro` / `max5` / `max20` / `none` |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Gmail, Calendar | See below |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Reminder delivery | Without these, reminders log + show on the dashboard |
| `TRADING212_API_KEY`, `TRADING212_API_SECRET` | Portfolio | Read scopes only |
| `TRADING212_BASE_URL` | Portfolio | Defaults to the demo environment |
| `DEFAULT_LOCATION` | Weather | `bangkok` or `london` |
| `TZ` | Everything dated | e.g. `Asia/Bangkok` |

**No weather key needed.** Open-Meteo is keyless. The spec mentions
`WEATHER_API_KEY`; it is deliberately unused.

`.env`, `token.json`, `credentials.json`, `jarvis.db` and `logs/` are gitignored
and must stay that way.

---

## Google Cloud setup

Unlocks Gmail triage and Calendar in the morning brief. Takes 30–45 minutes,
once.

1. Go to `console.cloud.google.com` and **create a project** (any name).
2. **APIs & Services → Library** → enable **Gmail API**.
3. Same place → enable **Google Calendar API**.
4. **APIs & Services → OAuth consent screen** → User type **External** → create.
5. Fill in app name and your own email for both support fields. Save.
6. **Add yourself as a test user** on the Test users step.
7. **Back on the OAuth consent screen, click `PUBLISH APP` → confirm.**

   Do not skip this. A project left in **Testing** issues refresh tokens that
   **expire after 7 days**, so you would be re-authorising every week forever.
   Verification is *not* required for personal use under 100 users. This is the
   single most common way this setup goes wrong.

8. **Credentials → Create credentials → OAuth client ID → Desktop app**.
9. Download the JSON, rename it `credentials.json`, put it in the repo root.
10. Authorise once, by hand:

    ```powershell
    .venv\Scripts\python.exe scripts\authorise-google.py
    ```

    This opens a browser, caches the token to `token.json`, then prints your
    recent mail and upcoming events as a check. **Run it interactively** — the
    06:25 job cannot open a browser and would hang waiting for a click.

If you see `invalid_grant: Token has been expired or revoked` a week after
setting this up, step 7 did not take.

---

## Telegram (optional, ~5 minutes)

1. Message `@BotFather` on Telegram → `/newbot` → follow prompts → copy the token.
2. Message your new bot once (it cannot message you first).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
4. Put both in `.env` as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

Without this, reminders still fire — they log and appear in the Urgent card.

---

## Autostart

The scheduler runs **inside** the server process, so the 06:30 brief only fires
if Jarvis is running at 06:30.

```powershell
.\scripts\install-task.ps1          # register a logon task
Start-ScheduledTask -TaskName Jarvis   # test without logging out
.\scripts\uninstall-task.ps1        # remove it
```

Autostart alone is not enough: if the machine is **off** through 06:30 and you
log in at 09:00, the cron time has already passed and the job never fires. The
catch-up in `app/jobs.py` covers that — on startup, if today has no brief and
06:30 has passed, it builds one. Both pieces are needed.

Logs go to `logs/jarvis.log` (the process runs with no console window).

---

## Model tiers and cost

Three tiers, because the agent harness is expensive and most questions do not
need it.

| Tier | Model | Used for | Measured cost |
| --- | --- | --- | --- |
| Quick | `claude-haiku-4-5` | Chat lookups, classification | ~$0.0007/call |
| Agent | `claude-sonnet-5` | Brief, interview prep, complex chat | ~$0.15/turn |
| Portfolio | configurable | Weekly portfolio reasoning | Not yet wired |

The agent tier carries ~25k tokens of Claude Code harness per turn. A chat
message like "what's my leetcode streak" is a database lookup, so it goes to the
quick tier over a prefetched snapshot — about **270× cheaper and 9× faster**.
The model itself decides when to escalate; anything needing files, the web, or
multi-step reasoning goes to the agent.

Spend is recorded per call and shown month-to-date in the dashboard header.

**Billing:** `AGENT_AUTH=subscription` bills the monthly Agent SDK credit
included with a Claude plan (Pro $20 / Max 5x $100 / Max 20x $200, no rollover,
one-time opt-in). The SDK picks up your `claude.ai` login when no API key
reaches the subprocess. When the credit runs out, requests stop — hence
`AGENT_AUTH_FALLBACK`.

---

## Scheduled jobs

| Time | Job | Notes |
| --- | --- | --- |
| 06:25 | Inbox triage | One batched quick-tier call, so the brief reads fresh results |
| 06:30 | Morning brief | Facts gathered in Python, judgement by the agent |
| 18:00 | Creatine check | Sends **exactly one** message, or nothing if already logged |
| Sun 19:00 | Weekly reflection | Rewrites the current month in `brain/goals.md` |

The 18:00 condition is a boolean evaluated in Python, never in a prompt.

Trigger any of them by hand from the brief panel, or `POST /brief/job/<id>`.

---

## Memory model

Three layers. Every piece of state belongs to exactly one.

- **`jarvis.db`** — structured, queryable. Habits, logs, tasks, applications,
  prep notes, skills, costs, reminders. The agent reads it through tools, never
  by dumping tables into context.
- **`brain/*.md`** — narrative, git-tracked. Profile, goals, weaknesses, skills,
  interview log, ideas. The agent may write here freely; **git is the safety
  net, so read `git diff brain/` after any session that changed something.**
- **`briefs/*.md`** — one file per day, never edited after writing.

Streaks are **computed, not stored** — a counter drifts the first time you
backfill a day.

---

## Agent permissions

- Working directory pinned to the repo root. The agent never sees `~`.
- `Write`/`Edit` confined to `brain/` and `briefs/` by an explicit resolved-path
  check. Deliberately **not** listed in `allowed_tools` — an allow entry
  auto-approves a tool *before* the callback runs, which would silently disable
  the confinement.
- Database writes are scoped to the **career tables only**. Habits and tasks are
  read-only: a fabricated habit log would silently corrupt a streak, and streaks
  are the one number here that has to be trustworthy.
- `Bash` is denied outright.
- `max_budget_usd` caps a runaway session.

Two checks that need a live model, so they are scripts rather than tests:

```powershell
.venv\Scripts\python.exe scripts\check_write_confinement.py
.venv\Scripts\python.exe scripts\check_session_lifecycle.py
```

---

## Testing

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check app tests scripts
.venv\Scripts\python.exe -m ruff format app tests scripts
```

Tests never touch the real `jarvis.db` — `tests/conftest.py` redirects the
engine to an in-memory database.

---

## Layout

```
app/
  main.py          FastAPI app, lifespan, middleware
  config.py        settings, model names, timezone
  db.py            SQLModel models
  agent.py         Agent SDK options, permission gate, session registry
  agent_tools.py   MCP tools the agent uses to read and write the database
  quick.py         single-shot tier
  jobs.py          scheduled jobs + catch-up
  routers/         habits, tasks, chat, brief, career
  services/        streaks, brief_builder, costs
  integrations/    weather, notify
.claude/           CLAUDE.md, settings.json, skills/
brain/             narrative memory (git tracked)
briefs/            dated brief archive
scripts/           autostart + live verification checks
```

---

## Known limitations

- The scheduler is in-process. Autostart plus catch-up covers the common cases,
  but Jarvis has to be running for anything to fire.
- The quick tier always bills the API key; it has no subscription path.
- Credit-exhaustion fallback is detected from error text and has not been
  exercised against a genuinely spent credit.
- Google access is **read-only**. Jarvis cannot label, archive, or send mail;
  triage state lives in its own table with a `handled` flag.
- Triage runs once a day at 06:25. It is not a live inbox, by design.
- Weather brackets only fire for events whose title or location matches a small
  outdoor-word list. A rained-on event with an unusual name will be missed.
