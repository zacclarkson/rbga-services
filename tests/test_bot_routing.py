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


def test_complaint_id_regex_reads_the_embed_title():
    assert c._ID_RE.search("Complaint #42").group(1) == "42"
