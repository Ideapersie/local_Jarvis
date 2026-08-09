# Jarvis: Local Personal OS

Build spec. Feed this to Claude Code as the first thing it reads in the project folder.

---

## 0. How to use this document

Run this in **Claude Code**, not the Claude app. This project needs file creation, git, package installs, a local server on a port, and OAuth flows that write token files to disk. Claude Code does all of that in the real filesystem. The Claude app is the right place to think and plan, which is what produced this document, but it cannot run your server or hold your repo.

Setup:

```bash
mkdir jarvis && cd jarvis
git init
mkdir -p docs
# save this file as docs/SPEC.md
claude
```

Then in the session: `read docs/SPEC.md and build Day 1`.

Keep this file in the repo. It doubles as the project's own memory: when a future session asks "what was the plan," the answer is on disk.

---

## 1. What this is

A single locally-hosted web dashboard, opened in a browser tab at `localhost:8000`, that acts as a personal operating system. It is not a chatbot with extras bolted on. It is a database with a Claude agent that reads and writes it.

Five surfaces:

1. **Habit tracker** with streaks, monthly customisation, no-snooze reminders
2. **To-do list**, lightweight, tied to nothing else
3. **Inbox triage**: what actually needs a reply today
4. **Career tracker**: application stages, per-interview prep gaps, weaknesses, skills inventory
5. **Chat panel**: a Claude window with real memory of all of the above

One scheduled brief in the morning, one conditional reminder at 6pm, one reflection on Sundays.

---

## 2. Non-negotiable design decisions

These are the decisions that make it survive past week one. Do not quietly reverse them.

**Memory lives in files and SQLite, never "in the model."** No model remembers anything between calls, local or hosted. Durability comes from the store. This also means the model is swappable later at zero cost.

**The agent reads memory on demand, it does not carry it.** Do not stuff the whole database into a system prompt. Give the agent file and query tools and let it pull the slice it needs. Context stays small, cost stays low, and it scales as the corpus grows.

**Structured state goes in SQLite. Narrative state goes in markdown.** Streak counts, application stages, and task rows are rows. "I froze on the systems design round and could not reason about backpressure" is prose in a markdown file. Trying to force the second kind into columns is the most common way projects like this die.

**Every write the agent makes is visible in git.** The brain folder is version controlled. If the agent writes something wrong to `weaknesses.md`, you can see the diff and revert it.

**Localhost only.** No public port. If you want it on your phone, use Tailscale, not port forwarding.

---

## 3. Architecture

```
                         browser (localhost:8000)
                                    |
                              HTMX + Tailwind
                                    |
                      ------------------------------
                      |         FastAPI            |
                      |  routes, HTMX partials     |
                      ------------------------------
                       |            |             |
              -------------   -------------   -------------
              |  SQLite   |   | Agent SDK |   |  Quick    |
              | jarvis.db |   |  tier     |   |  tier     |
              -------------   -------------   -------------
                                   |               |
                            brain/ + briefs/   Messages API
                            file read/write    (single-shot)
                                   |
                             MCP + integrations
                       (Gmail, Calendar, Trading212)
                                   |
                            APScheduler jobs
                      (06:30 brief, 18:00 creatine, Sun 19:00)
```

### Two Claude tiers, and when to use which

**Quick tier** (`app/quick.py`) uses the plain `anthropic` client SDK. One request, one response, no tools, no loop. Use it for anything with a fixed shape:

- Classify 40 email subjects into reply-needed / FYI / ignore
- Score a startup idea against a rubric
- Rewrite a brief into TTS-friendly prose
- Extract a company name and role from a rejection email

Model: `claude-haiku-4-5-20251001` for classification, `claude-sonnet-5` when the writing quality matters.

**Agent tier** (`app/agent.py`) uses `claude-agent-sdk`. Full agent loop with file tools, bash, web search, and MCP. Use it when the task needs planning and multiple reads:

- "Write tomorrow's brief" (reads calendar, queries DB, greps past briefs, writes a new file)
- "I have a Marshall Wace interview in six days, what should I do" (reads prep_notes, weaknesses.md, searches the web, writes a plan back to disk)
- The chat panel, every message
- Sunday reflection (reads a month of briefs and habit logs, rewrites goals.md)

