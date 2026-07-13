"""Mirror Discord announcements to the club Instagram as stories.

Flow: a message lands in the announcements channel (IG_ANNOUNCE_CHANNEL_ID) ->
the bot renders it as a story card (rbga/bot/igcard.py) and replies with a
preview + "Post to Instagram" / "Skip" buttons. Nothing is published until an
exec clicks Post. On Post the bot re-fetches the source message (source of
truth; survives restarts because the message id lives in the button custom_id),
re-renders, and publishes: the text card as frame one, then each attached image
as its own frame.

Instagram's Content Publishing API only *fetches* images from a public URL, so
each frame is first uploaded to the API's transient /ig-media store (write
token) and the resulting PUBLIC_API_BASE_URL link is handed to Instagram.

Token lifecycle: the long-lived Graph token expires every 60 days. The current
token lives in the instagram_config table (seeded from IG_ACCESS_TOKEN) and a
daily loop refreshes it once it's REFRESH_AFTER_DAYS old, so a token issued
once at setup keeps itself alive. If refresh ever fails the loop logs loudly;
an exec re-issues via docs/instagram-setup.md.

Requires the (privileged) message-content intent; __main__ only enables it
when this module is configured, so an unconfigured bot never demands a portal
toggle it doesn't need.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO

import aiohttp
import discord
from sqlalchemy import select

from ..db.database import SessionLocal
from ..db.models import InstagramConfig
from .common import EXEC_ROLE, _in_thread
from .igcard import clean_announcement, render_card

CHANNEL_ID = os.environ.get("IG_ANNOUNCE_CHANNEL_ID")
IG_USER_ID = os.environ.get("IG_USER_ID")
ENV_TOKEN = os.environ.get("IG_ACCESS_TOKEN")  # seed only; live token is in the DB
# Where Instagram fetches our frames from (the Caddy-fronted public API base,
# e.g. https://rmitbga.duckdns.org) vs where the bot reaches the API internally.
PUBLIC_BASE = os.environ.get("PUBLIC_API_BASE_URL", "").rstrip("/")
API_BASE = os.environ.get("RBGA_API_BASE_URL", "").rstrip("/")
WRITE_TOKEN = os.environ.get("RBGA_API_TOKEN")

GRAPH_HOST = "https://graph.instagram.com"
GRAPH_BASE = f"{GRAPH_HOST}/v23.0"
REFRESH_AFTER_DAYS = 7  # refresh weekly; tokens die at 60 days unrefreshed
_PUBLISH_RETRIES = 5  # media containers can take a moment to become ready
_PUBLISH_RETRY_SECONDS = 2


def configured() -> bool:
    """Whether the announcement mirror should run. The access token is checked
    at publish time (it may live only in the DB), not here."""
    return bool(CHANNEL_ID and IG_USER_ID and PUBLIC_BASE and API_BASE and WRITE_TOKEN)


def log_status() -> None:
    if configured():
        print(f"[instagram] announcement mirror active on channel {CHANNEL_ID}.")
    else:
        print(
            "[instagram] not configured (need IG_ANNOUNCE_CHANNEL_ID, IG_USER_ID, "
            "PUBLIC_API_BASE_URL, RBGA_API_BASE_URL and RBGA_API_TOKEN); "
            "announcement mirroring is disabled."
        )


# --- token storage (DB row id=1, seeded from the env) ------------------------
def _load_token() -> str | None:
    """Current Graph token. IG_ACCESS_TOKEN is absorbed into the DB row the
    first time it's seen — including a *changed* value (an exec pasting a
    replacement for a dead token), which re-seeds over whatever the row held.
    Synchronous (SQLAlchemy); call via _in_thread from the loop."""
    with SessionLocal() as db:
        row = db.get(InstagramConfig, 1)
        if ENV_TOKEN and (row is None or row.env_seed != ENV_TOKEN):
            if row is None:
                row = InstagramConfig(id=1)
                db.add(row)
            row.access_token = ENV_TOKEN
            row.env_seed = ENV_TOKEN
            # Assume freshly issued: setup docs say to paste a new token. The
            # refresh loop keeps it alive from here.
            row.token_refreshed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.commit()
            return ENV_TOKEN
        return row.access_token if row else None


def _save_token(token: str) -> None:
    with SessionLocal() as db:
        row = db.get(InstagramConfig, 1)
        if row is None:
            row = InstagramConfig(id=1)
            db.add(row)
        row.access_token = token
        row.token_refreshed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()


def _token_age_days() -> float | None:
    with SessionLocal() as db:
        row = db.get(InstagramConfig, 1)
        if not row or not row.access_token:
            return None
        if not row.token_refreshed_at:
            return float("inf")  # unknown age: refresh at the next opportunity
        return (datetime.now(timezone.utc).replace(tzinfo=None) - row.token_refreshed_at) / timedelta(days=1)


# --- HTTP -------------------------------------------------------------------
_session: aiohttp.ClientSession | None = None


async def _http() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def _graph_error(payload: dict) -> str:
    """Human-readable message from a Graph API error body (never the token)."""
    err = payload.get("error") or {}
    return err.get("error_user_msg") or err.get("message") or "unknown Graph API error"


async def _host_media(data: bytes, content_type: str) -> str:
    """Upload frame bytes to the API's transient /ig-media store; returns the
    public URL Instagram will fetch."""
    s = await _http()
    async with s.post(
        f"{API_BASE}/ig-media",
        data=data,
        headers={"X-API-Token": WRITE_TOKEN or "", "Content-Type": content_type},
    ) as r:
        if r.status != 200:
            raise RuntimeError(f"media upload failed ({r.status}): {await r.text()}")
        payload = await r.json()
    return f"{PUBLIC_BASE}{payload['path']}"


async def _publish_story(image_url: str, token: str) -> None:
    """The two-step Graph publish: create a STORIES container, then publish it
    (retrying briefly while Instagram processes the container)."""
    s = await _http()
    async with s.post(
        f"{GRAPH_BASE}/{IG_USER_ID}/media",
        data={"media_type": "STORIES", "image_url": image_url, "access_token": token},
    ) as r:
        payload = await r.json()
        if r.status != 200 or "id" not in payload:
            raise RuntimeError(f"story container failed: {_graph_error(payload)}")
    creation_id = payload["id"]

    for attempt in range(_PUBLISH_RETRIES):
        async with s.post(
            f"{GRAPH_BASE}/{IG_USER_ID}/media_publish",
            data={"creation_id": creation_id, "access_token": token},
        ) as r:
            payload = await r.json()
            if r.status == 200 and "id" in payload:
                return
        if attempt < _PUBLISH_RETRIES - 1:
            await asyncio.sleep(_PUBLISH_RETRY_SECONDS)
    raise RuntimeError(f"story publish failed: {_graph_error(payload)}")


# --- frames -------------------------------------------------------------------
def _image_attachments(message: discord.Message) -> list[discord.Attachment]:
    return [a for a in message.attachments if (a.content_type or "").startswith("image/")]


def preview_note(has_text: bool, image_count: int) -> str:
    """The caption above the preview card, spelling out what Post will publish."""
    frames = (1 if has_text else 0) + image_count
    parts = []
    if has_text:
        parts.append("the text card below")
    if image_count:
        parts.append(f"{image_count} attached image{'s' if image_count != 1 else ''}")
    return (
        f"**Instagram story preview** — Post publishes {frames} "
        f"frame{'s' if frames != 1 else ''}: {' + '.join(parts)}. "
        f"Only the **{EXEC_ROLE or '(unconfigured role)'}** role can post."
    )


async def _publish_announcement(source: discord.Message) -> int:
    """Render + publish every frame for `source`. Returns the frame count."""
    token = await _in_thread(_load_token)
    if not token:
        raise RuntimeError(
            "no Instagram access token configured (set IG_ACCESS_TOKEN; see "
            "docs/instagram-setup.md)"
        )
    frames = 0
    text = clean_announcement(source.clean_content)
    if text:
        png = await _in_thread(lambda: render_card(text))
        await _publish_story(await _host_media(png, "image/png"), token)
        frames += 1
    for att in _image_attachments(source):
        # Re-download from Discord and re-host: attachment URLs are signed and
        # expire, and Instagram must be able to fetch anonymously.
        data = await att.read()
        content_type = (att.content_type or "image/jpeg").split(";")[0]
        await _publish_story(await _host_media(data, content_type), token)
        frames += 1
    if not frames:
        raise RuntimeError("nothing to publish (no text and no image attachments)")
    return frames


# --- buttons ------------------------------------------------------------------
def _member_can_post(user: discord.User | discord.Member) -> bool:
    """Same gate as every other bot mutation: the exec role, failing closed."""
    if not EXEC_ROLE or not isinstance(user, discord.Member):
        return False
    return discord.utils.get(user.roles, name=EXEC_ROLE) is not None


class IgStoryAction(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"igstory:(?P<action>post|skip):(?P<mid>[0-9]+)",
):
    """Post/Skip button carrying the source announcement's message id, so a
    click after a bot restart can still re-fetch and publish (dynamic items are
    rebuilt from the custom_id; no registered view instance needed)."""

    def __init__(self, action: str, mid: int, disabled: bool = False) -> None:
        label, style = (
            ("Post to Instagram", discord.ButtonStyle.success)
            if action == "post"
            else ("Skip", discord.ButtonStyle.secondary)
        )
        super().__init__(
            discord.ui.Button(
                label=label, style=style, custom_id=f"igstory:{action}:{mid}", disabled=disabled
            )
        )
        self.action = action
        self.mid = mid

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "IgStoryAction":
        return cls(match["action"], int(match["mid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _member_can_post(interaction.user):
            need = f"the {EXEC_ROLE} role" if EXEC_ROLE else "a role that isn't configured yet"
            await interaction.response.send_message(f"You need {need} to do that.", ephemeral=True)
            return
        if self.action == "skip":
            await interaction.response.edit_message(
                content=f"Skipped — not posted to Instagram (by {interaction.user.display_name}).",
                view=None,
            )
            return
        # Grey the buttons while publishing so a second click can't double-post.
        await interaction.response.edit_message(
            content="Posting to Instagram…", view=story_view(self.mid, disabled=True)
        )
        try:
            source = await interaction.channel.fetch_message(self.mid)
        except discord.NotFound:
            await interaction.message.edit(
                content="The original announcement was deleted; nothing posted.", view=None
            )
            return
        try:
            frames = await _publish_announcement(source)
        except Exception as e:
            print(f"[instagram] publish failed for message {self.mid}: {e!r}")
            await interaction.message.edit(
                content=f"⚠️ Posting failed: {e}\nFix the problem and press Post again.",
                view=story_view(self.mid),
            )
            return
        await interaction.message.edit(
            content=(
                f"✅ Posted to Instagram as {frames} story frame"
                f"{'s' if frames != 1 else ''} (by {interaction.user.display_name})."
            ),
            view=None,
        )


def story_view(mid: int, disabled: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(IgStoryAction("post", mid, disabled=disabled))
    view.add_item(IgStoryAction("skip", mid, disabled=disabled))
    return view


# --- announcement listener ------------------------------------------------------
async def on_announcement(message: discord.Message) -> None:
    """Called from __main__'s on_message for every message the bot sees; replies
    with a story preview when it's a fresh announcement in the watched channel."""
    if not configured() or message.author.bot:
        return
    if str(message.channel.id) != CHANNEL_ID:
        return
    text = clean_announcement(message.clean_content)
    images = _image_attachments(message)
    if not text and not images:
        return  # nothing a story could show (e.g. a bare file/sticker)
    try:
        kwargs = {}
        if text:
            png = await _in_thread(lambda: render_card(text))
            kwargs["file"] = discord.File(BytesIO(png), filename="story-preview.png")
        await message.reply(
            content=preview_note(bool(text), len(images)),
            view=story_view(message.id),
            **kwargs,
        )
    except Exception as e:
        # Never let preview trouble take down the message handler.
        print(f"[instagram] preview failed for message {message.id}: {e!r}")


