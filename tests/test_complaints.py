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


def test_president_submission_is_rejected(client):
    # No impartial internal handler exists above the president, so the club does
    # not accept these — the submitter is redirected to RUSU (policy §5).
    r = client.post("/complaints", json={"category": "president", "body": "about the pres"})
    assert r.status_code == 400
    assert "RUSU" in r.json()["detail"]


def test_get_single_complaint_requires_reviewer(client, reviewer_token):
    cid = _submit(client)
    assert client.get(f"/complaints/{cid}").status_code == 403
    ok = client.get(f"/complaints/{cid}", headers={"X-Reviewer-Token": reviewer_token})
    assert ok.status_code == 200
    assert ok.json()["id"] == cid


def test_mark_routed_stamps_routed_at(client, reviewer_token):
    cid = _submit(client)
    headers = {"X-Reviewer-Token": reviewer_token}
    assert client.get(f"/complaints/{cid}", headers=headers).json()["routed_at"] is None

    routed = client.post(f"/complaints/{cid}/routed", headers=headers)
    assert routed.status_code == 200
    assert routed.json()["routed_at"] is not None

    # Idempotent: re-marking keeps the original timestamp.
    first = routed.json()["routed_at"]
    again = client.post(f"/complaints/{cid}/routed", headers=headers)
    assert again.json()["routed_at"] == first


def test_config_requires_reviewer(client, reviewer_token):
    assert client.get("/complaints/config").status_code == 403
    assert client.put("/complaints/config", json={}).status_code == 403


def test_config_defaults_empty_then_saves(client, reviewer_token):
    headers = {"X-Reviewer-Token": reviewer_token}

    default = client.get("/complaints/config", headers=headers)
    assert default.status_code == 200
    assert default.json()["committee_channel_id"] is None

    saved = client.put(
        "/complaints/config",
        json={"committee_channel_id": "111", "president_user_id": "222"},
        headers=headers,
    )
    assert saved.status_code == 200
    assert saved.json()["committee_channel_id"] == "111"
    assert saved.json()["president_user_id"] == "222"

    # Partial update leaves the untouched field as it was.
    updated = client.put(
        "/complaints/config", json={"exec_channel_id": "333"}, headers=headers
    )
    body = updated.json()
    assert body == {
        "committee_channel_id": "111",
        "exec_channel_id": "333",
        "president_user_id": "222",
    }