Model: `claude-sonnet-5`. Use `claude-opus-5` only for portfolio trim/buy reasoning, where a bad call costs actual money.

### Why the Agent SDK and not just the API

The Agent SDK is Claude Code as a Python library. It gives you the agent loop, built-in file and bash and web-search tools, MCP connections, subagents, and sessions that persist across exchanges, without you implementing tool dispatch. Critically, it loads skills, slash commands, and memory from a `.claude/` directory exactly like Claude Code does, so the project's brain is configured in files rather than in Python strings.

Set `setting_sources=["project"]` in `ClaudeAgentOptions` or the `.claude/` directory is ignored. This is the single easiest thing to get wrong.

The SDK spawns a bundled Claude Code CLI under the hood, which needs Node present. The Python package bundles it, so there is usually nothing extra to install, but if the SDK fails on first run with a spawn error, that is why.

### What cannot be connected

Your claude.ai memory files and the connectors configured in the Claude app are scoped to that product's login. There is no API to read them, and Anthropic does not permit third-party or self-built agents to authenticate via claude.ai login. Use an API key.

The formats port, though, so this is a rebuild and not a loss:

| In the Claude app | In this project |
| --- | --- |
| Memory files | `brain/*.md`, read by the agent's file tools |
| Skills | `.claude/skills/<name>/SKILL.md`, loaded automatically |
| Gmail / Calendar connectors | Google API OAuth in `app/integrations/`, or a Gmail MCP server |
| Notion connector | Notion MCP via `mcp_servers`, your own OAuth token |
| Project instructions | `.claude/CLAUDE.md` |

Copy your existing `linkedin-post` and `storytelling-agent` skills into `.claude/skills/` on day two. They work unchanged.

---

## 4. Memory model

Three layers. Every piece of state belongs to exactly one.

### Layer 1: SQLite (`jarvis.db`)

Structured, queryable, cheap. The agent reads it through a custom MCP tool or a plain function, never by dumping tables into context.

### Layer 2: `brain/` (markdown, git tracked)

Narrative memory. The agent reads these at the start of relevant tasks and edits them after. Keep each under roughly 300 lines so a read is cheap.

- `profile.md` — who you are, current situation, what you are optimising for
- `goals.md` — the monthly big goal, rewritten every Sunday
- `weaknesses.md` — recurring technical and behavioural gaps, with dates
- `interview-log.md` — what happened in each round, what was asked, what went badly
- `skills.md` — narrative version of the skills table, what "good at Kubernetes" actually means for you
- `ideas.md` — startup idea log with agent feedback appended under each

### Layer 3: `briefs/`

One markdown file per day, `briefs/2026-08-09.md`. Never edited after writing. The agent greps these for history rather than holding them in context. This is what lets it say "you have flagged the same weakness three Mondays in a row."

---

## 5. Data model

SQLModel, in `app/db.py`. Concrete enough to build from directly.

```python
class Habit(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    category: str                      # health | career | personal
    reminder_time: str | None          # "18:00", None for anytime
    active: bool = True
    created_at: date
    archived_at: date | None = None    # soft delete, keeps history intact

class HabitLog(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    habit_id: int = Field(foreign_key="habit.id")
    day: date
    done: bool = True
    # unique constraint on (habit_id, day)

class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    done: bool = False
    due: date | None = None
    created_at: datetime

class Application(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    company: str
    role: str
    stage: str          # applied | oa | phone | technical | final | offer | rejected
    next_date: date | None = None
    source: str | None = None
    notion_id: str | None = None       # for later two-way sync
    updated_at: datetime

class PrepNote(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id")
    kind: str           # topic_to_learn | weakness | question_asked | outcome
    body: str
    resolved: bool = False
    created_at: datetime

class Skill(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    confidence: int     # 1-5, you set it, agent suggests changes
    target: int         # where you want it
    last_touched: date | None = None

class Holding(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ticker: str
    quantity: float
    avg_price: float
    horizon: str        # long | short
    synced_at: datetime
```

**Streaks are computed, not stored.** A function over `HabitLog` walking backwards from today. Storing a counter means it drifts the first time you backfill a day.

---

## 6. Tech stack

