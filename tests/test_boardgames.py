"""Board-game tags: storage round-trip, tag filter, BGG category parsing,
the BGG enrichment pass, and the /game gallery rendering."""
import asyncio

import pytest
from sqlalchemy import select

from rbga.bgg import parse_thing
from rbga.bot import boardgames as bot_bg
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
      <thumbnail>https://cf.geekdo-images.com/x__thumb.jpg</thumbnail>
      <minplayers value="3"/><maxplayers value="4"/>
    </item></items>"""
    data = parse_thing(xml)
    assert data["tags"] == ["Economic", "Negotiation"]
    assert data["publisher"] == "KOSMOS"  # category scan doesn't eat other links
    assert data["thumbnail"] == "https://cf.geekdo-images.com/x__thumb.jpg"


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


# --- gallery rendering ----------------------------------------------------------
def _game(**kw) -> BoardGame:
    return BoardGame(**{"title": "Catan", **kw})


@pytest.mark.parametrize("total,pages", [(0, 1), (1, 1), (10, 1), (11, 2), (173, 18)])
def test_gallery_pages(total, pages):
    assert bot_bg.gallery_pages(total) == pages


def test_game_card_uses_url_image_as_thumbnail():
    card = bot_bg.game_card(
        _game(id=9, image="https://cf.geekdo-images.com/x.jpg", tags=["Economic"], owner="RBGA")
    )
    assert card.title == "#9 Catan"
    assert card.thumbnail.url == "https://cf.geekdo-images.com/x.jpg"
    assert "Tags: Economic" in card.description
    assert "Owner: RBGA" in card.description


def test_game_card_prefers_small_thumbnail_over_original():
    # Discord's proxy times out on multi-MB originals; the card must use the
    # small BGG thumbnail variant when one is stored.
    card = bot_bg.game_card(
        _game(
            id=9,
            image="https://cf.geekdo-images.com/x__original.jpg",
            thumbnail="https://cf.geekdo-images.com/x__thumb.jpg",
        )
    )
    assert card.thumbnail.url == "https://cf.geekdo-images.com/x__thumb.jpg"


def test_game_card_skips_bare_filename_image():
    card = bot_bg.game_card(_game(id=1, image="Catan.jpg"))
    assert card.thumbnail.url is None


def test_gallery_page_embeds_slices_ten_per_page():
    games = [_game(id=i, title=f"G{i}") for i in range(1, 24)]  # 23 games
    assert len(bot_bg.gallery_page_embeds(games, 0)) == 10
    assert len(bot_bg.gallery_page_embeds(games, 2)) == 3
    assert bot_bg.gallery_page_embeds(games, 1)[0].title == "#11 G11"


def test_gallery_view_button_states():
    one_page = bot_bg.GalleryView([_game(id=1)])
    assert one_page.prev_btn.disabled and one_page.next_btn.disabled

    many = bot_bg.GalleryView([_game(id=i) for i in range(1, 24)])
    assert many.prev_btn.disabled and not many.next_btn.disabled
    many.page = 2  # last page of 23 games
    many._sync_buttons()
    assert not many.prev_btn.disabled and many.next_btn.disabled


def test_list_pages_chunks_by_message_budget():
    # Long titles force multiple text pages; every game appears exactly once.
    games = [_game(id=i, title=f"Game {i} " + "x" * 60) for i in range(1, 61)]
    pages = bot_bg.list_pages(games)
    assert len(pages) > 1
    assert all(len(p) <= bot_bg.MAX_LIST_CHARS for p in pages)
    joined = "\n".join(pages)
    assert all(f"`#{i}`" in joined for i in range(1, 61))


def test_list_view_renders_header_and_buttons():
    view = bot_bg.ListView(60, ["page one", "page two"])
    assert view.prev_btn.disabled and not view.next_btn.disabled
    first = view.render()
    assert first["content"].startswith("**60 game(s)**, page 1/2")
    assert "page one" in first["content"]
    view.page = 1
    view._sync_buttons()
    assert not view.prev_btn.disabled and view.next_btn.disabled
    assert "page two" in view.render()["content"]


def test_export_csv_round_trips():
    import csv as _csv
    import io as _io

    games = [
        _game(id=1, title="Catan", owner="RBGA", condition="Fair",
              price=45.0, tags=["Economic", "Negotiation"]),
        _game(id=2, title="Uno, Deluxe"),  # comma in title must survive quoting
    ]
    rows = list(_csv.reader(_io.StringIO(bot_bg.export_csv(games))))
    assert rows[0] == bot_bg._EXPORT_FIELDS
    assert len(rows) == 3
    catan = dict(zip(rows[0], rows[1]))
    assert catan["title"] == "Catan"
    assert catan["tags"] == "Economic; Negotiation"
    assert catan["price"] == "45.0"
    uno = dict(zip(rows[0], rows[2]))
    assert uno["title"] == "Uno, Deluxe"
    assert uno["tags"] == ""


# --- sell price, stocktake, owner contacts --------------------------------------
@pytest.mark.parametrize(
    "price,condition,expected",
    [
        (100.0, "Like New", 70.0),
        (100.0, "Fair", 40.0),
        (100.0, "Damaged", 15.0),
        (100.0, "Damaged, Missing Pieces", 5.0),
        (100.0, None, 50.0),  # unknown condition uses the default factor
        (None, "Like New", None),  # nothing to estimate from
    ],
)
def test_estimate_sell_price(price, condition, expected):
    assert bot_bg.estimate_sell_price(price, condition) == expected


def test_sell_price_display_prefers_manual():
    manual = _game(id=1, price=100.0, condition="Fair", sell_price=25.0)
    assert bot_bg.sell_price_display(manual) == "$25.00"
    estimated = _game(id=2, price=100.0, condition="Fair")
    assert bot_bg.sell_price_display(estimated) == "~$40.00 (est.)"
    nothing = _game(id=3)
    assert bot_bg.sell_price_display(nothing) is None


def test_game_line_flags_missing():
    assert "⚠ MISSING" in bot_bg._game_line(_game(id=1, missing=True))
    assert "MISSING" not in bot_bg._game_line(_game(id=2, missing=False))


def test_export_includes_sell_and_stocktake_columns():
    import csv as _csv
    import io as _io
    from datetime import datetime as _dt

    games = [
        _game(id=1, price=100.0, condition="Fair", sell_price=25.0,
              missing=False, last_seen_at=_dt(2026, 7, 7, 3, 0)),
        _game(id=2, title="Lost Game", missing=True),
    ]
    rows = list(_csv.reader(_io.StringIO(bot_bg.export_csv(games))))
    head = rows[0]
    first = dict(zip(head, rows[1]))
    assert first["sell_price"] == "25.0"
    assert first["sell_estimate"] == "40.0"
    assert first["last_seen_at"] == "2026-07-07"
    assert first["missing"] == ""
    lost = dict(zip(head, rows[2]))
    assert lost["missing"] == "yes"


def test_owner_contact_upsert_round_trip():
    bot_bg.set_owner_contact("Quan", "quan#1234")
    assert bot_bg.get_owner_contact("Quan") == "quan#1234"
    bot_bg.set_owner_contact("Quan", "quan@rmit.edu.au")  # update, not duplicate
    assert bot_bg.get_owner_contact("Quan") == "quan@rmit.edu.au"
    assert bot_bg.get_owner_contact("Nobody") is None


def test_owner_contact_not_in_public_api(client, write_token):
    # The public board-games payload must never carry owner contact details.
    bot_bg.set_owner_contact("Quan", "quan@rmit.edu.au")
    _add(client, write_token, "Bark Avenue")
    payload = client.get("/board-games").json()
    assert "contact" not in payload[0]
    assert "quan@rmit.edu.au" not in str(payload)


# --- add/edit forms --------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [("$45", 45.0), ("45.50", 45.5), (" 12 ", 12.0), ("", None), (None, None)],
)
def test_parse_money(raw, expected):
    assert bot_bg.parse_money(raw) == expected


def test_parse_money_rejects_junk():
    with pytest.raises(ValueError, match="isn't a price"):
        bot_bg.parse_money("cheap")


def test_edit_modal_prefills_current_values():
    g = _game(id=7, title="Catan", owner="RBGA", condition="Fair", price=45.0)
    modal = bot_bg.EditGameModal(g)
    assert modal.title == "Edit #7 Catan"
    assert modal.gid == 7
    assert modal.game_title.default == "Catan"
    assert modal.owner.default == "RBGA"
    assert modal.condition.default == "Fair"
    assert modal.price.default == "45"
    assert modal.sell_price.default == ""  # unset stays blank


def test_edit_modal_title_truncated_to_discord_cap():
    g = _game(id=148, title="Arkham Horror: The Card Game and a very long name")
    assert len(bot_bg.EditGameModal(g).title) <= 45
