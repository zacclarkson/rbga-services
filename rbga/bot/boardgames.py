"""Discord CRUD for the board-game inventory — the `/game` command group.

Writes the same `board_games` table the REST API serves (rbga/api/routers/
boardgames.py), via the shared db layer. List/info are open to everyone; add/edit/
remove are gated to the exec role (see rbga/bot/common.py).

Titles aren't unique (e.g. Polyhedral Dice Set ×4), so info/edit/remove take a
numeric id, disambiguated for the user by autocomplete that shows "Title — owner".
"""
from typing import Literal

import discord
from discord import app_commands
from sqlalchemy import func, select

from ..bgg import BGGNotConfigured, extract_bgg_id, fetch_game
from ..db.database import SessionLocal
from ..db.models import BoardGame
from .common import _in_thread, require_exec_role

# Matches the SharePoint condition set imported from the CSV.
Condition = Literal["Like New", "Fair", "Damaged", "Damaged, Missing Pieces"]

MAX_LIST_CHARS = 1900  # keep under Discord's 2000-char message limit

game = app_commands.Group(name="game", description="Manage the board-game inventory")


# --- autocomplete helpers ---------------------------------------------------

async def game_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """Suggest games by title, resolving to their id (disambiguates duplicates)."""
    def query() -> list[tuple[int, str, str | None]]:
        with SessionLocal() as db:
            stmt = select(BoardGame.id, BoardGame.title, BoardGame.owner)
            if current:
                stmt = stmt.where(BoardGame.title.ilike(f"%{current}%"))
            return list(db.execute(stmt.order_by(BoardGame.title).limit(25)).all())

    rows = await _in_thread(query)
    choices = []
    for gid, title, owner in rows:
        label = f"{title} — {owner}" if owner else title
        choices.append(app_commands.Choice(name=label[:100], value=gid))
    return choices


async def owner_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest distinct owners, filtered by what's typed so far."""
    def query() -> list[str]:
        with SessionLocal() as db:
            stmt = select(BoardGame.owner).where(BoardGame.owner.is_not(None)).distinct()
            return sorted(o for (o,) in db.execute(stmt).all() if o)

    owners = await _in_thread(query)
    lowered = current.lower()
    return [
        app_commands.Choice(name=o, value=o) for o in owners if lowered in o.lower()
    ][:25]


# --- read (open to everyone) ------------------------------------------------

@game.command(name="list", description="List board games, optionally filtered")
@app_commands.describe(
    owner="Only show games owned by this person/RBGA",
    condition="Only show games in this condition",
    search="Only show games whose title contains this text",
)
@app_commands.autocomplete(owner=owner_autocomplete)
async def game_list(
    interaction: discord.Interaction,
    owner: str | None = None,
    condition: Condition | None = None,
    search: str | None = None,
):
    await interaction.response.defer()

    def query() -> list[BoardGame]:
        with SessionLocal() as db:
            stmt = select(BoardGame)
            if owner:
                stmt = stmt.where(BoardGame.owner == owner)
            if condition:
                stmt = stmt.where(BoardGame.condition == condition)
            if search:
                stmt = stmt.where(BoardGame.title.ilike(f"%{search}%"))
            return list(db.scalars(stmt.order_by(BoardGame.title)).all())

    games = await _in_thread(query)
    if not games:
        await interaction.followup.send("No board games match that.")
        return

    lines, shown = [], 0
    for g in games:
        bits = [f"**{g.title}**"]
        if g.owner:
            bits.append(f"({g.owner})")
        if g.condition:
            bits.append(f"— {g.condition}")
        line = f"`#{g.id}` " + " ".join(bits)
        if sum(len(x) + 1 for x in lines) + len(line) > MAX_LIST_CHARS:
            break
        lines.append(line)
        shown += 1

    header = f"**{len(games)} game(s)**"
    if shown < len(games):
        header += f" — showing first {shown}, refine with filters"
    await interaction.followup.send(header + "\n" + "\n".join(lines))


@game.command(name="info", description="Show full details for one game")
@app_commands.describe(game="Start typing a title to pick the game")
@app_commands.autocomplete(game=game_autocomplete)
async def game_info(interaction: discord.Interaction, game: int):
    await interaction.response.defer()

    def query() -> BoardGame | None:
        with SessionLocal() as db:
            return db.get(BoardGame, game)

    g = await _in_thread(query)
    if not g:
        await interaction.followup.send("No game with that id.")
        return

    embed = discord.Embed(title=g.title, url=g.bgg_link or None)
    if g.owner:
        embed.add_field(name="Owner", value=g.owner)
    if g.condition:
        embed.add_field(name="Condition", value=g.condition)
    if g.min_players or g.max_players:
        lo, hi = g.min_players, g.max_players
        players = f"{lo}-{hi}" if lo and hi else str(lo or hi)
        embed.add_field(name="Players", value=players)
    if g.publisher:
        embed.add_field(name="Publisher", value=g.publisher)
    if g.price is not None:
        embed.add_field(name="Price", value=f"${g.price:.2f}")
    if g.location:
        embed.add_field(name="Location", value=g.location)
    if g.notes:
        embed.add_field(name="Notes", value=g.notes, inline=False)
    # BGG imports store a real image URL; old CSV rows store a filename we can't render.
    if g.image and g.image.startswith(("http://", "https://")):
        embed.set_image(url=g.image)
    embed.set_footer(text=f"id #{g.id}")
    await interaction.followup.send(embed=embed)


# --- mutations (exec role only) ---------------------------------------------