# --- token refresh loop -----------------------------------------------------------
_refreshing = False


async def _refresh_once() -> None:
    age = await _in_thread(_token_age_days)
    if age is None or age < REFRESH_AFTER_DAYS:
        return
    token = await _in_thread(_load_token)
    s = await _http()
    async with s.get(
        # The refresh endpoint is unversioned, unlike the publish endpoints.
        f"{GRAPH_HOST}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token},
    ) as r:
        payload = await r.json()
    if "access_token" in payload:
        await _in_thread(lambda: _save_token(payload["access_token"]))
        print("[instagram] access token refreshed.")
    else:
        # Loud on purpose: if this keeps failing the token dies at 60 days and
        # an exec must issue a new one (docs/instagram-setup.md).
        print(f"[instagram] TOKEN REFRESH FAILED: {_graph_error(payload)}")


async def _refresh_loop(client: discord.Client) -> None:
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            await _refresh_once()
        except Exception as e:
            print(f"[instagram] token refresh error: {e!r}")
        await asyncio.sleep(24 * 3600)


def start_refresh(client: discord.Client) -> None:
    """Start the daily token-refresh loop, once. No-op when unconfigured."""
    global _refreshing
    if _refreshing or not configured():
        return
    _refreshing = True
    client.loop.create_task(_refresh_loop(client))


def register_persistent(client: discord.Client) -> None:
    """Register the Post/Skip buttons for dispatch. MUST be called with the
    event loop running (Client.setup_hook), never at import time — see the
    matching warning in complaints.register_persistent."""
    client.add_dynamic_items(IgStoryAction)