| Layer | Choice | Why this one |
| --- | --- | --- |
| Language | Python 3.12 | Same as ChanciAI, no new runtime |
| Web framework | FastAPI + Uvicorn | Async, you already know it, serves both JSON and HTML |
| DB | SQLite + SQLModel | Single file, zero ops, typed models without a migration framework |
| Frontend | HTMX + Tailwind (CDN) | No npm, no build step, no bundler. A habit toggle is one endpoint returning one HTML partial |
| Chat streaming | SSE via HTMX SSE extension | Native fit, no websocket plumbing |
| Agent | `claude-agent-sdk` | Agent loop, file tools, MCP, sessions, `.claude/` config |
| Single-shot LLM | `anthropic` | Cheap tier |
| Scheduler | APScheduler (in-process) | Runs inside Uvicorn, no separate cron or worker |
| Google | `google-api-python-client`, `google-auth-oauthlib` | Gmail + Calendar, desktop OAuth, cached `token.json` |
| Config | `python-dotenv` | `.env`, gitignored |
| Formatting | `ruff` | One tool, fast |

Deliberately not used: React or Vite (build step you do not need yet), Postgres (SQLite is correct at one user), Celery or Redis (APScheduler is enough), Docker (adds a layer before there is anything to isolate), a local LLM (see section 10).

---

## 7. Repository layout

```
jarvis/
  docs/
    SPEC.md                  this file
  app/
    main.py                  FastAPI app, lifespan starts scheduler
    config.py                settings from .env, model name constants
    db.py                    SQLModel models + engine + create_all
    deps.py                  session dependency

    routers/
      habits.py              GET dashboard partial, POST toggle, POST add, POST archive
      tasks.py               CRUD, all returning HTML partials
      career.py              applications, prep notes, skills
      inbox.py               triage results, mark handled
      chat.py                SSE endpoint streaming the agent
      brief.py               view today's brief, trigger manual run

    services/
      streaks.py             current + best streak from HabitLog
      brief_builder.py       assembles inputs, calls agent tier, writes briefs/
      triage.py              fetch -> quick tier classify -> persist

    agent.py                 Agent SDK wrapper: session mgmt, options, tools
    quick.py                 Messages API helpers, typed returns
    jobs.py                  APScheduler job definitions + registration

    integrations/
      gmail.py               OAuth flow, list recent, fetch bodies
      gcal.py                today + next 7 days
      outlook.py             MS Graph, phase 2
      trading212.py          portfolio + positions, phase 2
      weather.py             forecast by hour for a location
      elevenlabs.py          text -> mp3, phase 2

  .claude/
    CLAUDE.md                agent's standing instructions
    skills/
      linkedin-post/         copied from existing setup
      storytelling-agent/    copied from existing setup
      brief-writer/          new: how to write the morning brief
      interview-prep/        new: how to turn an application into a prep plan
    settings.json            MCP servers, permissions

  brain/                     markdown memory, git tracked
  briefs/                    dated brief archive
  templates/
    index.html               shell, loads all panels
    partials/                habit_row.html, task_row.html, chat_msg.html, ...
  static/
    app.css                  the small amount Tailwind cannot do

  .env                       gitignored
  .env.example
  .gitignore                 .env, token.json, jarvis.db, __pycache__
  requirements.txt
  jarvis.db
  README.md
```

### `.claude/CLAUDE.md` starting content

```markdown
# Jarvis project instructions

You are the agent behind a personal dashboard for Idea MH Khan, an MSc AI student
at KCL graduating August 2026, job hunting for London AI/SWE grad roles.

## Memory protocol
- Read only the brain/ files relevant to the current task. Do not read all of them.
- After any session that reveals something durable, append to the right brain/ file.
  Do not rewrite whole files unless asked.
- Never invent an entry. If you inferred it rather than were told it, say so in the line.

## Voice
Direct and concise. Short sentences. No preamble, no filler, no summarising what you
just did. No em-dashes. No emojis in files or code.

## Database
Query via the provided tools. Never assume a row exists without checking.

## Guardrails
- Do not make financial recommendations framed as advice. Present reasoning and let
  the user decide.
- Do not write to brain/ or briefs/ during a chat session unless the user confirms.
```

---

