# RBGA Services

Services for the RMIT Board Game Association, built as **one small app, not a
pile of microservices** (see `CLAUDE.md` for the reasoning). Everything lives in
the `rbga` Python package and shares one Postgres database:

| Feature | Where | Notes |
|---------|-------|-------|
| Key tracker | `rbga/api/routers/keys.py`, `rbga/bot/` | Ported from Owen's [RBGAKeyTracker](https://github.com/Scroojalix/RBGAKeyTracker). Usable over REST *and* Discord. |
| Board-game inventory | `rbga/api/routers/boardgames.py` | REST CRUD. |
| Anonymous complaints | `rbga/api/routers/complaints.py` | Public submit, token-gated read. Stores nothing identifying. |
| Discord bot | `rbga/bot/` | Slash commands (`/keys`, `/whohas`, `/take`). |

## Two processes, one image

- **API** (FastAPI, HTTP) — `uvicorn rbga.api.main:app`
- **Bot** (discord.py, long-running gateway connection) — `python -m rbga.bot`

They're separate processes because their runtimes differ, but they build from the
same Dockerfile and import the same `rbga/db` layer. Adding a *feature* means
adding a module, not a new service.

## Local dev (SQLite, no Postgres needed)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
uvicorn rbga.api.main:app --reload                  # http://localhost:8000/docs
```

With no `DATABASE_URL` set it uses a local `rbga_dev.db` SQLite file and
auto-creates the tables, so the API runs with zero setup. Interactive docs at
`/docs`.

To run the bot locally, set `DISCORD_TOKEN` (see `.env.example`) then
`python -m rbga.bot`.

## Full stack (Postgres + api + bot)

```bash
cp .env.example .env   # fill in values
docker compose up -d --build
```

## Layout

```
rbga/
  db/       models.py + database.py  (one schema of truth, shared)
  api/      main.py + routers/       (FastAPI: keys, boardgames, complaints)
  bot/      __main__.py              (discord.py; `python -m rbga.bot`)
compose.yml  Dockerfile  pyproject.toml
```

## Not done yet

- **Alembic migrations.** The API auto-creates tables on startup for dev
  convenience; production needs real migrations before the schema is trusted.
- **Complaints front-end.** The endpoint exists; the actual form (likely the
  GitHub Pages landing page POSTing to `/complaints`) is TODO.
- **Auth on keys/board-games writes.** Currently open. Fine on a private network;
  needs a gate before public exposure.
