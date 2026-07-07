"""Discord handling for complaints.

Complaints are *submitted* anonymously through the web API and stored in the
isolated complaints schema. This module is only the **handling** surface:

  * A poll loop asks the API for new complaints and posts a **metadata-only**
    notification (id, category, status, never the body) to the Discord
    destination for that complaint's handler tier:
        member    -> committee channel
        committee -> exec channel
        exec      -> president DM
    (Complaints about the president are rejected at submission and redirected to
    RUSU, so they never reach here.)
  * Handlers act via buttons: View / Acknowledge / Escalate / Close. *View*
    fetches the body and shows it **ephemerally** (only to the clicker), so the
    text never lands in a Discord channel. The others drive the API.

The bot reaches complaints ONLY through the API (with the reviewer token); it has
no direct complaints DB access, preserving the credential isolation. See
docs/complaints-policy.md (and docs/deploy.md for the DB role setup).
"""
import asyncio
import os
import re

import aiohttp
import discord
from discord import app_commands

API_BASE = os.environ.get("RBGA_API_BASE_URL", "").rstrip("/")
REVIEWER_TOKEN = os.environ.get("COMPLAINTS_API_TOKEN")
# Routing targets are normally set at runtime via /complaints-setup (stored in the
# DB); these env vars are an optional fallback for a scripted deploy.
ENV_COMMITTEE = os.environ.get("COMPLAINTS_COMMITTEE_CHANNEL_ID")
ENV_EXEC = os.environ.get("COMPLAINTS_EXEC_CHANNEL_ID")
ENV_PRESIDENT = os.environ.get("COMPLAINTS_PRESIDENT_USER_ID")
POLL_SECONDS = int(os.environ.get("COMPLAINTS_POLL_SECONDS", "60"))
# Who may run /complaints-setup (besides the server owner). Defaults to the exec
# role that already gates bot mutations.
ADMIN_ROLE = os.environ.get("COMPLAINTS_ADMIN_ROLE") or os.environ.get("DISCORD_KEYS_ROLE")