## 8. How the pieces connect

**Habit toggle:** click ring, HTMX POST to `/habits/{id}/toggle`, upsert `HabitLog`, recompute streak, return the single habit row partial. No page reload, no JSON parsing, no client state.

**6pm creatine check:** APScheduler fires `jobs.creatine_check` at 18:00. Query `HabitLog` for today. If done, return silently and send nothing. If not, send one message. The conditional is in Python, not in a prompt, because it is a boolean and a model should never be asked to evaluate a boolean.

**Morning brief at 06:30:** `brief_builder` collects the deterministic inputs first (calendar events, hourly weather for the day's location, habit streaks, open applications with dates inside 14 days, unread email count). Then it calls the agent tier once with those inputs plus instructions to grep the last seven briefs and read `goals.md` and `weaknesses.md`. The agent writes `briefs/YYYY-MM-DD.md` and a short summary the dashboard renders. Deterministic data is fetched in code, judgement is delegated to the agent. Do not ask the agent to fetch the weather.

**Weather and the football problem:** run two forecast lookups, one for the hour before the event and one for two hours after, and pass both as plain numbers. Do not ask the model to reason about a time window from a raw forecast blob, it will occasionally get it wrong and you will not notice.

**Inbox triage:** `gmail.py` pulls the last 24 hours of headers. Quick tier classifies in one batched call returning JSON. Anything marked reply-needed gets its body fetched and a one-line "why" generated. Results go in a small `triage` table with a handled flag so the panel does not resurface the same thread.

**LinkedIn and WhatsApp:** there is no legitimate API for reading either. LinkedIn has no messaging API and scraping breaches its terms, which risks your account during a job hunt. WhatsApp's Business API only covers messages to a business number you own, and the unofficial web-session libraries breach terms and do get accounts banned. Both platforms already email you notifications. Route them through Gmail with their own triage category and you get almost all the value with none of the risk. Build it this way.

**Career tracker:** `Application` rows hold stage and dates. `PrepNote` rows hold the interesting part. When an interview is inside seven days, the dashboard shows a prep button that runs the `interview-prep` skill: read the application, read its unresolved prep notes, read `weaknesses.md`, search the web for the firm's process, write a plan, and append new prep notes. After the interview you tell the chat panel how it went and the agent appends to `interview-log.md` and adjusts `Skill.confidence`.

**Chat panel:** each message hits `/chat` as SSE. Reuse one Agent SDK session per browser session so context carries across exchanges without resending history. The agent has the DB tools and file access, which is what makes it a Claude window over your own life rather than a generic chat.

**MCP:** configure servers in `.claude/settings.json`. Notion is the one worth wiring early, for two-way sync with your existing job page. Your own OAuth token, not the app's.

---

## 9. Three day plan

Each day ends with something that runs. Do not start the next day until the acceptance check passes.

### Day 1: shell, database, habits, tasks

- [ ] `requirements.txt`, venv, `.env.example`, `.gitignore`, initial commit
- [ ] `db.py` with every model from section 5, `create_all` on startup
- [ ] Seed the five habits: workout (20:00), creatine (18:00), call home, leetcode, ai research
- [ ] `streaks.py` with current and best streak, plus a unit test for the backfill case
- [ ] `templates/index.html` shell, dark theme, panel grid
- [ ] Habit panel: rings, 7 day history bars, streak count, add and archive, all HTMX
- [ ] Task panel: add, toggle, delete
- [ ] Uvicorn with reload, APScheduler started in the FastAPI lifespan with one no-op job that logs, to prove it fires

**Acceptance:** open `localhost:8000`, toggle a habit, refresh the browser, streak persisted. Scheduler log line appears.

Port the visual design from the HTML prototype already built: amber on charcoal, IBM Plex Mono for numbers, Inter for text, rings not checkboxes.

### Day 2: the agent and the inbox

- [ ] `pip install claude-agent-sdk anthropic`, key in `.env`
- [ ] `.claude/CLAUDE.md` from section 7
- [ ] `brain/` seeded with `profile.md`, `goals.md`, `weaknesses.md`, `skills.md`, `interview-log.md`, `ideas.md`
- [ ] Copy `linkedin-post` and `storytelling-agent` into `.claude/skills/`
- [ ] `agent.py`: `ClaudeAgentOptions` with `setting_sources=["project"]`, working directory pinned to the project root, permissions scoped so it cannot touch anything outside it
- [ ] Custom tools exposing DB queries to the agent (read-only first, writes once reads are proven)
- [ ] `/chat` SSE endpoint, chat panel in the UI, session reuse
- [ ] Google OAuth desktop flow, `token.json` cached and gitignored
- [ ] `gmail.py` list last 24h, `quick.py` batch classifier, triage panel

**Acceptance:** ask the chat panel "what is my leetcode streak and what should I work on this week." It queries the DB, reads `goals.md` and `weaknesses.md`, and answers from real data. Inbox panel shows a correctly triaged list.

### Day 3: career tracker, brief, schedule

- [ ] Career panel: applications by stage, prep notes inline, skills with confidence versus target
- [ ] `.claude/skills/interview-prep/SKILL.md` and the button that runs it
- [ ] `gcal.py` for today plus next seven days
- [ ] `weather.py` with the two-lookup pattern
- [ ] `.claude/skills/brief-writer/SKILL.md`
- [ ] `brief_builder.py`, writes `briefs/YYYY-MM-DD.md`, renders a summary card
- [ ] Real jobs registered: 06:30 brief, 18:00 creatine check, Sunday 19:00 reflection that rewrites `goals.md`
- [ ] Delivery for reminders. Telegram bot is the least work and reaches desktop and phone
- [ ] `README.md` with setup steps for future you

**Acceptance:** trigger the brief manually, get a real one covering calendar, weather, streaks, and upcoming interviews. Untick creatine, force the 18:00 job, receive exactly one message. Tick it, force again, receive nothing.

---

## 10. Phase 2 backlog

In rough value order, none of it on the critical path.

**Trading212 and the analysis agent.** Portfolio sync, then a pre-market and market-open summary, then the trim/buy analysis on Opus with sourced articles. Deliberately last of the big pieces: it is the highest-effort integration and the one where a wrong output costs money rather than time. Frame every output as reasoning, never as a recommendation.

**ElevenLabs TTS.** Once the brief is consistently good in text. Generating audio from a mediocre brief just makes a mediocre brief you cannot skim.

**Outlook via MS Graph.** Only if something actually lands there that Google does not.

**Notion two-way sync.** Notion MCP, reconciling `Application.notion_id`. Worth it if you keep using the Notion job page as a second surface.

**Notion Calendar alongside this.** It already unifies Google, Apple, and Outlook into one view on desktop and mobile for free. Use it as the calendar you look at. Jarvis reads the underlying APIs for reasoning. Do not build a calendar UI.

**Apple Calendar.** No usable API. Subscribe or forward it into Google Calendar and read it from there.

**Tailscale.** When you want the dashboard on your phone.

**Local model.** Only worth it for embeddings once `briefs/` is large enough that grep stops being sufficient, likely months out. A 7B local model cannot do the judgement calls in this project, and it cannot call Gmail or Trading212 without you rebuilding the tool layer the Agent SDK gives you free. If cost is the worry, the quick tier on Haiku is already close to nothing at one user.

---

## 11. Environment

```
ANTHROPIC_API_KEY=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
WEATHER_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DEFAULT_LOCATION=bangkok
TZ=Asia/Bangkok
```

Phase 2 adds `TRADING212_API_KEY`, `ELEVENLABS_API_KEY`, `NOTION_TOKEN`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET`.

Location: default from `.env`, overridden per day if a calendar event carries a location that implies the other city. Two cities only, so a simple keyword match beats anything clever.

---

## 12. Guardrails

The agent has bash and filesystem access. Treat that seriously.

- Pin the working directory to the project root. Never the home directory.
- Scope tool permissions in `.claude/settings.json`. Start restrictive and loosen when something is actually blocked.
- Keep `brain/` in git so every agent write is a reviewable diff.
- Give the agent read-only DB access first. Add writes per table, only after reads are proven correct.
- `.env`, `token.json`, and `jarvis.db` never get committed.
- No public port. Ever.
- Financial output is reasoning, not advice. The agent is not a licensed adviser and neither is the model behind it.
