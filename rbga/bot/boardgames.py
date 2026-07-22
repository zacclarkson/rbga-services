"""Discord CRUD for the board-game inventory: the `/game` command group.

Writes the same `board_games` table the REST API serves (rbga/api/routers/
boardgames.py), via the shared db layer. List/gallery/info are open to
everyone; add/edit/remove/export are gated to the exec role (see
rbga/bot/common.py) — export because the CSV carries prices and owner names.

Titles aren't unique (e.g. Polyhedral Dice Set ×4), so info/edit/remove take a
numeric id, disambiguated for the user by autocomplete that shows "#id Title".
"""
import csv
import io
from datetime import datetime
from typing import Literal, get_args

import discord
from discord import app_commands
from sqlalchemy import func, select, update

from ..bgg import BGGNotConfigured, extract_bgg_id, fetch_game
from ..db.database import SessionLocal
from ..db.models import BoardGame, Owner
from .common import _in_thread, require_exec_role

# Matches the SharePoint condition set imported from the CSV.
Condition = Literal["Like New", "Fair", "Damaged", "Damaged, Missing Pieces"]

MAX_LIST_CHARS = 1900  # keep under Discord's 2000-char message limit

game = app_commands.Group(name="game", description="Manage the board-game inventory")


# Resale factor per condition, applied to the purchase price when no manual
# sell_price is set. An exec decision: tweak the numbers here.
_CONDITION_FACTOR = {
    "Like New": 0.7,
    "Fair": 0.4,
    "Damaged": 0.15,
    "Damaged, Missing Pieces": 0.05,
}
_UNKNOWN_CONDITION_FACTOR = 0.5


def estimate_sell_price(price: float | None, condition: str | None) -> float | None:
    """Estimated resale value from purchase price x condition factor.
    None when there is no purchase price to estimate from."""
    if price is None:
        return None
    return round(price * _CONDITION_FACTOR.get(condition or "", _UNKNOWN_CONDITION_FACTOR), 2)


def sell_price_display(g: BoardGame) -> str | None:
    """The asking price if set, else the estimate marked as such."""
    if g.sell_price is not None:
        return f"${g.sell_price:.2f}"
    est = estimate_sell_price(g.price, g.condition)
    return f"~${est:.2f} (est.)" if est is not None else None


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
    """Suggest games by title, resolving to their id. The label leads with the
    id so duplicate copies of the same title are distinguishable."""
    def query() -> list[tuple[int, str]]:
        with SessionLocal() as db:
            stmt = select(BoardGame.id, BoardGame.title)
            if current:
                stmt = stmt.where(BoardGame.title.ilike(f"%{current}%"))
            return list(db.execute(stmt.order_by(BoardGame.title).limit(25)).all())

    rows = await _in_thread(query)
    return [
        app_commands.Choice(name=f"#{gid} {title}"[:100], value=gid)
        for gid, title in rows
    ]


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


async def location_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest distinct existing locations, filtered by what's typed. De-duped
    case-insensitively so a lingering 'city'/'City' split shows once; typing a
    brand-new location is still allowed (it's a free-text option)."""
    def query() -> list[str]:
        with SessionLocal() as db:
            stmt = select(BoardGame.location).where(BoardGame.location.is_not(None)).distinct()
            return sorted(l for (l,) in db.execute(stmt).all() if l)

    locs = await _in_thread(query)
    lowered = current.lower()
    seen: set[str] = set()
    out: list[app_commands.Choice[str]] = []
    for l in locs:
        if lowered in l.lower() and l.casefold() not in seen:
            seen.add(l.casefold())
            out.append(app_commands.Choice(name=l, value=l))
    return out[:25]


# --- read (open to everyone) ------------------------------------------------

def _query_games(
    owner: str | None,
    condition: str | None,
    search: str | None,
    tag: str | None,
    location: str | None = None,
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
        if location:
            # Case-insensitive so it's robust against any pre-canonicalization dupes.
            stmt = stmt.where(func.lower(BoardGame.location) == location.lower())
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
    location="Only show games stored at this location",
)
@app_commands.autocomplete(owner=owner_autocomplete, location=location_autocomplete)
async def game_list(
    interaction: discord.Interaction,
    owner: str | None = None,
    condition: Condition | None = None,
    search: str | None = None,
    tag: str | None = None,
    location: str | None = None,
):
    await interaction.response.defer()

    games = await _in_thread(lambda: _query_games(owner, condition, search, tag, location))
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
    if g.missing:
        bits.append("⚠ MISSING")
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
    location="Only show games stored at this location",
)
@app_commands.autocomplete(owner=owner_autocomplete, location=location_autocomplete)
async def game_gallery(
    interaction: discord.Interaction,
    owner: str | None = None,
    condition: Condition | None = None,
    search: str | None = None,
    tag: str | None = None,
    location: str | None = None,
):
    await interaction.response.defer()

    games = await _in_thread(lambda: _query_games(owner, condition, search, tag, location))
    if not games:
        await interaction.followup.send("No board games match that.")
        return

    view = GalleryView(games)
    view.message = await interaction.followup.send(view=view, **view.render())


