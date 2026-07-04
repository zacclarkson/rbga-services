"""Single source of truth for the schema — one table per club feature.

Ported from Owen's RBGAKeyTracker (the `keys` table), plus `board_games` and
`complaints`. The complaint model deliberately stores NOTHING that could
de-anonymise a submitter (no IP, no user agent, no session id).
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, Numeric, String, Text
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
    # Purchase value in dollars (the SharePoint export's "Cost" field).
    price: Mapped[float | None] = mapped_column(Numeric(10, 2, asdecimal=False), nullable=True)


class ComplaintCategory(str, enum.Enum):
    """Who the complaint is *about* (the subject). The handler is derived from
    this per the escalation ladder in docs/complaints-policy.md — never store the
    handler here, so the conflict-of-interest rule stays enforceable."""

    member = "member"
    committee = "committee"
    exec = "exec"
    president = "president"


class ComplaintStatus(str, enum.Enum):
    """Lifecycle of a complaint: new -> acknowledged -> (escalated) -> closed."""

    new = "new"
    acknowledged = "acknowledged"
    escalated = "escalated"
    closed = "closed"


class EscalationTarget(str, enum.Enum):
    """Who a complaint was escalated to. `rusu` is the external backstop (RMIT
    University Student Union) — see docs/complaints-policy.md §5-6."""

    committee = "committee"
    exec = "exec"
    president = "president"
    rusu = "rusu"


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
    # Lifecycle + escalation tracking. Set by reviewers, not submitters.
    status: Mapped[ComplaintStatus] = mapped_column(
        Enum(ComplaintStatus), default=ComplaintStatus.new, server_default="new", nullable=False
    )
    escalated_to: Mapped[EscalationTarget | None] = mapped_column(
        Enum(EscalationTarget), nullable=True
    )
    # Set when status becomes 'closed'; the retention purge counts from here.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set once the Discord handler has posted this complaint to its handler tier,
    # so the bot's poll loop doesn't route it twice.
    routed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
