"""REST write endpoints are gated by RBGA_API_TOKEN; reads stay open."""
import rbga.api.auth as auth


def test_post_key_rejected_without_token(client, write_token):
    r = client.post("/keys", json={"colour": "red", "campus": "city"})
    assert r.status_code == 403


def test_post_key_rejected_with_wrong_token(client, write_token):
    r = client.post(
        "/keys", json={"colour": "red", "campus": "city"}, headers={"X-API-Token": "nope"}
    )
    assert r.status_code == 403


def test_post_key_allowed_with_correct_token(client, write_token):
    r = client.post(
        "/keys", json={"colour": "red", "campus": "city"}, headers={"X-API-Token": write_token}
    )
    assert r.status_code == 201


def test_post_key_fails_closed_when_token_unconfigured(client, monkeypatch):
    # No RBGA_API_TOKEN set -> every write is rejected even with a header.
    monkeypatch.setattr(auth, "_WRITE_TOKEN", None)
    r = client.post(
        "/keys", json={"colour": "red", "campus": "city"}, headers={"X-API-Token": "anything"}
    )
    assert r.status_code == 403


def test_reads_need_no_token(client):
    assert client.get("/keys").status_code == 200
    assert client.get("/board-games").status_code == 200


def test_boardgame_write_rejected_without_token(client, write_token):
    assert client.post("/board-games", json={"title": "Catan"}).status_code == 403


def test_boardgame_write_allowed_with_token(client, write_token):
    r = client.post(
        "/board-games", json={"title": "Catan"}, headers={"X-API-Token": write_token}
    )
    assert r.status_code == 201