# --- export (the whole inventory in one file) ---------------------------------

_EXPORT_FIELDS = [
    "id", "title", "owner", "condition", "price", "sell_price", "sell_estimate",
    "missing", "last_seen_at", "location",
    "publisher", "min_players", "max_players", "tags", "bgg_link", "notes",
]


def export_csv(games: list[BoardGame]) -> str:
    """The inventory as CSV text, one row per game (tags joined with '; ').
    sell_estimate is the computed condition-based value; sell_price is the
    manual asking price when an exec has set one."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_EXPORT_FIELDS)
    for g in games:
        computed = {
            "tags": "; ".join(g.tags or []),
            "sell_estimate": estimate_sell_price(g.price, g.condition),
            "missing": "yes" if g.missing else "",
            "last_seen_at": f"{g.last_seen_at:%Y-%m-%d}" if g.last_seen_at else "",
        }
        writer.writerow(
            [computed[f] if f in computed else getattr(g, f) for f in _EXPORT_FIELDS]
        )
    return buf.getvalue()


@game.command(name="export", description="Download the whole inventory as a CSV file")
@app_commands.describe(
    owner="Only include games owned by this person/RBGA",
    condition="Only include games in this condition",
    search="Only include games whose title contains this text",
    tag="Only include games with this tag",
    location="Only include games stored at this location",
)
@app_commands.autocomplete(owner=owner_autocomplete, location=location_autocomplete)
@app_commands.check(require_exec_role)
async def game_export(
    interaction: discord.Interaction,
    owner: str | None = None,
    condition: Condition | None = None,
    search: str | None = None,
    tag: str | None = None,
    location: str | None = None,
):
    await interaction.response.defer()

    games = await _in_thread(lambda: _query_games(owner, condition, search, tag, location))
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
    sell = sell_price_display(g)
    if sell:
        embed.add_field(name="Sell", value=sell)
    if g.location:
        embed.add_field(name="Location", value=g.location)
    if g.missing:
        embed.add_field(name="Stocktake", value="⚠ MISSING")
    elif g.last_seen_at:
        embed.add_field(name="Stocktake", value=f"Last seen {g.last_seen_at:%Y-%m-%d}")
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

def parse_money(raw: str | None) -> float | None:
    """'$45' / '45.50' -> float; blank/None -> None. Raises ValueError on junk."""
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw.strip().lstrip("$"))
    except ValueError:
        raise ValueError(f"'{raw.strip()}' isn't a price.")


async def _bgg_autofill(bgg_link: str) -> tuple[dict | None, str | None]:
    """Fetch the BGG record for a link. Returns (data, None) or (None, error
    message to show the user)."""
    bgg_id = extract_bgg_id(bgg_link)
    if bgg_id is None:
        return None, (
            "That doesn't look like a BoardGameGeek link. Paste one like "
            "`https://boardgamegeek.com/boardgame/13/catan`, or give a title instead."
        )
    try:
        data = await fetch_game(bgg_id)
    except BGGNotConfigured:
        return None, (
            "BGG lookups aren't set up yet (an admin needs to set `BGG_API_TOKEN`). "
            "For now, add the game manually with a `title`."
        )
    except Exception:
        data = None
    if data is None:
        return None, (
            f"Couldn't fetch BGG id {bgg_id}. It may not exist, or BGG is busy. "
            "Try again, or add the game manually with a title."
        )
    return data, None


# --- donor-contact bookkeeping -------------------------------------------------
# Owners are donors/lenders: the contact exists so the club can reach them when
# they may want a game back. These prompts keep board_games.owner (free-text
# name) and the owners table in sync without a hard FK.

def owner_game_count(name: str) -> int:
    with SessionLocal() as db:
        return (
            db.scalar(
                select(func.count()).select_from(BoardGame).where(BoardGame.owner == name)
            )
            or 0
        )


def owner_contact_exists(name: str) -> bool:
    with SessionLocal() as db:
        return db.scalar(select(Owner.id).where(Owner.name == name)) is not None


def delete_owner_contact(name: str) -> bool:
    """Remove an owner's contact row; False when there was none."""
    with SessionLocal() as db:
        row = db.scalar(select(Owner).where(Owner.name == name))
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True


