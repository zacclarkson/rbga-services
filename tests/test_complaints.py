"""Complaints: public submit, reviewer-gated management (escalate/close)."""


def _submit(client) -> int:
    r = client.post("/complaints", json={"category": "member", "body": "something happened"})
    assert r.status_code == 201
    return r.json()["id"]


def test_submit_is_public(client):
    # No token needed to submit.
    _submit(client)


def test_patch_requires_reviewer_token(client, reviewer_token):
    cid = _submit(client)
    r = client.patch(f"/complaints/{cid}", json={"status": "acknowledged"})
    assert r.status_code == 403


def test_patch_unknown_id_404(client, reviewer_token):
    r = client.patch(
        "/complaints/9999",
        json={"status": "acknowledged"},
        headers={"X-Reviewer-Token": reviewer_token},
    )
    assert r.status_code == 404


def test_escalating_sets_status_and_target(client, reviewer_token):
    cid = _submit(client)
    r = client.patch(
        f"/complaints/{cid}",
        json={"escalated_to": "rusu"},
        headers={"X-Reviewer-Token": reviewer_token},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "escalated"
    assert body["escalated_to"] == "rusu"


def test_closing_sets_closed_at_and_reopening_clears_it(client, reviewer_token):
    cid = _submit(client)
    headers = {"X-Reviewer-Token": reviewer_token}

    closed = client.patch(f"/complaints/{cid}", json={"status": "closed"}, headers=headers)
    assert closed.json()["closed_at"] is not None

    reopened = client.patch(f"/complaints/{cid}", json={"status": "acknowledged"}, headers=headers)
    assert reopened.json()["closed_at"] is None