RUSU_LINKS = (
    "• RUSU Student Rights: https://rusu.rmit.edu.au/studentrights/\n"
    "• RMIT Safer Community: "
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


def is_submittable(category: str) -> bool:
    """False for 'president': those are directed to RUSU, not taken into the
    club's system (policy §5)."""
    return category in ("member", "committee", "exec")


def merge_targets(config: dict, env: dict) -> dict:
    """Effective routing targets: a saved config value wins, else the env
    fallback, else None. Keys: 'committee', 'exec', 'president'."""
    return {k: (config.get(k) or env.get(k)) for k in ("committee", "exec", "president")}


def is_authorised(user_id: int, owner_id: int | None, user_role_names: list[str], admin_role: str | None) -> bool:
    """Who may run /complaints-setup: the guild owner, or a holder of admin_role."""
    if owner_id is not None and user_id == owner_id:
        return True
    return bool(admin_role) and admin_role in user_role_names


def _configured() -> bool:
    # Only the API link is required to start; routing targets can arrive later via
    # the /complaints-setup wizard.
    return bool(API_BASE and REVIEWER_TOKEN)


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


async def _api_get_config() -> dict:
    s = await _http()
    async with s.get(f"{API_BASE}/complaints/config") as r:
        r.raise_for_status()
        return await r.json()


async def _api_put_config(committee: str | None, exec_: str | None, president: str | None) -> dict:
    s = await _http()
    payload = {
        "committee_channel_id": committee,
        "exec_channel_id": exec_,
        "president_user_id": president,
    }
    async with s.put(f"{API_BASE}/complaints/config", json=payload) as r:
        r.raise_for_status()
        return await r.json()


async def _api_submit(category: str, body: str, contact: str | None) -> dict:
    """Create a complaint on behalf of a Discord submitter. Sends ONLY the
    content, never the submitter's identity. The reviewer token (already on the
    session) exempts this from the public rate limit. Returns the ack ({id, ...})."""
    s = await _http()
    payload = {"category": category, "body": body, "contact": contact}
    async with s.post(f"{API_BASE}/complaints", json=payload) as r:
        r.raise_for_status()
        return await r.json()


async def resolve_targets() -> dict:
    """The effective routing targets (saved config over env fallback)."""
    cfg = await _api_get_config()
    saved = {
        "committee": cfg.get("committee_channel_id"),
        "exec": cfg.get("exec_channel_id"),
        "president": cfg.get("president_user_id"),
    }
    env = {"committee": ENV_COMMITTEE, "exec": ENV_EXEC, "president": ENV_PRESIDENT}
    return merge_targets(saved, env)


def _target_id(kind: str, symbol: str | None, targets: dict) -> str | None:
    if kind == "channel":
        return targets.get(symbol)
    if kind == "dm":
        return targets.get("president")
    return None


def tier_ready(category: str, targets: dict) -> bool:
    """Whether the handler tier for `category` has a destination configured
    (via /complaints-setup or the env fallback), i.e. a new complaint of this
    category could actually be delivered rather than sitting unrouted."""
    kind, symbol = destination_for(category)
    return bool(_target_id(kind, symbol, targets))


# --- Discord presentation ---------------------------------------------------
_STATUS_COLOUR = {
    "new": discord.Colour.orange(),
    "acknowledged": discord.Colour.blurple(),
    "escalated": discord.Colour.red(),
    "closed": discord.Colour.dark_grey(),
}
_ID_RE = re.compile(r"#(\d+)")


def _embed(c: dict) -> discord.Embed:
    """Metadata-only embed that deliberately never includes the body."""
    e = discord.Embed(
        title=f"Complaint #{c['id']}",
        colour=_STATUS_COLOUR.get(c["status"], discord.Colour.greyple()),
    )
    e.add_field(name="About", value=c["category"])
    e.add_field(name="Status", value=c["status"])
    if c.get("escalated_to"):
        e.add_field(name="Escalated to", value=c["escalated_to"])
    e.set_footer(text="Body hidden. Click View (only you will see it).")
    return e


def _complaint_id(interaction: discord.Interaction) -> int:
    return int(_ID_RE.search(interaction.message.embeds[0].title).group(1))


async def _post(client: discord.Client, kind: str, symbol: str | None, c: dict, targets: dict) -> bool:
    """Post a notification (embed + buttons) to a channel or the president's DM.
    Returns False (without posting) if that tier's destination isn't configured."""
    target_id = _target_id(kind, symbol, targets)
    if not target_id:
        who = symbol or kind
        print(f"[complaints] no {who} destination set; run /complaints-setup to route complaint #{c['id']}.")
        return False

    embed, view = _embed(c), complaint_view(c["id"])
    if kind == "channel":
        channel = client.get_channel(int(target_id)) or await client.fetch_channel(int(target_id))
        await channel.send(embed=embed, view=view)
    elif kind == "dm":
        user = await client.fetch_user(int(target_id))
        await user.send(embed=embed, view=view)
    return True


async def _route(client: discord.Client, c: dict, targets: dict) -> bool:
    """Post a new complaint to its handler tier and mark it routed. Returns False
    (leaving it unrouted for a later retry) if the tier isn't configured yet."""
    kind, symbol = destination_for(c["category"])
    if await _post(client, kind, symbol, c, targets):
        await _api_mark_routed(c["id"])
        return True
    return False


async def _refresh(interaction: discord.Interaction, c: dict) -> None:
    """Update the original notification's embed; drop the buttons once closed."""
    view = None if c["status"] == "closed" else complaint_view(c["id"])
    await interaction.message.edit(embed=_embed(c), view=view)


async def _send_body(interaction: discord.Interaction, cid: int, c: dict) -> None:
    header = f"**Complaint #{cid}** (about {c['category']})\n\n"
    text = header + (c.get("body") or "(empty)")
    if c.get("contact"):
        text += f"\n\n**Contact left by submitter:** {c['contact']}"
    # Discord caps messages ~2000 chars; chunk to be safe. All ephemeral.
    for i in range(0, len(text), 1900):
        await interaction.followup.send(text[i : i + 1900], ephemeral=True)


# --- button handlers --------------------------------------------------------
async def _do_view(interaction: discord.Interaction, cid: int) -> None:
    await interaction.response.defer(ephemeral=True)
    await _send_body(interaction, cid, await _api_get(cid))


async def _do_status(interaction: discord.Interaction, cid: int, status: str) -> None:
    await interaction.response.defer(ephemeral=True)
    c = await _api_patch(cid, status=status)
    await _refresh(interaction, c)
    await interaction.followup.send(f"Complaint #{cid} → {status}.", ephemeral=True)


async def _do_escalate(interaction: discord.Interaction, cid: int) -> None:
    await interaction.response.defer(ephemeral=True)
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
        posted = await _post(interaction.client, kind, symbol, c, await resolve_targets())
        tail = "" if posted else f" (no {target} destination set yet; run /complaints-setup)"
        await interaction.followup.send(f"Escalated complaint #{cid} to the {target}.{tail}", ephemeral=True)


# Action name -> (label, style). Order is the button order in the row.
_ACTIONS: dict[str, tuple[str, discord.ButtonStyle]] = {
    "view": ("View", discord.ButtonStyle.secondary),
    "ack": ("Acknowledge", discord.ButtonStyle.primary),
    "escalate": ("Escalate", discord.ButtonStyle.danger),
    "close": ("Close", discord.ButtonStyle.success),
}


class ComplaintAction(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"complaint:(?P<action>view|ack|escalate|close):(?P<id>[0-9]+)",
):
    """A complaint button whose custom_id carries both the action and the
    complaint id (e.g. "complaint:view:42"). Dynamic items are rebuilt from the
    custom_id on every click, so they work across bot restarts with no
    registered view instance and no reliance on the embed title."""

    def __init__(self, action: str, cid: int) -> None:
        label, style = _ACTIONS[action]
        super().__init__(
            discord.ui.Button(label=label, style=style, custom_id=f"complaint:{action}:{cid}")
        )
        self.action = action
        self.cid = cid

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: "re.Match[str]"
    ) -> "ComplaintAction":
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.action == "view":
            await _do_view(interaction, self.cid)
        elif self.action == "ack":
            await _do_status(interaction, self.cid, "acknowledged")
        elif self.action == "escalate":
            await _do_escalate(interaction, self.cid)
        else:
            await _do_status(interaction, self.cid, "closed")


