"""Discord handling for complaints.

Complaints are *submitted* anonymously through the web API and stored in the
isolated complaints schema. This module is only the **handling** surface:

  * A poll loop asks the API for new complaints and posts a **metadata-only**
    notification (id, category, status — never the body) to the Discord
    destination for that complaint's handler tier:
        member    -> committee channel
        committee -> exec channel
        exec      -> president DM
    (Complaints about the president are rejected at submission and redirected to
    RUSU, so they never reach here.)
  * Handlers act via buttons — View / Acknowledge / Escalate / Close. *View*
    fetches the body and shows it **ephemerally** (only to the clicker), so the
    text never lands in a Discord channel. The others drive the API.

The bot reaches complaints ONLY through the API (with the reviewer token) — it has
no direct complaints DB access, preserving the credential isolation. See
CLAUDE.md and docs/complaints-policy.md.
"""
import asyncio
import os
import re

import aiohttp
import discord

API_BASE = os.environ.get("RBGA_API_BASE_URL", "").rstrip("/")
REVIEWER_TOKEN = os.environ.get("COMPLAINTS_API_TOKEN")
COMMITTEE_CHANNEL_ID = os.environ.get("COMPLAINTS_COMMITTEE_CHANNEL_ID")
EXEC_CHANNEL_ID = os.environ.get("COMPLAINTS_EXEC_CHANNEL_ID")
PRESIDENT_USER_ID = os.environ.get("COMPLAINTS_PRESIDENT_USER_ID")
POLL_SECONDS = int(os.environ.get("COMPLAINTS_POLL_SECONDS", "60"))

RUSU_LINKS = (
    "• RUSU Student Rights — https://rusu.rmit.edu.au/studentrights/\n"
    "• RMIT Safer Community — "
    "https://www.rmit.edu.au/about/our-locations-and-facilities/facilities/safety-security/safer-community"
)

# --- routing (pure, unit-testable) -----------------------------------------
# Each maps a complaint category to (kind, symbol) where kind is "channel"/"dm"/
# "rusu" and symbol names the tier the id is resolved from.
_INITIAL = {
    "member": ("channel", "committee"),
    "committee": ("channel", "exec"),
    "exec": ("dm", "president"),
}
_ESCALATED = {
    "member": ("channel", "exec"),
    "committee": ("dm", "president"),
    "exec": ("rusu", None),
}
_ESCALATION_TARGET = {"member": "exec", "committee": "president", "exec": "rusu"}


def destination_for(category: str, escalated: bool = False) -> tuple[str, str | None]:
    """Where a complaint of `category` should be posted (or, if escalated, sent
    on to). Raises KeyError for unroutable categories (e.g. president)."""
    return (_ESCALATED if escalated else _INITIAL)[category]


def next_escalation_target(category: str) -> str:
    """The EscalationTarget a complaint of `category` escalates to."""
    return _ESCALATION_TARGET[category]


def _configured() -> bool:
    return all(
        [API_BASE, REVIEWER_TOKEN, COMMITTEE_CHANNEL_ID, EXEC_CHANNEL_ID, PRESIDENT_USER_ID]
    )


# --- API client (reviewer token; the bot never touches the complaints DB) ----
_session: aiohttp.ClientSession | None = None


async def _http() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(headers={"X-Reviewer-Token": REVIEWER_TOKEN or ""})
    return _session


async def _api_list() -> list[dict]:
    s = await _http()
    async with s.get(f"{API_BASE}/complaints") as r:
        r.raise_for_status()
        return await r.json()


async def _api_get(cid: int) -> dict:
    s = await _http()
    async with s.get(f"{API_BASE}/complaints/{cid}") as r:
        r.raise_for_status()
        return await r.json()


async def _api_patch(cid: int, *, status: str | None = None, escalated_to: str | None = None) -> dict:
    s = await _http()
    payload: dict[str, str] = {}
    if status:
        payload["status"] = status
    if escalated_to:
        payload["escalated_to"] = escalated_to
    async with s.patch(f"{API_BASE}/complaints/{cid}", json=payload) as r:
        r.raise_for_status()
        return await r.json()


async def _api_mark_routed(cid: int) -> None:
    s = await _http()
    async with s.post(f"{API_BASE}/complaints/{cid}/routed") as r:
        r.raise_for_status()


# --- Discord presentation ---------------------------------------------------
_STATUS_COLOUR = {
    "new": discord.Colour.orange(),
    "acknowledged": discord.Colour.blurple(),
    "escalated": discord.Colour.red(),
    "closed": discord.Colour.dark_grey(),
}
_ID_RE = re.compile(r"#(\d+)")


