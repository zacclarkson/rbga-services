"""Single source of truth for the schema: one table per club feature.

Ported from Owen's RBGAKeyTracker (the `keys` table), plus `board_games` and
`complaints`. The complaint model deliberately stores NOTHING that could
de-anonymise a submitter (no IP, no user agent, no session id).
"""
import enum
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import COMPLAINTS_SCHEMA, Base


class Key(Base):
    """A physical cabinet key. Mirrors Owen's original SQLite schema."""

    __tablename__ = "keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    colour: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    campus: Mapped[str] = mapped_column(String(64))
    holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prev_holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    transfer_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BoardGame(Base):
    """The club's board-game inventory."""

    __tablename__ = "board_games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    publisher: Mapped[str | None] = mapped_column(String(200), nullable=True)
    min_players: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_players: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # From the club's SharePoint export (see rbga/db/import_boardgames.py).
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    condition: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bgg_link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Raw SharePoint attachment filename for CSV imports; a real image URL for
    # BGG imports (see rbga/bgg.py).
    image: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # BGG's small image variant. The full `image` is often a multi-MB original
    # that Discord's media proxy times out on when a gallery page shows ten at
    # once; embed thumbnails use this instead.
    thumbnail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Purchase value in dollars (the SharePoint export's "Cost" field).
    price: Mapped[float | None] = mapped_column(Numeric(10, 2, asdecimal=False), nullable=True)
    # Free-form labels as a JSON list of strings (e.g. ["Strategy", "Party"]).
    # Auto-filled from BGG's category links on /game add; a JSON column keeps
    # this simple (no join table) and works on both SQLite and Postgres.
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Asking price set by an exec. When unset, /game info and the export show
    # a computed estimate from `price` x condition factor instead.
    sell_price: Mapped[float | None] = mapped_column(
        Numeric(10, 2, asdecimal=False), nullable=True
    )
    # Stocktake: when the game was last physically sighted, and whether the
    # last stocktake marked it missing. Managed by /game stocktake in Discord.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    missing: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )


class Owner(Base):
    """Contact details for a game owner (a member, or the club itself).

    Owners are donors/lenders: the record exists so the club can reach them
    when they may want a game back. The bot keeps this table in sync with the
    inventory via prompts (add a contact for a first-time owner, drop it when
    their last game leaves; see prompt_owner_contact_upkeep in the bot).

    Deliberately a separate table from board_games: the public API serves game
    records to the web page, and contact details must never ride along. This
    table has NO API endpoint; it is read and written only by the exec-gated
    /owner commands in Discord (rbga/bot/boardgames.py). Games reference
    owners loosely by name (BoardGame.owner), matching the CSV heritage."""

    __tablename__ = "owners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    contact: Mapped[str | None] = mapped_column(String(256), nullable=True)


class ComplaintCategory(str, enum.Enum):
    """Who the complaint is *about* (the subject). The handler is derived from
    this per the handling table in docs/complaints-policy.md; never store the
    handler here, so the conflict-of-interest rule stays enforceable."""

    member = "member"
    committee = "committee"
    exec = "exec"
    president = "president"


class ComplaintStatus(str, enum.Enum):
    """Lifecycle of a complaint: new -> acknowledged -> closed."""

    new = "new"
    acknowledged = "acknowledged"
    closed = "closed"


class Complaint(Base):
    """An anonymous complaint. See the privacy note at the top of this file:
    nothing identifying is recorded. `contact` is optional and only present if
    the submitter *chose* to leave a way to be reached."""

    __tablename__ = "complaints"
    # Isolated schema on Postgres; None (default schema) on SQLite dev.
    __table_args__ = {"schema": COMPLAINTS_SCHEMA}

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    category: Mapped[ComplaintCategory] = mapped_column(Enum(ComplaintCategory))
    body: Mapped[str] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Lifecycle tracking. Set by reviewers, not submitters.
    status: Mapped[ComplaintStatus] = mapped_column(
        Enum(ComplaintStatus), default=ComplaintStatus.new, server_default="new", nullable=False
    )
    # Set when status becomes 'closed'; the retention purge counts from here.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set once the Discord handler has posted this complaint to its handler tier,
    # so the bot's poll loop doesn't route it twice.
    routed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ComplaintsConfig(Base):
    """Single-row (id=1) runtime config for where complaints are routed in
    Discord, set by the /complaints-setup wizard. Not sensitive (just channel/user
    ids), so it lives in the default schema; the bot reaches it via the API."""

    __tablename__ = "complaints_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    committee_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exec_channel_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    president_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