class RemoveOwnerContactView(discord.ui.View):
    """Confirm dropping the contact of an owner with no games left. Ephemeral
    and transient (same lifecycle as the pager views): after the timeout just
    use /owner list + a manual check instead."""

    def __init__(self, owner_name: str) -> None:
        super().__init__(timeout=300)
        self.owner_name = owner_name

    @discord.ui.button(label="Remove contact", style=discord.ButtonStyle.danger)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        removed = await _in_thread(lambda: delete_owner_contact(self.owner_name))
        self.stop()
        msg = (
            f"Removed the saved contact for **{self.owner_name}**."
            if removed
            else f"**{self.owner_name}** had no saved contact anymore."
        )
        await interaction.response.edit_message(content=msg, view=None)

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary)
    async def keep_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            content=f"Kept **{self.owner_name}**'s contact.", view=None
        )


class AddOwnerContactModal(discord.ui.Modal):
    """One-field form saving how to reach a new game owner."""

    def __init__(self, owner_name: str) -> None:
        super().__init__(title=f"Contact for {owner_name}"[:45])
        self.owner_name = owner_name
        self.contact = discord.ui.TextInput(
            label="How to reach them",
            placeholder="Discord handle, email, phone",
            max_length=256,
        )
        self.add_item(self.contact)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        contact = self.contact.value.strip()
        await _in_thread(lambda: set_owner_contact(self.owner_name, contact))
        await interaction.response.send_message(
            f"Saved contact for **{self.owner_name}**.", ephemeral=True
        )


class AddOwnerContactView(discord.ui.View):
    """Button opening the contact form (modals can't be sent unprompted)."""

    def __init__(self, owner_name: str) -> None:
        super().__init__(timeout=300)
        self.owner_name = owner_name

    @discord.ui.button(label="Add contact", style=discord.ButtonStyle.primary)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AddOwnerContactModal(self.owner_name))


async def prompt_owner_contact_upkeep(
    interaction: discord.Interaction,
    old_owner: str | None = None,
    new_owner: str | None = None,
) -> None:
    """After a game mutation, nudge the exec to keep donor contacts in sync.

    * old_owner left with zero games and a saved contact -> confirm removing it.
    * new_owner whose only game is the one just written and who has no contact
      -> offer to save one. The only-game condition keeps contact-less bulk
      owners (RBGA itself) from nagging on every add.
    Both messages are ephemeral follow-ups to an already-deferred interaction.
    """
    if old_owner and old_owner != new_owner:
        if await _in_thread(lambda: owner_game_count(old_owner)) == 0 and await _in_thread(
            lambda: owner_contact_exists(old_owner)
        ):
            await interaction.followup.send(
                f"**{old_owner}** no longer owns any games in the inventory. "
                "Remove their saved contact, or keep it in case they lend again?",
                view=RemoveOwnerContactView(old_owner),
                ephemeral=True,
            )
    if new_owner and new_owner != old_owner:
        if await _in_thread(lambda: owner_game_count(new_owner)) == 1 and not await _in_thread(
            lambda: owner_contact_exists(new_owner)
        ):
            await interaction.followup.send(
                f"**{new_owner}** is a new owner with no saved contact. Add one so "
                "the club can reach them about their game?",
                view=AddOwnerContactView(new_owner),
                ephemeral=True,
            )


# Fields refreshed from BGG when an edit changes the link. Title is deliberately
# excluded: existing titles may be hand-customised (e.g. Polyhedral Dice Set ×4).
_BGG_REFRESH_FIELDS = ("publisher", "min_players", "max_players", "image", "thumbnail", "tags")


def merge_bgg_refresh(changes: dict, data: dict) -> dict:
    """Fold fetched BGG data into an edit's changes; explicit args win, and
    fields BGG has nothing for keep their stored value."""
    for k in _BGG_REFRESH_FIELDS:
        if k not in changes and data.get(k) is not None:
            changes[k] = data[k]
    return changes


def canonical_location(raw: str | None) -> str | None:
    """Trim input and reuse an existing location's spelling when it matches
    case-insensitively, so 'city' is stored as the existing 'City'. A brand-new
    location is kept as typed; blank/None -> None. Runs its own session, so call
    it inside an _in_thread closure."""
    if raw is None or not raw.strip():
        return None
    val = raw.strip()
    with SessionLocal() as db:
        existing = db.scalars(
            select(BoardGame.location).where(BoardGame.location.is_not(None)).distinct()
        ).all()
    for loc in existing:
        if loc and loc.casefold() == val.casefold():
            return loc
    return val


def _insert_game(**fields) -> int:
    """Create a BoardGame row (runs in a thread); returns the new id."""
    with SessionLocal() as db:
        g = BoardGame(**fields)
        db.add(g)
        db.commit()
        return g.id


