"""/ig-media: gated upload, public unguessable GET, and the age sweep."""
import os
import time

import pytest

import rbga.api.routers.igmedia as igmedia

PNG = b"\x89PNG\r\n\x1a\nfakebytes"


@pytest.fixture(autouse=True)
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(igmedia, "MEDIA_DIR", tmp_path)
    return tmp_path


def _upload(client, token, body=PNG, content_type="image/png"):
    return client.post(
        "/ig-media", content=body, headers={"X-API-Token": token, "Content-Type": content_type}
    )


def test_upload_requires_write_token(client, write_token):
    r = client.post("/ig-media", content=PNG, headers={"Content-Type": "image/png"})
    assert r.status_code == 403
    r = _upload(client, "wrong-token")
    assert r.status_code == 403


def test_upload_then_public_get_roundtrip(client, write_token):
    r = _upload(client, write_token)
    assert r.status_code == 200
    path = r.json()["path"]
    assert path.startswith("/ig-media/") and path.endswith(".png")
    # The GET is public (Instagram fetches with no credentials).
    got = client.get(path)
    assert got.status_code == 200
    assert got.content == PNG
    assert got.headers["content-type"] == "image/png"


def test_upload_rejects_non_image_content_type(client, write_token):
    assert _upload(client, write_token, content_type="text/html").status_code == 415


def test_upload_rejects_empty_body(client, write_token):
    assert _upload(client, write_token, body=b"").status_code == 400


def test_upload_rejects_oversize_body(client, write_token, monkeypatch):
    monkeypatch.setattr(igmedia, "MAX_BYTES", 10)
    assert _upload(client, write_token, body=b"x" * 11).status_code == 413


@pytest.mark.parametrize(
    "name",
    [
        "notahexname.png",  # not a uuid4 hex stem
        "a" * 32 + ".png",  # right length, not hex
        "0" * 32 + ".exe",  # unknown extension
        "%2e%2e%2fsecrets.png",  # traversal probe
    ],
)
def test_get_rejects_names_we_never_generate(client, name):
    assert client.get(f"/ig-media/{name}").status_code == 404


def test_get_missing_file_404s(client):
    assert client.get(f"/ig-media/{'0' * 32}.png").status_code == 404


def test_upload_sweeps_expired_files(client, write_token, media_dir):
    old = media_dir / ("a" * 32 + ".png")
    old.write_bytes(PNG)
    expired = time.time() - igmedia.MAX_AGE_SECONDS - 60
    os.utime(old, (expired, expired))
    fresh = _upload(client, write_token)
    assert fresh.status_code == 200
    assert not old.exists()  # swept
    assert (media_dir / fresh.json()["name"]).exists()  # new upload kept
