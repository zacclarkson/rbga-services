"""Instagram announcement mirror: publish sequencing, token lifecycle, and the
restart-surviving buttons. Graph/API HTTP is faked; no Discord or network."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from rbga.bot import instagram as ig


# --- fake aiohttp ------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)


class FakeSession:
    """Hands out canned responses in order and records every request."""

    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict | bytes | None]] = []

    def post(self, url, data=None, headers=None):
        self.calls.append(("POST", url, data))
        return self.responses.pop(0)

    def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        return self.responses.pop(0)


@pytest.fixture
def fake_http(monkeypatch):
    def install(responses: list[FakeResponse]) -> FakeSession:
        session = FakeSession(responses)

        async def _fake_http():
            return session

        monkeypatch.setattr(ig, "_http", _fake_http)
        return session

    monkeypatch.setattr(ig, "_PUBLISH_RETRY_SECONDS", 0)
    monkeypatch.setattr(ig, "IG_USER_ID", "17840000000000000")
    return install


# --- two-step Graph publish ---------------------------------------------------
def test_publish_story_creates_container_then_publishes(fake_http):
    s = fake_http([FakeResponse(200, {"id": "111"}), FakeResponse(200, {"id": "222"})])
    asyncio.run(ig._publish_story("https://x/ig-media/a.png", "tok"))
    (m1, url1, data1), (m2, url2, data2) = s.calls
    assert url1.endswith("/17840000000000000/media")
    assert data1["media_type"] == "STORIES"
    assert data1["image_url"] == "https://x/ig-media/a.png"
    assert url2.endswith("/17840000000000000/media_publish")
    assert data2["creation_id"] == "111"


def test_publish_story_retries_until_container_ready(fake_http):
    s = fake_http(
        [
            FakeResponse(200, {"id": "111"}),
            FakeResponse(400, {"error": {"message": "Media ID is not available"}}),
            FakeResponse(400, {"error": {"message": "Media ID is not available"}}),
            FakeResponse(200, {"id": "222"}),
        ]
    )
    asyncio.run(ig._publish_story("https://x/a.png", "tok"))
    assert len(s.calls) == 4  # 1 container + 3 publish attempts


def test_publish_story_surfaces_container_error(fake_http):
    fake_http([FakeResponse(400, {"error": {"message": "token expired"}})])
    with pytest.raises(RuntimeError, match="token expired"):
        asyncio.run(ig._publish_story("https://x/a.png", "tok"))


def test_publish_story_gives_up_after_retries(fake_http):
    err = FakeResponse(400, {"error": {"message": "still not ready"}})
    fake_http([FakeResponse(200, {"id": "111"})] + [err] * ig._PUBLISH_RETRIES)
    with pytest.raises(RuntimeError, match="still not ready"):
        asyncio.run(ig._publish_story("https://x/a.png", "tok"))


# --- frame assembly ------------------------------------------------------------
def _fake_message(content="", attachments=()):
    msg = MagicMock()
    msg.clean_content = content
    msg.attachments = list(attachments)
    return msg


def _fake_image_attachment(content_type="image/jpeg", data=b"jpegbytes"):
    att = MagicMock()
    att.content_type = content_type
    att.read = AsyncMock(return_value=data)
    return att


def test_publish_announcement_text_plus_images(monkeypatch):
    monkeypatch.setattr(ig, "_load_token", lambda: "tok")
    hosted, published = [], []

    async def fake_host(data, content_type):
        hosted.append(content_type)
        return f"https://x/{len(hosted)}"

    async def fake_publish(url, token):
        published.append((url, token))

    monkeypatch.setattr(ig, "_host_media", fake_host)
    monkeypatch.setattr(ig, "_publish_story", fake_publish)

    msg = _fake_message("**Games night!**", [_fake_image_attachment(), _fake_image_attachment("image/png", b"png")])
    frames = asyncio.run(ig._publish_announcement(msg))
    assert frames == 3
    assert hosted == ["image/png", "image/jpeg", "image/png"]  # card first, then attachments
    assert all(token == "tok" for _, token in published)


def test_publish_announcement_needs_a_token(monkeypatch):
    monkeypatch.setattr(ig, "_load_token", lambda: None)
    with pytest.raises(RuntimeError, match="access token"):
        asyncio.run(ig._publish_announcement(_fake_message("hello")))


def test_publish_announcement_skips_non_image_attachments(monkeypatch):
    monkeypatch.setattr(ig, "_load_token", lambda: "tok")
    monkeypatch.setattr(ig, "_host_media", AsyncMock(return_value="https://x/1"))
    publish = AsyncMock()
    monkeypatch.setattr(ig, "_publish_story", publish)
    pdf = _fake_image_attachment(content_type="application/pdf")
    frames = asyncio.run(ig._publish_announcement(_fake_message("hi", [pdf])))
    assert frames == 1  # just the text card
    pdf.read.assert_not_awaited()


# --- token lifecycle (real DB via the fresh_db fixture) -------------------------
def test_load_token_none_when_unseeded(monkeypatch):
    monkeypatch.setattr(ig, "ENV_TOKEN", None)
    assert ig._load_token() is None


def test_load_token_seeds_from_env_once(monkeypatch):
    monkeypatch.setattr(ig, "ENV_TOKEN", "tok1")
    assert ig._load_token() == "tok1"
    # A refresh updates the DB; the unchanged env seed no longer shadows it.
    ig._save_token("refreshed")
    assert ig._load_token() == "refreshed"


def test_changed_env_token_reseeds_over_db(monkeypatch):
    monkeypatch.setattr(ig, "ENV_TOKEN", "tok1")
    ig._load_token()
    ig._save_token("dead-by-now")
    # Exec pastes a NEW token into .env: it must win over the stale DB row.
    monkeypatch.setattr(ig, "ENV_TOKEN", "tok2")
    assert ig._load_token() == "tok2"


def test_token_age_fresh_after_save(monkeypatch):
    monkeypatch.setattr(ig, "ENV_TOKEN", None)
    assert ig._token_age_days() is None
    ig._save_token("tok")
    assert ig._token_age_days() < 0.01


# --- preview + buttons -----------------------------------------------------------
def test_preview_note_counts_frames():
    note = ig.preview_note(has_text=True, image_count=2)
    assert "3 frames" in note
    assert "text card" in note and "2 attached images" in note
    note = ig.preview_note(has_text=True, image_count=0)
    assert "1 frame" in note


@pytest.mark.parametrize("action", ["post", "skip"])
def test_dynamic_template_matches_and_extracts(action):
    pat = ig.IgStoryAction.__discord_ui_compiled_template__
    m = pat.fullmatch(f"igstory:{action}:42")
    assert m is not None and m["mid"] == "42"


@pytest.mark.parametrize("custom_id", ["igstory:post", "igstory:nuke:1", "igstory:post:x", "complaint:view:1"])
def test_dynamic_template_rejects_junk(custom_id):
    pat = ig.IgStoryAction.__discord_ui_compiled_template__
    assert pat.fullmatch(custom_id) is None


def test_story_view_builder_embeds_the_message_id():
    view = ig.story_view(7)
    assert view.timeout is None
    assert [item.custom_id for item in view.children] == ["igstory:post:7", "igstory:skip:7"]
    disabled = ig.story_view(7, disabled=True)
    assert all(item.item.disabled for item in disabled.children)


def _fake_button_interaction(custom_id: str) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    inter.data = {"custom_id": custom_id, "component_type": 2}
    inter.message = MagicMock()
    inter.message.id = 999888777
    inter.user = MagicMock()
    inter.user.display_name = "Some Exec"
    inter.response = MagicMock()
    inter.response.edit_message = AsyncMock()
    inter.response.send_message = AsyncMock()
    return inter


def test_registered_dynamic_button_actually_dispatches(monkeypatch):
    """Same regression class as the complaints legacy-view test: after
    register_persistent runs in-loop (setup_hook), a click with no live view
    instance (i.e. after a restart) must route through discord.py's real view
    store to our callback. The store's final step rebuilds the view from the
    real message's Discord components — impossible for a mock message — so
    only that reconstruction is stubbed; registration, template matching, and
    the factory/callback all run for real."""
    monkeypatch.setattr(ig, "_member_can_post", lambda user: True)
    inter = _fake_button_interaction("igstory:skip:1")

    async def scenario():
        client = discord.Client(intents=discord.Intents.default())
        ig.register_persistent(client)  # in-loop, as setup_hook does
        store = client._connection._view_store

        async def run_matched_item(component_type, factory, interaction, custom_id, match):
            item = await factory.from_custom_id(interaction, MagicMock(), match)
            await item.callback(interaction)

        monkeypatch.setattr(store, "schedule_dynamic_item_call", run_matched_item)
        store.dispatch_view(2, "igstory:skip:1", inter)
        await asyncio.sleep(0.05)  # let the dispatch task run

    asyncio.run(scenario())
    inter.response.edit_message.assert_awaited_once()
    assert "Skipped" in inter.response.edit_message.await_args.kwargs["content"]


def test_button_denies_without_exec_role():
    # Fail closed: a MagicMock user is not a discord.Member holding the role.
    inter = _fake_button_interaction("igstory:post:1")

    async def scenario():
        item = await ig.IgStoryAction.from_custom_id(
            inter, None, ig.IgStoryAction.__discord_ui_compiled_template__.fullmatch("igstory:post:1")
        )
        await item.callback(inter)

    asyncio.run(scenario())
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.await_args.kwargs.get("ephemeral") is True
