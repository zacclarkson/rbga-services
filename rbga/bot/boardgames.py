"""Discord CRUD for the board-game inventory: the `/game` command group.

Writes the same `board_games` table the REST API serves (rbga/api/routers/
boardgames.py), via the shared db layer. List/gallery/info/export are open to
everyone; add/edit/remove are gated to the exec role (see rbga/bot/common.py).

Titles aren't unique (e.g. Polyhedral Dice Set ×4), so info/edit/remove take a
numeric id, disambiguated for the user by autocomplete that shows "Title (owner)".
"""
import csv
import io
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


def parse_tags(raw: str | None) -> list[str] | None:
    """Comma-separated input to a clean tag list: " Strategy, Party game " ->
    ["Strategy", "Party game"]. None/blank/only-commas -> None (unset)."""
    if not raw:
        return None
    tags = [t.strip() for t in raw.split(",")]
    return [t for t in tags if t] or None


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
        label = f"{title} ({owner})" if owner else title
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

def _query_games(
    owner: str | None, condition: str | None, search: str | None, tag: str | None
) -> list[BoardGame]:
    """Shared filter query for /game list and /game gallery (runs in a thread)."""
    with SessionLocal() as db:
        stmt = select(BoardGame)
        if owner:
            stmt = stmt.where(BoardGame.owner == owner)
        if condition:
            stmt = stmt.where(BoardGame.condition == condition)
        if search:
            stmt = stmt.where(BoardGame.title.ilike(f"%{search}%"))
        games = list(db.scalars(stmt.order_by(BoardGame.title)).all())
    if tag:
        # JSON column, so filter in Python (portable; the inventory is small).
        wanted = tag.casefold()
        games = [g for g in games if any(t.casefold() == wanted for t in (g.tags or []))]
    return games


@game.command(name="list", description="List board games, optionally filtered")
@app_commands.describe(
    owner="Only show games owned by this person/RBGA",
    condition="Only show games in this condition",
    search="Only show games whose title contains this text",
    tag="Only show games with this tag",
)
@app_commands.autocomplete(owner=owner_autocomplete)
async def game_list(
    interaction: discord.Interaction,
    owner: str | None = None,
    condition: Condition | None = None,
    search: str | None = None,
    tag: str | None = None,
):
    await interaction.response.defer()

    games = await _in_thread(lambda: _query_games(owner, condition, search, tag))
    if not games:
        await interaction.followup.send("No board games match that.")
        return

    pages = list_pages(games)
    if len(pages) == 1:
        await interaction.followup.send(f"**{len(games)} game(s)**\n" + pages[0])
        return
    view = ListView(len(games), pages)
    view.message = await interaction.followup.send(view=view, **view.render())


def _game_line(g: BoardGame) -> str:
    bits = [f"**{g.title}**"]
    if g.owner:
        bits.append(f"({g.owner})")
    if g.condition:
        bits.append(f"[{g.condition}]")
    return f"`#{g.id}` " + " ".join(bits)


def list_pages(games: list[BoardGame]) -> list[str]:
    """Chunk the text list into message-sized pages."""
    pages: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for g in games:
        line = _game_line(g)
        if cur and cur_len + len(line) + 1 > MAX_LIST_CHARS:
            pages.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        pages.append("\n".join(cur))
    return pages or [""]


# --- gallery (embed cards with images, paginated) ----------------------------

GALLERY_PAGE = 10  # Discord's embeds-per-message cap