@game.command(name="add", description="Add a game — paste a BGG link to auto-fill the details")
@app_commands.describe(
    bgg_link="BoardGameGeek URL — pulls title, publisher, players, and image",
    condition="Physical condition",
    price="Purchase value in dollars",
    title="The game's title (optional if a BGG link is given)",
    owner="Who owns it (e.g. RBGA or a member's name)",
    publisher="Publisher (overrides BGG)",
    min_players="Minimum players (overrides BGG)",
    max_players="Maximum players (overrides BGG)",
    location="Where it's stored",
    notes="Anything else worth recording",
)
@app_commands.autocomplete(owner=owner_autocomplete)
@app_commands.check(require_exec_role)
async def game_add(
    interaction: discord.Interaction,
    bgg_link: str | None = None,
    condition: Condition | None = None,
    price: float | None = None,
    title: str | None = None,
    owner: str | None = None,
    publisher: str | None = None,
    min_players: int | None = None,
    max_players: int | None = None,
    location: str | None = None,
    notes: str | None = None,
):
    await interaction.response.defer(ephemeral=True)

    image = None
    # Pull details from BGG when a link is given; explicit args always win.
    if bgg_link:
        bgg_id = extract_bgg_id(bgg_link)
        if bgg_id is None:
            await interaction.followup.send(
                "That doesn't look like a BoardGameGeek link. Paste one like "
                "`https://boardgamegeek.com/boardgame/13/catan`, or give a title instead.",
                ephemeral=True,
            )
            return
        try:
            data = await fetch_game(bgg_id)
        except BGGNotConfigured:
            await interaction.followup.send(
                "BGG lookups aren't set up yet (an admin needs to set `BGG_API_TOKEN`). "
                "For now, add the game manually with a `title`.",
                ephemeral=True,
            )
            return
        except Exception:
            data = None
        if data is None:
            await interaction.followup.send(
                f"Couldn't fetch BGG id {bgg_id} — it may not exist, or BGG is busy. "
                "Try again, or add the game manually with a title.",
                ephemeral=True,
            )
            return
        title = title or data.get("title")
        publisher = publisher or data.get("publisher")
        min_players = min_players if min_players is not None else data.get("min_players")
        max_players = max_players if max_players is not None else data.get("max_players")
        image = data.get("image")

    if not title:
        await interaction.followup.send(
            "Give me a title, or a BGG link to pull one from.", ephemeral=True
        )
        return

    def mutate() -> int:
        with SessionLocal() as db:
            g = BoardGame(
                title=title,
                owner=owner,
                condition=condition,
                price=price,
                bgg_link=bgg_link,
                image=image,
                publisher=publisher,
                min_players=min_players,
                max_players=max_players,
                location=location,
                notes=notes,
            )
            db.add(g)
            db.commit()
            return g.id

    new_id = await _in_thread(mutate)
    await interaction.followup.send(f"Added **{title}** (id #{new_id}).", ephemeral=True)


@game.command(name="edit", description="Edit a game (only the fields you set change)")
@app_commands.describe(
    game="Start typing a title to pick the game",
    title="New title",
    owner="New owner",
    condition="New condition",
    bgg_link="New BoardGameGeek URL",
    publisher="New publisher",
    min_players="New minimum players",
    max_players="New maximum players",
    location="New storage location",
    notes="New notes",
)
@app_commands.autocomplete(game=game_autocomplete, owner=owner_autocomplete)
@app_commands.check(require_exec_role)
async def game_edit(
    interaction: discord.Interaction,
    game: int,
    title: str | None = None,
    owner: str | None = None,
    condition: Condition | None = None,
    bgg_link: str | None = None,
    publisher: str | None = None,
    min_players: int | None = None,
    max_players: int | None = None,
    location: str | None = None,
    notes: str | None = None,
):
    await interaction.response.defer(ephemeral=True)

    # None means "leave unchanged" (fields can't be cleared to null via edit).
    changes = {
        k: v
        for k, v in dict(
            title=title,
            owner=owner,
            condition=condition,
            bgg_link=bgg_link,
            publisher=publisher,
            min_players=min_players,
            max_players=max_players,
            location=location,
            notes=notes,
        ).items()
        if v is not None
    }
    if not changes:
        await interaction.followup.send("Nothing to change — set at least one field.", ephemeral=True)
        return

    def mutate() -> str | None:
        with SessionLocal() as db:
            g = db.get(BoardGame, game)
            if not g:
                return None
            for k, v in changes.items():
                setattr(g, k, v)
            db.commit()
            return g.title

    name = await _in_thread(mutate)
    if name is None:
        await interaction.followup.send("No game with that id.", ephemeral=True)
    else:
        fields = ", ".join(changes)
        await interaction.followup.send(f"Updated **{name}** ({fields}).", ephemeral=True)


@game.command(name="remove", description="Delete a game from the inventory")
@app_commands.describe(game="Start typing a title to pick the game")
@app_commands.autocomplete(game=game_autocomplete)
@app_commands.check(require_exec_role)
async def game_remove(interaction: discord.Interaction, game: int):
    await interaction.response.defer(ephemeral=True)

    def mutate() -> str | None:
        with SessionLocal() as db:
            g = db.get(BoardGame, game)
            if not g:
                return None
            title = g.title
            db.delete(g)
            db.commit()
            return title

    name = await _in_thread(mutate)
    if name is None:
        await interaction.followup.send("No game with that id.", ephemeral=True)
    else:
        await interaction.followup.send(f"Deleted **{name}**.", ephemeral=True)


def setup(tree: app_commands.CommandTree) -> None:
    """Register the /game group on the bot's command tree."""
    tree.add_command(game)