def complaint_view(cid: int) -> discord.ui.View:
    """The action row for a complaint notification, with the complaint id
    embedded in every button's custom_id."""
    view = discord.ui.View(timeout=None)
    for action in _ACTIONS:
        view.add_item(ComplaintAction(action, cid))
    return view


class ComplaintView(discord.ui.View):
    """LEGACY persistent (timeout=None) action row, for messages posted before
    the complaint id moved into the custom_id. One instance registered at
    startup handles those; the id is read from the embed title. New
    notifications use complaint_view() / ComplaintAction instead."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="View", style=discord.ButtonStyle.secondary, custom_id="complaint:view")
    async def view_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _do_view(interaction, _complaint_id(interaction))

    @discord.ui.button(label="Acknowledge", style=discord.ButtonStyle.primary, custom_id="complaint:ack")
    async def ack_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _do_status(interaction, _complaint_id(interaction), "acknowledged")

    @discord.ui.button(label="Escalate", style=discord.ButtonStyle.danger, custom_id="complaint:escalate")
    async def escalate_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _do_escalate(interaction, _complaint_id(interaction))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.success, custom_id="complaint:close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _do_status(interaction, _complaint_id(interaction), "closed")


# --- setup wizard (owner / admin only) --------------------------------------
def _chan_default(cid: str | None) -> list:
    return (
        [discord.SelectDefaultValue(id=int(cid), type=discord.SelectDefaultValueType.channel)]
        if cid
        else []
    )


def _user_default(uid: str | None) -> list:
    return (
        [discord.SelectDefaultValue(id=int(uid), type=discord.SelectDefaultValueType.user)]
        if uid
        else []
    )


class _CommitteeSelect(discord.ui.ChannelSelect):
    def __init__(self, current: str | None) -> None:
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Committee channel (member complaints)",
            min_values=1, max_values=1, row=0,
            default_values=_chan_default(current),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.committee = str(self.values[0].id)
        await interaction.response.defer()


class _ExecSelect(discord.ui.ChannelSelect):
    def __init__(self, current: str | None) -> None:
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="Exec channel (committee complaints)",
            min_values=1, max_values=1, row=1,
            default_values=_chan_default(current),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.exec_ = str(self.values[0].id)
        await interaction.response.defer()


class _PresidentSelect(discord.ui.UserSelect):
    def __init__(self, current: str | None) -> None:
        super().__init__(
            placeholder="President (receives exec complaints by DM)",
            min_values=1, max_values=1, row=2,
            default_values=_user_default(current),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.view.president = str(self.values[0].id)
        await interaction.response.defer()


class SetupView(discord.ui.View):
    """Transient (ephemeral, 5-min) panel to pick the routing targets."""

    def __init__(self, cfg: dict) -> None:
        super().__init__(timeout=300)
        self.committee = cfg.get("committee_channel_id")
        self.exec_ = cfg.get("exec_channel_id")
        self.president = cfg.get("president_user_id")
        self.add_item(_CommitteeSelect(self.committee))
        self.add_item(_ExecSelect(self.exec_))
        self.add_item(_PresidentSelect(self.president))

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=3)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _api_put_config(self.committee, self.exec_, self.president)
        lines = [
            f"• Committee → <#{self.committee}>" if self.committee else "• Committee → *(unset)*",
            f"• Exec → <#{self.exec_}>" if self.exec_ else "• Exec → *(unset)*",
            f"• President → <@{self.president}>" if self.president else "• President → *(unset)*",
        ]
        await interaction.response.edit_message(
            content="**Saved complaints routing:**\n" + "\n".join(lines), view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled. No changes made.", view=None)


@app_commands.command(
    name="complaints-setup", description="Configure where complaints are routed in Discord"
)
@app_commands.guild_only()
async def complaints_setup(interaction: discord.Interaction) -> None:
    member = interaction.user
    owner_id = interaction.guild.owner_id if interaction.guild else None
    role_names = [r.name for r in getattr(member, "roles", [])]
    if not is_authorised(member.id, owner_id, role_names, ADMIN_ROLE):
        who = f"the server owner or the **{ADMIN_ROLE}** role" if ADMIN_ROLE else "the server owner"
        await interaction.response.send_message(
            f"Only {who} can configure complaints routing.", ephemeral=True
        )
        return
    try:
        cfg = await _api_get_config()
    except Exception as e:
        await interaction.response.send_message(
            f"Couldn't reach the API to load the current config: {e!r}", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "**Complaints setup**: choose where each tier's complaints go, then **Save**.",
        view=SetupView(cfg),
        ephemeral=True,
    )


# --- submission (/complain) -------------------------------------------------
class ComplaintModal(discord.ui.Modal, title="Raise a complaint"):
    body = discord.ui.TextInput(
        label="What happened?",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,  # Discord's modal cap (server allows 5000)
        placeholder="Describe your concern. Don't include your name unless you want to be identified.",
    )
    contact = discord.ui.TextInput(
        label="Contact (optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=256,
        placeholder="Only if you want a reply. Leaving this can de-anonymise you.",
    )

    def __init__(self, category: str) -> None:
        super().__init__()
        self.category = category

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Forward ONLY the content, never interaction.user.
        ack = await _api_submit(self.category, self.body.value, self.contact.value or None)
        await interaction.response.send_message(
            "Thanks! Your complaint was submitted **anonymously**. The people handling it "
            "won't see who you are.",
            ephemeral=True,
        )
        # Route it right away rather than waiting for the poll loop. If it fails
        # (e.g. the tier isn't configured yet) the poll loop retries it later.
        try:
            c = {"id": ack["id"], "category": self.category, "status": "new", "escalated_to": None}
            await _route(interaction.client, c, await resolve_targets())
        except Exception as e:
            print(f"[complaints] instant-route failed for #{ack['id']}: {e!r}")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        msg = "Sorry, something went wrong submitting that. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        print(f"[complaints] submit error: {error!r}")  # no submitter identity logged


@app_commands.command(
    name="complain", description="Raise a complaint (anonymous; handlers won't see who you are)"
)
@app_commands.guild_only()
@app_commands.describe(about="Who the complaint is about")
@app_commands.choices(
    about=[
        app_commands.Choice(name="A member", value="member"),
        app_commands.Choice(name="The committee", value="committee"),
        app_commands.Choice(name="An exec", value="exec"),
        app_commands.Choice(name="The president", value="president"),
    ]
)
async def complain(interaction: discord.Interaction, about: app_commands.Choice[str]) -> None:
    if not is_submittable(about.value):
        await interaction.response.send_message(
            "Complaints about the president are handled **independently of the club**, "
            f"so please contact one of these directly:\n{RUSU_LINKS}",
            ephemeral=True,
        )
        return
    # Don't take a complaint that has nowhere to go; tell the submitter routing
    # hasn't been configured instead of letting it sit unrouted in the DB.
    try:
        ready = tier_ready(about.value, await resolve_targets())
    except Exception as e:
        # Fail open: a transient API error shouldn't block complaints (submission
        # itself will surface the error if the API really is down).
        print(f"[complaints] setup check failed, allowing submission: {e!r}")
        ready = True
    if not ready:
        who = f"the server owner or someone with the **{ADMIN_ROLE}** role" if ADMIN_ROLE else "the server owner"
        await interaction.response.send_message(
            "The complaints system hasn't been fully set up yet, so this complaint "
            f"couldn't be delivered. Please ask {who} to run **/complaints-setup** first, "
            "then try again.",
            ephemeral=True,
        )
        return
    await interaction.response.send_modal(ComplaintModal(about.value))


# --- poll loop --------------------------------------------------------------
_polling = False


async def _poll_loop(client: discord.Client) -> None:
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            new = [c for c in await _api_list() if c["status"] == "new" and c.get("routed_at") is None]
            if new:
                targets = await resolve_targets()
                # Catches web-form submissions and anything instant-routing missed
                # (e.g. a tier that wasn't configured when it was submitted).
                for c in new:
                    await _route(client, c, targets)
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
            "[complaints] not configured (need RBGA_API_BASE_URL and COMPLAINTS_API_TOKEN) "
            "so Discord handling is disabled. Set routing targets with /complaints-setup."
        )
        return
    _polling = True
    client.loop.create_task(_poll_loop(client))


def register_persistent(client: discord.Client) -> None:
    """Register the complaint buttons for dispatch. MUST be called with the
    event loop running (Client.setup_hook), never at import time: a View
    constructed without a running loop is left undispatchable by discord.py,
    which then silently drops every click on it (no error, no log)."""
    # New-style buttons: rebuilt from the custom_id on every click.
    client.add_dynamic_items(ComplaintAction)
    # Legacy buttons on messages posted before the id lived in the custom_id.
    client.add_view(ComplaintView())


def setup(client: discord.Client, tree: app_commands.CommandTree) -> None:
    """Register /complaints-setup and /complain. Button registration lives in
    register_persistent(), which the client's setup_hook must call."""
    tree.add_command(complaints_setup)
    tree.add_command(complain)