def gallery_pages(total: int) -> int:
    """How many pages a gallery of `total` games needs."""
    return max(1, (total + GALLERY_PAGE - 1) // GALLERY_PAGE)


def game_card(g: BoardGame) -> discord.Embed:
    """A compact embed card: details in the body, the game's image as a
    thumbnail. Prefers the small BGG thumbnail variant (Discord's proxy times
    out on multi-MB originals when a page shows ten at once); only real URLs
    are used (CSV rows may hold bare filenames)."""
    bits = []
    if g.owner:
        bits.append(f"Owner: {g.owner}")
    if g.condition:
        bits.append(f"Condition: {g.condition}")
    if g.min_players or g.max_players:
        lo, hi = g.min_players, g.max_players
        bits.append("Players: " + (f"{lo}-{hi}" if lo and hi else str(lo or hi)))
    if g.tags:
        bits.append("Tags: " + ", ".join(g.tags[:6]))
    e = discord.Embed(
        title=f"#{g.id} {g.title}"[:256],
        url=g.bgg_link or None,
        description="\n".join(bits) or None,
    )
    small = g.thumbnail or g.image
    if small and small.startswith(("http://", "https://")):
        e.set_thumbnail(url=small)
    return e


def gallery_page_embeds(games: list[BoardGame], page: int) -> list[discord.Embed]:
    start = page * GALLERY_PAGE
    return [game_card(g) for g in games[start : start + GALLERY_PAGE]]


def _gallery_header(total: int, page: int) -> str:
    return f"**{total} game(s)**, page {page + 1}/{gallery_pages(total)}"


class _Pager(discord.ui.View):
    """Prev/Next pager over a fixed page count. Transient by design (the game
    list is held in memory): buttons stop working after the timeout or a bot
    restart, and the row is removed on timeout. Just run the command again.
    Subclasses implement render() with the message kwargs for the page."""

    def __init__(self, page_count: int) -> None:
        super().__init__(timeout=300)
        self.page = 0
        self.page_count = page_count
        self.message: discord.Message | None = None
        self._sync_buttons()

    def render(self) -> dict:
        """content=/embeds= kwargs for the current page."""
        raise NotImplementedError

    def _sync_buttons(self) -> None:
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.page_count - 1

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(view=self, **self.render())

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(self.page - 1, 0)
        await self._show(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.page + 1, self.page_count - 1)
        await self._show(interaction)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass  # message may have been deleted


class GalleryView(_Pager):
    """Pager rendering embed cards with images."""

    def __init__(self, games: list[BoardGame]) -> None:
        self.games = games
        super().__init__(gallery_pages(len(games)))

    def render(self) -> dict:
        return {
            "content": _gallery_header(len(self.games), self.page),
            "embeds": gallery_page_embeds(self.games, self.page),
        }


class ListView(_Pager):
    """Pager rendering the compact text list."""

    def __init__(self, total: int, pages: list[str]) -> None:
        self.total = total
        self.text_pages = pages
        super().__init__(len(pages))

    def render(self) -> dict:
        header = f"**{self.total} game(s)**, page {self.page + 1}/{self.page_count}"
        return {"content": header + "\n" + self.text_pages[self.page]}


@game.command(name="gallery", description="Browse games as image cards, 10 per page")
@app_commands.describe(
    owner="Only show games owned by this person/RBGA",
    condition="Only show games in this condition",
    search="Only show games whose title contains this text",
    tag="Only show games with this tag",
)
@app_commands.autocomplete(owner=owner_autocomplete)
async def game_gallery(
    interaction: discord.Interaction,
    owner: str | None = None,
    condition: Condition | None = None,
    search: str | None = None,
    tag: str | None = None,
):
    await interaction.response.defer()

    games = await _in_thread(lambda: _query_games(owner, condition, search, tag))
    if not games:
        await interaction.followup.send("No board games match that.")
        return

    view = GalleryView(games)
    view.message = await interaction.followup.send(view=view, **view.render())


# --- export (the whole inventory in one file) ---------------------------------

_EXPORT_FIELDS = [
    "id", "title", "owner", "condition", "price", "location",
    "publisher", "min_players", "max_players", "tags", "bgg_link", "notes",
]


def export_csv(games: list[BoardGame]) -> str:
    """The inventory as CSV text, one row per game (tags joined with '; ')."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_EXPORT_FIELDS)
    for g in games:
        row = [getattr(g, f) for f in _EXPORT_FIELDS]
        row[_EXPORT_FIELDS.index("tags")] = "; ".join(g.tags or [])
        writer.writerow(row)
    return buf.getvalue()


@game.command(name="export", description="Download the whole inventory as a CSV file")
@app_commands.describe(
    owner="Only include games owned by this person/RBGA",
    condition="Only include games in this condition",
    search="Only include games whose title contains this text",
    tag="Only include games with this tag",
)
@app_commands.autocomplete(owner=owner_autocomplete)
async def game_export(
    interaction: discord.Interaction,
    owner: str | None = None,
    condition: Condition | None = None,
    search: str | None = None,
    tag: str | None = None,
):
    await interaction.response.defer()

    games = await _in_thread(lambda: _query_games(owner, condition, search, tag))
    if not games:
        await interaction.followup.send("No board games match that.")
        return

    # utf-8-sig so Excel detects the encoding (titles have accents, ×, etc.).
    payload = io.BytesIO(export_csv(games).encode("utf-8-sig"))
    await interaction.followup.send(
        f"**{len(games)} game(s)**, the full set in one file:",
        file=discord.File(payload, filename="rbga-board-games.csv"),
    )


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
    if g.tags:
        embed.add_field(name="Tags", value=", ".join(g.tags), inline=False)
    if g.notes:
        embed.add_field(name="Notes", value=g.notes, inline=False)
    # BGG imports store a real image URL; old CSV rows store a filename we can't render.
    if g.image and g.image.startswith(("http://", "https://")):
        embed.set_image(url=g.image)
    embed.set_footer(text=f"id #{g.id}")
    await interaction.followup.send(embed=embed)


# --- mutations (exec role only) ---------------------------------------------

@game.command(name="add", description="Add a game (paste a BGG link to auto-fill the details)")
@app_commands.describe(
    bgg_link="BoardGameGeek URL; pulls title, publisher, players, and image",
    condition="Physical condition",
    price="Purchase value in dollars",
    title="The game's title (optional if a BGG link is given)",
    owner="Who owns it (e.g. RBGA or a member's name)",
    publisher="Publisher (overrides BGG)",
    min_players="Minimum players (overrides BGG)",
    max_players="Maximum players (overrides BGG)",
    location="Where it's stored",
    notes="Anything else worth recording",
    tags="Comma-separated tags (auto-filled from BGG categories if omitted)",
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
    tags: str | None = None,
):
    await interaction.response.defer(ephemeral=True)

    tag_list = parse_tags(tags)
    image = thumbnail = None
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
                f"Couldn't fetch BGG id {bgg_id}. It may not exist, or BGG is busy. "
                "Try again, or add the game manually with a title.",
                ephemeral=True,
            )
            return
        title = title or data.get("title")
        publisher = publisher or data.get("publisher")
        min_players = min_players if min_players is not None else data.get("min_players")
        max_players = max_players if max_players is not None else data.get("max_players")
        image = data.get("image")
        thumbnail = data.get("thumbnail")
        tag_list = tag_list or data.get("tags")

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
                thumbnail=thumbnail,
                publisher=publisher,
                min_players=min_players,
                max_players=max_players,
                location=location,
                notes=notes,
                tags=tag_list,
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
    tags="New comma-separated tags (replaces the existing set)",
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
    tags: str | None = None,
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
            tags=parse_tags(tags),
        ).items()
        if v is not None
    }
    if not changes:
        await interaction.followup.send("Nothing to change; set at least one field.", ephemeral=True)
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
