"""Public POST /complaints is throttled and size-capped."""
import rbga.api.ratelimit as ratelimit

_BODY = {"category": "member", "body": "something happened"}


def test_post_over_limit_returns_429(client, monkeypatch):
    # Pin a small deterministic limit regardless of the ambient env.
    monkeypatch.setattr(ratelimit, "_LIMIT", 3)
    monkeypatch.setattr(ratelimit, "_WINDOW", 60.0)
    ratelimit._reset()

    codes = [client.post("/complaints", json=_BODY).status_code for _ in range(4)]
    assert codes[:3] == [201, 201, 201]  # within the window
    assert codes[3] == 429  # over the limit


def test_oversized_body_rejected(client):
    r = client.post("/complaints", json={"category": "member", "body": "x" * 5001})
    assert r.status_code == 422


def test_empty_body_rejected(client):
    r = client.post("/complaints", json={"category": "member", "body": ""})
    assert r.status_code == 422


def test_reviewer_token_bypasses_rate_limit(client, reviewer_token, monkeypatch):
    # The bot forwards Discord submissions with the reviewer token — not throttled.
    monkeypatch.setattr(ratelimit, "_LIMIT", 2)
    monkeypatch.setattr(ratelimit, "_WINDOW", 60.0)
    ratelimit._reset()

    headers = {"X-Reviewer-Token": reviewer_token}
    codes = [client.post("/complaints", json=_BODY, headers=headers).status_code for _ in range(5)]
    assert codes == [201] * 5  # well over the limit, all accepted

    # Body caps still apply even for the trusted caller.
    assert client.post("/complaints", json={"category": "member", "body": ""}, headers=headers).status_code == 422