def _apply_changes(gid: int, changes: dict) -> str | None:
    """Apply field changes to a game (runs in a thread). Returns its title,
    or None when no such game exists."""
    with SessionLocal() as db:
        g = db.get(BoardGame, gid)
        if not g:
            return None
        for k, v in changes.items():
            setattr(g, k, v)
        db.commit()
        return g.title


_CONDITION_HINT = "Like New / Fair / Damaged / Damaged, Missing Pieces"


class AddGameModal(discord.ui.Modal, title="Add a board game"):
    """Quick-add form: the five fields that matter most (Discord caps modals
    at 5 inputs). A BGG link auto-fills publisher, players, image, and tags;
    the long-tail fields remain as options on /game add."""

    bgg_link = discord.ui.TextInput(
        label="BGG link (auto-fills details)",
        required=False,
        placeholder="https://boardgamegeek.com/boardgame/13/catan",
        max_length=512,
    )
    game_title = discord.ui.TextInput(
        label="Title (optional if a BGG link is given)", required=False, max_length=200
    )
    owner = discord.ui.TextInput(
        label="Owner", required=False, placeholder="RBGA or a member's name", max_length=128
    )
    condition = discord.ui.TextInput(
        label="Condition", required=False, placeholder=_CONDITION_HINT, max_length=64
    )
    price = discord.ui.TextInput(
        label="Purchase price (dollars)", required=False, placeholder="45.00", max_length=16
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            price = parse_money(self.price.value)
        except ValueError as e:
            await interaction.followup.send(f"{e} Nothing was added.", ephemeral=True)
            return

        link = self.bgg_link.value.strip() or None
        fields: dict = dict(
            title=self.game_title.value.strip() or None,
            owner=self.owner.value.strip() or None,
            condition=self.condition.value.strip() or None,
            price=price,
            bgg_link=link,
        )
        if link:
            data, err = await _bgg_autofill(link)
            if err:
                await interaction.followup.send(err, ephemeral=True)
                return
            fields["title"] = fields["title"] or data.get("title")
            fields["publisher"] = data.get("publisher")
            fields["min_players"] = data.get("min_players")
            fields["max_players"] = data.get("max_players")
            fields["image"] = data.get("image")
            fields["thumbnail"] = data.get("thumbnail")
            fields["tags"] = data.get("tags")
        if not fields["title"]:
            await interaction.followup.send(
                "Give a title, or a BGG link to pull one from.", ephemeral=True
            )
            return
        new_id = await _in_thread(lambda: _insert_game(**fields))
        await interaction.followup.send(
            f"Added **{fields['title']}** (id #{new_id}).", ephemeral=True
        )
        await prompt_owner_contact_upkeep(interaction, new_owner=fields["owner"])

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        msg = "Sorry, something went wrong adding that. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        print(f"[boardgames] add-form error: {error!r}")


class EditGameModal(discord.ui.Modal):
    """Pre-filled edit form for the five most-edited fields. Clearing a field
    leaves it unchanged (edits can't null a field, same as the slash options);
    the long-tail fields remain as options on /game edit."""

    def __init__(self, g: BoardGame) -> None:
        super().__init__(title=f"Edit #{g.id} {g.title}"[:45])
        self.gid = g.id
        self.game_title = discord.ui.TextInput(
            label="Title", default=g.title, required=False, max_length=200
        )
        self.owner = discord.ui.TextInput(
            label="Owner", default=g.owner or "", required=False, max_length=128
        )
        self.condition = discord.ui.TextInput(
            label="Condition",
            default=g.condition or "",
            required=False,
            placeholder=_CONDITION_HINT,
            max_length=64,
        )
        self.price = discord.ui.TextInput(
            label="Purchase price (dollars)",
            default="" if g.price is None else f"{g.price:g}",
            required=False,
            max_length=16,
        )
        self.sell_price = discord.ui.TextInput(
            label="Sell price (dollars)",
            default="" if g.sell_price is None else f"{g.sell_price:g}",
            required=False,
            max_length=16,
        )
        for item in (self.game_title, self.owner, self.condition, self.price, self.sell_price):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            changes = dict(
                title=self.game_title.value.strip() or None,
                owner=self.owner.value.strip() or None,
                condition=self.condition.value.strip() or None,
                price=parse_money(self.price.value),
                sell_price=parse_money(self.sell_price.value),
            )
        except ValueError as e:
            await interaction.followup.send(f"{e} Nothing was changed.", ephemeral=True)
            return
        changes = {k: v for k, v in changes.items() if v is not None}
        if not changes:
            await interaction.followup.send("Nothing to change.", ephemeral=True)
            return
        old_owner = None
        if "owner" in changes:
            g = await _in_thread(lambda: _get_game(self.gid))
            old_owner = g.owner if g else None
        name = await _in_thread(lambda: _apply_changes(self.gid, changes))
        if name is None:
            await interaction.followup.send("No game with that id.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"Updated **{name}** ({', '.join(changes)}).", ephemeral=True
            )
            if "owner" in changes:
                await prompt_owner_contact_upkeep(
                    interaction, old_owner=old_owner, new_owner=changes["owner"]
                )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        msg = "Sorry, something went wrong saving that. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        print(f"[boardgames] edit-form error: {error!r}")


@game.command(name="add", description="Add a game (run with no options for a form; a BGG link auto-fills)")
@app_commands.describe(
    bgg_link="BoardGameGeek URL; pulls title, publisher, players, and image",
    condition="Physical condition",
    price="Purchase value in dollars",
    sell_price="Asking price in dollars (estimated from price+condition if unset)",
    title="The game's title (optional if a BGG link is given)",
    owner="Who owns it (e.g. RBGA or a member's name)",
    publisher="Publisher (overrides BGG)",
    min_players="Minimum players (overrides BGG)",
    max_players="Maximum players (overrides BGG)",
    location="Where it's stored",
    notes="Anything else worth recording",
    tags="Comma-separated tags (auto-filled from BGG categories if omitted)",
)
@app_commands.autocomplete(owner=owner_autocomplete, location=location_autocomplete)
@app_commands.check(require_exec_role)
async def game_add(
    interaction: discord.Interaction,
    bgg_link: str | None = None,
    condition: Condition | None = None,
    price: float | None = None,
    sell_price: float | None = None,
    title: str | None = None,
    owner: str | None = None,
    publisher: str | None = None,
    min_players: int | None = None,
    max_players: int | None = None,
    location: str | None = None,
    notes: str | None = None,
    tags: str | None = None,
):
    # Bare /game add opens the quick-add form instead.
    if not any(
        v is not None
        for v in (bgg_link, condition, price, sell_price, title, owner, publisher,
                  min_players, max_players, location, notes, tags)
    ):
        await interaction.response.send_modal(AddGameModal())
        return

    await interaction.response.defer(ephemeral=True)

    tag_list = parse_tags(tags)
    image = thumbnail = None
    # Pull details from BGG when a link is given; explicit args always win.
    if bgg_link:
        data, err = await _bgg_autofill(bgg_link)
        if err:
            await interaction.followup.send(err, ephemeral=True)
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

    new_id = await _in_thread(
        lambda: _insert_game(
            title=title,
            owner=owner,
            condition=condition,
            price=price,
            sell_price=sell_price,
            bgg_link=bgg_link,
            image=image,
            thumbnail=thumbnail,
            publisher=publisher,
            min_players=min_players,
            max_players=max_players,
            location=canonical_location(location),
            notes=notes,
            tags=tag_list,
        )
    )
    await interaction.followup.send(f"Added **{title}** (id #{new_id}).", ephemeral=True)
    await prompt_owner_contact_upkeep(interaction, new_owner=owner)


@game.command(name="edit", description="Edit a game (pick it alone for a form; a new bgg_link refreshes details)")
@app_commands.describe(
    game="Start typing a title to pick the game",
    title="New title",
    owner="New owner",
    condition="New condition",
    bgg_link="New BoardGameGeek URL; re-fetches publisher, players, image, and tags",
    publisher="New publisher",
    min_players="New minimum players",
    max_players="New maximum players",
    location="New storage location",
    notes="New notes",
    tags="New comma-separated tags (replaces the existing set)",
    price="New purchase value in dollars",
    sell_price="New asking price in dollars",
)
@app_commands.autocomplete(
    game=game_autocomplete, owner=owner_autocomplete, location=location_autocomplete
)
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
    price: float | None = None,
    sell_price: float | None = None,
):
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
            price=price,
            sell_price=sell_price,
        ).items()
        if v is not None
    }

    # Just the game picked, no fields: open the pre-filled edit form.
    if not changes:
        g = await _in_thread(lambda: _get_game(game))
        if not g:
            await interaction.response.send_message("No game with that id.", ephemeral=True)
            return
        await interaction.response.send_modal(EditGameModal(g))
        return

    await interaction.response.defer(ephemeral=True)

    # A changed link means the stored auto-filled details describe the old
    # game: re-fetch BGG and refresh them (explicit args above still win).
    warn = ""
    if "bgg_link" in changes:
        bgg_id = extract_bgg_id(changes["bgg_link"])
        if bgg_id is None:
            await interaction.followup.send(
                "That doesn't look like a BoardGameGeek link. Paste one like "
                "`https://boardgamegeek.com/boardgame/13/catan`. Nothing was changed.",
                ephemeral=True,
            )
            return
        try:
            data = await fetch_game(bgg_id)
        except BGGNotConfigured:
            data = None
            warn = " Link saved, but details weren't refreshed: `BGG_API_TOKEN` is unset."
        except Exception:
            data = None
        if data:
            merge_bgg_refresh(changes, data)
        elif not warn:
            await interaction.followup.send(
                f"Couldn't fetch BGG id {bgg_id}. It may not exist, or BGG is busy. "
                "Try again. Nothing was changed.",
                ephemeral=True,
            )
            return

    # Reuse an existing location's spelling so 'city' doesn't fork from 'City'.
    if "location" in changes:
        changes["location"] = await _in_thread(
            lambda: canonical_location(changes["location"])
        )

    # Reassigning the owner may orphan the old one's contact; snapshot it first.
    old_owner = None
    if "owner" in changes:
        g = await _in_thread(lambda: _get_game(game))
        old_owner = g.owner if g else None

    name = await _in_thread(lambda: _apply_changes(game, changes))
    if name is None:
        await interaction.followup.send("No game with that id.", ephemeral=True)
    else:
        fields = ", ".join(changes)
        await interaction.followup.send(f"Updated **{name}** ({fields}).{warn}", ephemeral=True)
        if "owner" in changes:
            await prompt_owner_contact_upkeep(
                interaction, old_owner=old_owner, new_owner=changes["owner"]
            )


