# RBGA Services

Services for the RMIT Board Game Association, built as **one small app, not a
pile of microservices** (see `CLAUDE.md` for the reasoning). Everything lives in
the `rbga` Python package and shares one Postgres database:

| Feature | Where | Notes |
|---------|-------|-------|
| Key tracker | `rbga/api/routers/keys.py`, `rbga/bot/__main__.py` | Ported from Owen's [RBGAKeyTracker](https://github.com/Scroojalix/RBGAKeyTracker). Full CRUD over REST *and* Discord. |
| Board-game inventory | `rbga/api/routers/boardgames.py`, `rbga/bot/boardgames.py` | REST + Discord CRUD. Seed from a CSV with `rbga/db/import_boardgames.py`. |
| Anonymous complaints | `rbga/api/routers/complaints.py` | Public submit, token-gated read. Stores nothing identifying. Deliberately **not** exposed over Discord. |
| Discord bot | `rbga/bot/` | Slash commands for keys (`/keys`, `/whohas`, `/take`, `/return`, `/addkey`, `/removekey`) and board games (`/game list\|info\|add\|edit\|remove`). |

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
`python -m rbga.bot`. Bot reads (`/keys`, `/game list`, …) are open to everyone;
mutations are gated to the exec role named by `DISCORD_KEYS_ROLE` (fail-closed: no
role configured means no mutations).

### Seeding the board-game inventory

```bash
python -m rbga.db.import_boardgames "path/to/Board Games.csv" [--replace]
```

Skips the SharePoint `ListSchema` header, trims titles, treats blanks as NULL, and
refuses to run against a non-empty table unless `--replace`.

## Full stack (Postgres + api + bot)

```bash
cp .env.example .env   # fill in values
docker compose up -d --build
```

## Layout

```
rbga/
  db/       models.py + database.py     (one schema of truth, shared)
            import_boardgames.py        (CSV seed for the inventory)
  api/      main.py + routers/          (FastAPI: keys, boardgames, complaints)
  bot/      __main__.py + common.py     (discord.py; `python -m rbga.bot`)
            boardgames.py               (the /game command group)
compose.yml  Dockerfile  pyproject.toml
.github/workflows/ci-cd.yml            (build + auto-deploy to the home server)
```

## Deploy (home Debian server)

Pushes to `main` auto-deploy via GitHub Actions: the `build` job verifies the
Dockerfile on a hosted runner, then the `deploy` job runs on a **self-hosted runner
on the box**, does `git reset --hard origin/main` in `~/servers/rbga`, rebuilds, and
health-checks `http://localhost:30010/health`. See `.github/workflows/ci-cd.yml` and
`CLAUDE.md` for the server details (cloudflared ingress, `.env`). The bot container
is left out of the auto-deploy until a `DISCORD_TOKEN` is configured.

## Not done yet

- **Alembic migrations.** The API auto-creates tables on startup for dev
  convenience; production needs real migrations before the schema is trusted.
- **Complaints front-end.** The endpoint exists; the actual form (likely the
  GitHub Pages landing page POSTing to `/complaints`) is TODO.
- **Auth on the REST writes.** The HTTP endpoints for keys/board-games are still
  open (the *Discord* mutations are role-gated). Fine on a private network; the REST
  side needs a gate before public exposure.
- **Production move to Oracle Cloud.** Dev/staging runs on the home Debian box; the
  club-owned Oracle instance is the intended production home (config-only migration).
