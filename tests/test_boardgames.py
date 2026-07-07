"""Board-game tags: storage round-trip, tag filter, BGG category parsing,
and the BGG enrichment pass."""
import asyncio

import pytest
from sqlalchemy import select

from rbga.bgg import parse_thing
from rbga.bot.boardgames import parse_tags
from rbga.db import enrich_boardgames
from rbga.db.database import SessionLocal
from rbga.db.enrich_boardgames import enrich, needs_image
from rbga.db.models import BoardGame


def _add(client, token, title, tags=None):
    r = client.post(
        "/board-games",
        json={"title": title, "tags": tags},
        headers={"X-API-Token": token},
    )
    assert r.status_code == 201
    return r.json()


def test_tags_round_trip(client, write_token):
    created = _add(client, write_token, "Catan", ["Strategy", "Negotiation"])
    assert created["tags"] == ["Strategy", "Negotiation"]
    fetched = client.get(f"/board-games/{created['id']}").json()
    assert fetched["tags"] == ["Strategy", "Negotiation"]


def test_tags_default_to_null(client, write_token):
    created = _add(client, write_token, "Untagged")
    assert created["tags"] is None


def test_list_filters_by_tag_case_insensitively(client, write_token):
    _add(client, write_token, "Catan", ["Strategy"])
    _add(client, write_token, "Telestrations", ["Party"])
    _add(client, write_token, "Untagged")  # must not break the filter

    titles = [g["title"] for g in client.get("/board-games", params={"tag": "strategy"}).json()]
    assert titles == ["Catan"]
    assert client.get("/board-games", params={"tag": "nope"}).json() == []
    # No filter returns everything.
    assert len(client.get("/board-games").json()) == 3


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Strategy, Party game", ["Strategy", "Party game"]),
        ("  solo , , coop ", ["solo", "coop"]),
        ("single", ["single"]),
        ("", None),
        (None, None),
        (" , ,", None),
    ],
)
def test_parse_tags(raw, expected):
    assert parse_tags(raw) == expected


def test_bgg_categories_become_tags():
    xml = b"""<items><item type="boardgame" id="13">
      <name type="primary" value="Catan"/>
      <link type="boardgamecategory" id="1021" value="Economic"/>
      <link type="boardgamecategory" id="1026" value="Negotiation"/>
      <link type="boardgamepublisher" id="37" value="KOSMOS"/>
      <minplayers value="3"/><maxplayers value="4"/>
    </item></items>"""
    data = parse_thing(xml)
    assert data["tags"] == ["Economic", "Negotiation"]
    assert data["publisher"] == "KOSMOS"  # category scan doesn't eat other links


def test_bgg_no_categories_means_no_tags():
    xml = b"""<items><item type="boardgame" id="1">
      <name type="primary" value="Mystery Game"/>
    </item></items>"""
    assert parse_thing(xml)["tags"] is None


# --- enrichment pass ----------------------------------------------------------
@pytest.mark.parametrize(
    "image,needed",
    [
        (None, True),
        ("", True),
        ("SomePhoto.jpg", True),  # bare SharePoint filename from the CSV import
        ("https://cf.geekdo-images.com/x.jpg", False),
        ("http://example.com/y.png", False),
    ],
)
def test_needs_image(image, needed):
    assert needs_image(image) is needed


def test_enrich_fills_missing_fields_only(monkeypatch):
    with SessionLocal() as db:
        db.add(
            BoardGame(
                title="Catan",
                bgg_link="https://boardgamegeek.com/boardgame/13/catan",
                image="Catan.jpg",  # unusable filename: should be replaced
                publisher="Hand Entered",  # should be kept
            )
        )
        db.add(BoardGame(title="No Link"))  # untouched, reported as missing image
        db.commit()

    async def fake_fetch(bgg_id):
        assert bgg_id == 13
        return {
            "title": "Catan",
            "publisher": "KOSMOS",
            "min_players": 3,
            "max_players": 4,
            "image": "https://cf.geekdo-images.com/catan.jpg",
            "tags": ["Economic", "Negotiation"],
        }

    monkeypatch.setattr(enrich_boardgames, "fetch_game", fake_fetch)
    updated = asyncio.run(enrich(delay=0))
    assert updated == 1

    with SessionLocal() as db:
        catan = db.scalars(select(BoardGame).filter_by(title="Catan")).one()
        assert catan.image == "https://cf.geekdo-images.com/catan.jpg"
        assert catan.tags == ["Economic", "Negotiation"]
        assert catan.publisher == "Hand Entered"  # not overwritten
        assert catan.min_players == 3 and catan.max_players == 4
        nolink = db.scalars(select(BoardGame).filter_by(title="No Link")).one()
        assert nolink.image is None