# --- bulk edit (exec only) -----------------------------------------------------

def _bulk_apply(ids: list[int], changes: dict) -> int:
    """Set the same field(s) on many games in one statement (runs in a thread).
    Returns how many rows were updated."""
    with SessionLocal() as db:
        result = db.execute(
            update(BoardGame).where(BoardGame.id.in_(ids)).values(**changes)
        )
        db.commit()
        return result.rowcount


def _summarise_changes(changes: dict) -> str:
    """'location → Bundoora, owner → RBGA' for a confirm/echo message."""
    return ", ".join(f"{k} → {v}" for k, v in changes.items())


class BulkEditConfirmView(discord.ui.View):
    """Confirm a bulk field change before it's written. Ephemeral and transient
    (same lifecycle as the other views here): after the 5-minute timeout, just
    run /game bulk-edit again. Any exec may confirm (interaction_check)."""

    def __init__(self, game_ids: list[int], changes: dict) -> None:
        super().__init__(timeout=300)
        self.game_ids = game_ids
        self.changes = changes
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if require_exec_role(interaction):
            return True
        await interaction.response.send_message(
            "Only execs can confirm a bulk edit.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        n = await _in_thread(lambda: _bulk_apply(self.game_ids, self.changes))
        self.stop()
        await interaction.response.edit_message(
            content=f"Updated **{n} game(s)** ({_summarise_changes(self.changes)}).",
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Nothing changed.", view=None)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(
                    content="Bulk edit timed out; nothing changed. Run /game bulk-edit again.",
                    view=None,
                )
            except discord.HTTPException:
                pass


@game.command(name="bulk-edit", description="Change a field on many games at once (filter, then set)")
@app_commands.describe(
    owner="Filter: only games owned by this person/RBGA",
    condition="Filter: only games in this condition",
    location="Filter: only games stored here",
    tag="Filter: only games with this tag",
    search="Filter: only games whose title contains this text",
    set_location="New location for every matched game",
    set_owner="New owner for every matched game",
    set_condition="New condition for every matched game",
)
@app_commands.autocomplete(
    owner=owner_autocomplete,
    location=location_autocomplete,
    set_owner=owner_autocomplete,
    set_location=location_autocomplete,
)
@app_commands.check(require_exec_role)
async def game_bulk_edit(
    interaction: discord.Interaction,
    owner: str | None = None,
    condition: Condition | None = None,
    location: str | None = None,
    tag: str | None = None,
    search: str | None = None,
    set_location: str | None = None,
    set_owner: str | None = None,
    set_condition: Condition | None = None,
):
    await interaction.response.defer(ephemeral=True)

    # What to change. location is canonicalised so a bulk move reuses an
    # existing spelling ('city' -> 'City') like /game add and /game edit.
    changes: dict = {}
    if set_location is not None:
        changes["location"] = await _in_thread(lambda: canonical_location(set_location))
    if set_owner is not None:
        changes["owner"] = set_owner
    if set_condition is not None:
        changes["condition"] = set_condition
    if not changes:
        await interaction.followup.send(
            "Tell me what to change: set at least one of set_location, set_owner, "
            "or set_condition.",
            ephemeral=True,
        )
        return

    games = await _in_thread(lambda: _query_games(owner, condition, search, tag, location))
    if not games:
        await interaction.followup.send("No games match that.", ephemeral=True)
        return

    ids = [g.id for g in games]
    sample = ", ".join(g.title for g in games[:5])
    if len(games) > 5:
        sample += ", …"
    note = (
        "\n\nNote: owner contacts aren't auto-checked on a bulk change; "
        "tidy them with /owner if needed."
        if "owner" in changes
        else ""
    )
    view = BulkEditConfirmView(ids, changes)
    view.message = await interaction.followup.send(
        f"Update **{len(games)} game(s)** ({_summarise_changes(changes)})?\n"
        f"_{sample}_{note}",
        view=view,
        ephemeral=True,
    )


@game.command(name="remove", description="Delete a game from the inventory")
@app_commands.describe(game="Start typing a title to pick the game")
@app_commands.autocomplete(game=game_autocomplete)
@app_commands.check(require_exec_role)
async def game_remove(interaction: discord.Interaction, game: int):
    await interaction.response.defer(ephemeral=True)

    def mutate() -> tuple[str, str | None] | None:
        with SessionLocal() as db:
            g = db.get(BoardGame, game)
            if not g:
                return None
            title, owner = g.title, g.owner
            db.delete(g)
            db.commit()
            return title, owner

    deleted = await _in_thread(mutate)
    if deleted is None:
        await interaction.followup.send("No game with that id.", ephemeral=True)
    else:
        name, owner = deleted
        await interaction.followup.send(f"Deleted **{name}**.", ephemeral=True)
        await prompt_owner_contact_upkeep(interaction, old_owner=owner)


# --- stocktake (exec only) -----------------------------------------------------

class StocktakeView(discord.ui.View):
    """One-game-at-a-time checklist: Seen / Missing / Skip. Any exec can press
    the buttons (interaction_check), so a stocktake can be shared. Transient:
    it times out after 15 minutes; progress already recorded is kept, and
    running /game stocktake again resumes (sorted by id)."""

    def __init__(self, game_ids: list[int]) -> None:
        super().__init__(timeout=900)
        self.game_ids = game_ids
        self.idx = 0
        self.seen = 0
        self.missing = 0
        self.skipped = 0
        self.recondition = 0
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if require_exec_role(interaction):
            return True
        await interaction.response.send_message(
            "Only execs can run the stocktake.", ephemeral=True
        )
        return False

    def _summary(self) -> str:
        return (
            f"**Stocktake done:** {self.seen} seen, {self.missing} missing, "
            f"{self.skipped} skipped, {self.recondition} condition(s) updated. "
            "Missing games show ⚠ in /game list; get the full picture with "
            "/game export."
        )

    async def render_kwargs(self) -> dict:
        """content+embed for the current game, or the final summary."""
        if self.idx >= len(self.game_ids):
            return {"content": self._summary(), "embed": None, "view": None}
        gid = self.game_ids[self.idx]
        g = await _in_thread(lambda: _get_game(gid))
        header = f"**Stocktake {self.idx + 1}/{len(self.game_ids)}**: is this on the shelf?"
        return {"content": header, "embed": game_card(g) if g else None, "view": self}

    async def _mark(
        self,
        interaction: discord.Interaction,
        missing: bool | None,
        condition: str | None = None,
    ) -> None:
        gid = self.game_ids[self.idx]
        if missing is None:
            self.skipped += 1
        else:
            def mutate() -> None:
                with SessionLocal() as db:
                    g = db.get(BoardGame, gid)
                    if g:
                        g.missing = missing
                        if not missing:
                            g.last_seen_at = datetime.utcnow()
                        if condition is not None:
                            g.condition = condition
                        db.commit()

            await _in_thread(mutate)
            if missing:
                self.missing += 1
            else:
                self.seen += 1
            if condition is not None:
                self.recondition += 1
        self.idx += 1
        kwargs = await self.render_kwargs()
        if self.idx >= len(self.game_ids):
            self.stop()
        await interaction.response.edit_message(**kwargs)

    # A condition pick means "I've found it and looked at it", so it counts as
    # Seen and also corrects the recorded condition (many games are stuck on the
    # imported "Like New"). Sits above the buttons.
    @discord.ui.select(
        placeholder="Mark seen & set condition…",
        options=[discord.SelectOption(label=c) for c in get_args(Condition)],
        row=0,
    )
    async def condition_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        await self._mark(interaction, missing=False, condition=select.values[0])

    @discord.ui.button(label="Seen ✔", style=discord.ButtonStyle.success, row=1)
    async def seen_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._mark(interaction, missing=False)

    @discord.ui.button(label="Missing ✖", style=discord.ButtonStyle.danger, row=1)
    async def missing_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._mark(interaction, missing=True)

    @discord.ui.button(label="Skip ▶", style=discord.ButtonStyle.secondary, row=1)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._mark(interaction, missing=None)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(
                    content=f"Stocktake timed out at {self.idx}/{len(self.game_ids)}; "
                    "progress so far is saved. Run /game stocktake to continue.",
                    embed=None,
                    view=None,
                )
            except discord.HTTPException:
                pass


def _get_game(gid: int) -> BoardGame | None:
    with SessionLocal() as db:
        return db.get(BoardGame, gid)


@game.command(name="stocktake", description="Walk the shelf: mark each game seen or missing")
@app_commands.describe(
    owner="Only stocktake games owned by this person/RBGA",
    location="Only stocktake games stored at this location",
    unseen_only="Only games not yet sighted today (resume a stocktake)",
)
@app_commands.autocomplete(owner=owner_autocomplete, location=location_autocomplete)
@app_commands.check(require_exec_role)
async def game_stocktake(
    interaction: discord.Interaction,
    owner: str | None = None,
    location: str | None = None,
    unseen_only: bool = True,
):
    await interaction.response.defer()

    def query() -> list[int]:
        with SessionLocal() as db:
            stmt = select(BoardGame.id)
            if owner:
                stmt = stmt.where(BoardGame.owner == owner)
            if location:
                stmt = stmt.where(func.lower(BoardGame.location) == location.lower())
            if unseen_only:
                today = datetime.utcnow().date()
                stmt = stmt.where(
                    (BoardGame.last_seen_at.is_(None))
                    | (func.date(BoardGame.last_seen_at) < today)
                )
            return list(db.scalars(stmt.order_by(BoardGame.id)).all())

    ids = await _in_thread(query)
    if not ids:
        await interaction.followup.send("Nothing to stocktake: everything was sighted today.")
        return

    view = StocktakeView(ids)
    kwargs = await view.render_kwargs()
    view.message = await interaction.followup.send(**kwargs)


# --- owner contacts (exec only; never exposed via the API) ---------------------

owner_group = app_commands.Group(
    name="owner", description="Manage game-owner contact details (exec only)"
)


def set_owner_contact(name: str, contact: str) -> None:
    """Upsert the contact for an owner name (runs in a thread)."""
    with SessionLocal() as db:
        row = db.scalar(select(Owner).where(Owner.name == name))
        if row is None:
            db.add(Owner(name=name, contact=contact))
        else:
            row.contact = contact
        db.commit()


def get_owner_contact(name: str) -> str | None:
    with SessionLocal() as db:
        row = db.scalar(select(Owner).where(Owner.name == name))
        return row.contact if row else None


@owner_group.command(name="set", description="Save how to reach a game owner")
@app_commands.describe(
    name="Owner name exactly as it appears on their games",
    contact="How to reach them (Discord handle, email, phone)",
)
@app_commands.autocomplete(name=owner_autocomplete)
@app_commands.check(require_exec_role)
async def owner_set(interaction: discord.Interaction, name: str, contact: str):
    await interaction.response.defer(ephemeral=True)
    await _in_thread(lambda: set_owner_contact(name, contact))
    await interaction.followup.send(f"Saved contact for **{name}**.", ephemeral=True)


@owner_group.command(name="info", description="Look up a game owner's contact")
@app_commands.describe(name="Owner name")
@app_commands.autocomplete(name=owner_autocomplete)
@app_commands.check(require_exec_role)
async def owner_info(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)
    contact = await _in_thread(lambda: get_owner_contact(name))
    if contact:
        await interaction.followup.send(f"**{name}**: {contact}", ephemeral=True)
    else:
        await interaction.followup.send(
            f"No contact saved for **{name}**. Add one with /owner set.", ephemeral=True
        )


@owner_group.command(name="list", description="All saved owner contacts")
@app_commands.check(require_exec_role)
async def owner_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    def query() -> list[Owner]:
        with SessionLocal() as db:
            return list(db.scalars(select(Owner).order_by(Owner.name)).all())

    rows = await _in_thread(query)
    if not rows:
        await interaction.followup.send("No owner contacts saved yet.", ephemeral=True)
        return
    lines = [f"**{o.name}**: {o.contact or '(no contact)'}" for o in rows]
    await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)


def setup(tree: app_commands.CommandTree) -> None:
    """Register the /game and /owner groups on the bot's command tree."""
    tree.add_command(game)
    tree.add_command(owner_group)
