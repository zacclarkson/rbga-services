"""Pure routing logic for the Discord complaints handler (no Discord needed),
plus a real-library dispatch regression test at the bottom."""
import asyncio
from unittest.mock import MagicMock

import discord
import pytest

from rbga.bot import complaints as c


@pytest.mark.parametrize(
    "category,expected",
    [
        ("member", ("channel", "committee")),
        ("committee", ("channel", "exec")),
        ("exec", ("dm", "president")),
    ],
)
def test_initial_destination(category, expected):
    assert c.destination_for(category, escalated=False) == expected


@pytest.mark.parametrize(
    "category,expected",
    [
        ("member", ("channel", "exec")),
        ("committee", ("dm", "president")),
        ("exec", ("rusu", None)),
    ],
)
def test_escalated_destination(category, expected):
    assert c.destination_for(category, escalated=True) == expected


@pytest.mark.parametrize(
    "category,target",
    [("member", "exec"), ("committee", "president"), ("exec", "rusu")],
)
def test_escalation_target(category, target):
    assert c.next_escalation_target(category) == target


def test_president_is_not_routable():
    # President complaints are rejected at the API, so they never reach routing.
    with pytest.raises(KeyError):
        c.destination_for("president")


@pytest.mark.parametrize(
    "category,ok",
    [("member", True), ("committee", True), ("exec", True), ("president", False)],
)
def test_is_submittable(category, ok):
    # /complain sends president-about complaints to RUSU instead of submitting.
    assert c.is_submittable(category) is ok


def test_complaint_id_regex_reads_the_embed_title():
    # Legacy path: messages posted before the id lived in the custom_id.
    assert c._ID_RE.search("Complaint #42").group(1) == "42"


# --- dynamic buttons (id carried in the custom_id) ----------------------------
@pytest.mark.parametrize("action", ["view", "ack", "escalate", "close"])
def test_dynamic_template_matches_and_extracts(action):
    pat = c.ComplaintAction.__discord_ui_compiled_template__
    m = pat.fullmatch(f"complaint:{action}:42")
    assert m is not None
    assert m["action"] == action
    assert m["id"] == "42"


@pytest.mark.parametrize(
    "custom_id",
    ["complaint:view", "complaint:ack", "complaint:view:", "complaint:nuke:1", "complaint:view:x"],
)
def test_dynamic_template_ignores_legacy_and_junk_ids(custom_id):
    # Legacy ids (no trailing :id) stay with the registered ComplaintView.
    pat = c.ComplaintAction.__discord_ui_compiled_template__
    assert pat.fullmatch(custom_id) is None


def test_complaint_view_builder_embeds_the_id():
    view = c.complaint_view(7)
    assert view.timeout is None
    assert [item.custom_id for item in view.children] == [
        "complaint:view:7",
        "complaint:ack:7",
        "complaint:escalate:7",
        "complaint:close:7",
    ]


def _fake_button_interaction(embed_title: str) -> MagicMock:
    inter = MagicMock(spec=discord.Interaction)
    msg = MagicMock()
    msg.id = 999888777  # a message id no view instance is stored for
    embed = MagicMock()
    embed.title = embed_title
    msg.embeds = [embed]
    inter.message = msg
    inter.data = {"custom_id": "complaint:view", "component_type": 2}
    return inter


def test_registered_legacy_view_actually_dispatches(monkeypatch):
    """Regression: a View constructed with NO running event loop is silently
    undispatchable (discord.py's _dispatch_item drops the click without any
    error or log). register_persistent() must therefore run inside the loop
    (setup_hook). This drives discord.py's real dispatch path to prove a
    restart-surviving click on a legacy message reaches the handler."""
    calls = []

    async def fake_do_view(interaction, cid):
        calls.append(cid)

    monkeypatch.setattr(c, "_do_view", fake_do_view)

    async def scenario():
        client = discord.Client(intents=discord.Intents.default())
        c.register_persistent(client)  # in-loop, as setup_hook does
        store = client._connection._view_store
        store.dispatch_view(2, "complaint:view", _fake_button_interaction("Complaint #1"))
        await asyncio.sleep(0.05)  # let the dispatch task run

    asyncio.run(scenario())
    assert calls == [1]


# --- setup wizard access + config resolution --------------------------------
def test_is_authorised_owner():
    assert c.is_authorised(user_id=7, owner_id=7, user_role_names=[], admin_role="Exec")


def test_is_authorised_admin_role():
    assert c.is_authorised(user_id=1, owner_id=7, user_role_names=["Exec"], admin_role="Exec")


def test_is_authorised_denies_other():
    assert not c.is_authorised(user_id=1, owner_id=7, user_role_names=["Member"], admin_role="Exec")


def test_is_authorised_denies_in_dm():
    # No guild -> owner_id is None; a non-owner without the role is denied.
    assert not c.is_authorised(user_id=1, owner_id=None, user_role_names=[], admin_role="Exec")


@pytest.mark.parametrize(
    "category,targets,ready",
    [
        # Each tier only needs *its own* destination set.
        ("member", {"committee": "100", "exec": None, "president": None}, True),
        ("committee", {"committee": None, "exec": "200", "president": None}, True),
        ("exec", {"committee": None, "exec": None, "president": "300"}, True),
        # Missing destination for the tier -> not ready, even if others are set.
        ("member", {"committee": None, "exec": "200", "president": "300"}, False),
        ("committee", {"committee": "100", "exec": None, "president": "300"}, False),
        ("exec", {"committee": "100", "exec": "200", "president": None}, False),
    ],
)
def test_tier_ready(category, targets, ready):
    # /complain refuses up front (asking for /complaints-setup) when the tier a
    # complaint would route to has no destination configured.
    assert c.tier_ready(category, targets) is ready


def test_merge_targets_saved_wins_then_env_then_none():
    config = {"committee": "100", "exec": None, "president": None}
    env = {"committee": "999", "exec": "200", "president": None}
    assert c.merge_targets(config, env) == {
        "committee": "100",  # saved wins
        "exec": "200",  # falls back to env
        "president": None,  # neither set
    }
