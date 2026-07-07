"""Pure routing logic for the Discord complaints handler (no Discord needed)."""
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
    assert c._ID_RE.search("Complaint #42").group(1) == "42"


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