def _embed(c: dict) -> discord.Embed:
    """Metadata-only embed — deliberately never includes the body."""
    e = discord.Embed(
        title=f"Complaint #{c['id']}",
        colour=_STATUS_COLOUR.get(c["status"], discord.Colour.greyple()),
    )
    e.add_field(name="About", value=c["category"])
    e.add_field(name="Status", value=c["status"])
    if c.get("escalated_to"):
        e.add_field(name="Escalated to", value=c["escalated_to"])
    e.set_footer(text="Body hidden — click View (only you will see it).")
    return e


def _complaint_id(interaction: discord.Interaction) -> int:
    return int(_ID_RE.search(interaction.message.embeds[0].title).group(1))


async def _post(client: discord.Client, kind: str, symbol: str | None, c: dict) -> None:
    """Post a notification (embed + buttons) to a channel or the president's DM."""
    embed, view = _embed(c), ComplaintView()
    if kind == "channel":
        chan_id = int(COMMITTEE_CHANNEL_ID if symbol == "committee" else EXEC_CHANNEL_ID)
        channel = client.get_channel(chan_id) or await client.fetch_channel(chan_id)
        await channel.send(embed=embed, view=view)
    elif kind == "dm":
        user = await client.fetch_user(int(PRESIDENT_USER_ID))
        await user.send(embed=embed, view=view)


async def _refresh(interaction: discord.Interaction, c: dict) -> None:
    """Update the original notification's embed; drop the buttons once closed."""
    view = None if c["status"] == "closed" else ComplaintView()
    await interaction.message.edit(embed=_embed(c), view=view)


async def _send_body(interaction: discord.Interaction, cid: int, c: dict) -> None:
    header = f"**Complaint #{cid}** — about {c['category']}\n\n"
    text = header + (c.get("body") or "(empty)")
    if c.get("contact"):
        text += f"\n\n**Contact left by submitter:** {c['contact']}"
    # Discord caps messages ~2000 chars; chunk to be safe. All ephemeral.
    for i in range(0, len(text), 1900):
        await interaction.followup.send(text[i : i + 1900], ephemeral=True)


# --- button handlers --------------------------------------------------------
async def _do_view(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    cid = _complaint_id(interaction)
    await _send_body(interaction, cid, await _api_get(cid))


async def _do_status(interaction: discord.Interaction, status: str) -> None:
    await interaction.response.defer(ephemeral=True)
    cid = _complaint_id(interaction)
    c = await _api_patch(cid, status=status)
    await _refresh(interaction, c)
    await interaction.followup.send(f"Complaint #{cid} → {status}.", ephemeral=True)


async def _do_escalate(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    cid = _complaint_id(interaction)
    category = (await _api_get(cid))["category"]
    target = next_escalation_target(category)
    c = await _api_patch(cid, status="escalated", escalated_to=target)
    await _refresh(interaction, c)

    kind, symbol = destination_for(category, escalated=True)
    if kind == "rusu":
        await interaction.followup.send(
            f"Complaint #{cid} should now be referred to RUSU:\n{RUSU_LINKS}\nMarked escalated.",
            ephemeral=True,
        )
    else:
        await _post(interaction.client, kind, symbol, c)
        await interaction.followup.send(f"Escalated complaint #{cid} to the {target}.", ephemeral=True)


class ComplaintView(discord.ui.View):
    """Persistent (timeout=None) action row. One instance registered at startup
    handles every complaint message; the complaint id is read from the embed."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="View", style=discord.ButtonStyle.secondary, custom_id="complaint:view")
    async def view_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _do_view(interaction)

    @discord.ui.button(label="Acknowledge", style=discord.ButtonStyle.primary, custom_id="complaint:ack")
    async def ack_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _do_status(interaction, "acknowledged")

    @discord.ui.button(label="Escalate", style=discord.ButtonStyle.danger, custom_id="complaint:escalate")
    async def escalate_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _do_escalate(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.success, custom_id="complaint:close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _do_status(interaction, "closed")


# --- poll loop --------------------------------------------------------------
_polling = False


async def _poll_loop(client: discord.Client) -> None:
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            for c in await _api_list():
                if c["status"] == "new" and c.get("routed_at") is None:
                    kind, symbol = destination_for(c["category"])
                    await _post(client, kind, symbol, c)
                    await _api_mark_routed(c["id"])
        except Exception as e:  # never let a transient API/Discord error kill the loop
            print(f"[complaints] poll error: {e!r}")
        await asyncio.sleep(POLL_SECONDS)


def start_polling(client: discord.Client) -> None:
    """Start the routing poll loop, once. No-op if complaints handling isn't
    configured, so the bot still runs keys/board-games without it."""
    global _polling
    if _polling:
        return
    if not _configured():
        print(
            "[complaints] not configured (need RBGA_API_BASE_URL, COMPLAINTS_API_TOKEN, "
            "and the committee/exec channel + president user ids) — Discord handling disabled."
        )
        return
    _polling = True
    client.loop.create_task(_poll_loop(client))


def setup(client: discord.Client) -> None:
    """Register the persistent view so complaint buttons work across restarts."""
    client.add_view(ComplaintView())
