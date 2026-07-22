"""Board-game tags: storage round-trip, tag filter, BGG category parsing,
the BGG enrichment pass, and the /game gallery rendering."""
import asyncio
from typing import get_args

import pytest
from sqlalchemy import func, select

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


_OWNER_CSV = '''"First Name/s","Last Name","Phone Number","Email","ID"
"Zac","Clarkson","0400000000","zac@example.com","1"
"Kian Seen","Goh",," kian@example.com","2"
"RBGA",,,,"3"
"Paul","Baquiran",,,"4"
'''


def test_import_owners_parses_names_and_contacts():
    from rbga.db.import_owners import parse_rows

    pairs = dict(parse_rows(_OWNER_CSV.splitlines(keepends=True)))
    # Owner.name is the first name, matching how games label owners.
    assert pairs["Zac"] == "Zac Clarkson, 0400000000, zac@example.com"
    assert pairs["Kian Seen"] == "Kian Seen Goh, kian@example.com"  # gaps + stray space handled
    assert pairs["Paul"] == "Paul Baquiran"  # name-only rows still record the surname
    assert "RBGA" not in pairs  # nothing to record for the club row


def test_import_owners_strips_utf8_bom():
    # The stdin path (piping the CSV over SSH) sees the raw BOM the export carries.
    from rbga.db.import_owners import parse_rows

    lines = ("﻿" + _OWNER_CSV).splitlines(keepends=True)
    assert dict(parse_rows(lines))["Zac"] == "Zac Clarkson, 0400000000, zac@example.com"


def test_import_owners_upserts_by_name():
    from rbga.db.import_owners import upsert_owners

    bot_bg.set_owner_contact("Zac", "old-handle")
    created, updated = upsert_owners([("Zac", "Zac Clarkson, 0400000000"), ("Paul", "Paul Baquiran")])
    assert (created, updated) == (1, 1)
    assert bot_bg.get_owner_contact("Zac") == "Zac Clarkson, 0400000000"
    assert bot_bg.get_owner_contact("Paul") == "Paul Baquiran"
    # Idempotent: re-running updates in place, never duplicates.
    created, updated = upsert_owners([("Paul", "Paul Baquiran")])
    assert (created, updated) == (0, 1)


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


# --- BGG refresh on link edit ------------------------------------------------
_BGG_DATA = {
    "title": "Catan",
    "publisher": "KOSMOS",
    "min_players": 3,
    "max_players": 4,
    "image": "https://cf.geekdo-images.com/catan.jpg",
    "thumbnail": "https://cf.geekdo-images.com/catan__thumb.jpg",
    "tags": ["Economic", "Negotiation"],
}


def test_merge_bgg_refresh_fills_autofilled_fields():
    changes = {"bgg_link": "https://boardgamegeek.com/boardgame/13/catan"}
    merged = bot_bg.merge_bgg_refresh(changes, _BGG_DATA)
    assert merged is changes  # in-place, returned for convenience
    assert merged["publisher"] == "KOSMOS"
    assert merged["min_players"] == 3 and merged["max_players"] == 4
    assert merged["image"] == "https://cf.geekdo-images.com/catan.jpg"
    assert merged["thumbnail"] == "https://cf.geekdo-images.com/catan__thumb.jpg"
    assert merged["tags"] == ["Economic", "Negotiation"]


def test_merge_bgg_refresh_explicit_args_win():
    changes = {"bgg_link": "x", "publisher": "Hand Entered", "tags": ["Custom"]}
    merged = bot_bg.merge_bgg_refresh(changes, _BGG_DATA)
    assert merged["publisher"] == "Hand Entered"
    assert merged["tags"] == ["Custom"]
    assert merged["min_players"] == 3  # non-explicit fields still refresh


def test_merge_bgg_refresh_skips_fields_bgg_lacks():
    # BGG returning nothing for a field must not null the stored value.
    merged = bot_bg.merge_bgg_refresh({"bgg_link": "x"}, {"publisher": None, "tags": None})
    assert merged == {"bgg_link": "x"}


def test_merge_bgg_refresh_never_injects_title():
    merged = bot_bg.merge_bgg_refresh({"bgg_link": "x"}, _BGG_DATA)
    assert "title" not in merged  # hand-customised titles must survive a link edit


class _FakeInteraction:
    """Just enough of discord.Interaction for the command callbacks."""

    class _Response:
        def __init__(self) -> None:
            self.edited: list[dict] = []

        async def defer(self, ephemeral: bool = False) -> None:
            pass

        async def edit_message(self, **kwargs) -> None:
            self.edited.append(kwargs)

    class _Followup:
        def __init__(self) -> None:
            self.messages: list[str] = []
            self.views: list = []

        async def send(self, content=None, view=None, **kwargs) -> None:
            self.messages.append(content)
            self.views.append(view)

    def __init__(self) -> None:
        self.response = self._Response()
        self.followup = self._Followup()


def test_edit_with_new_link_refreshes_stale_bgg_fields(monkeypatch):
    with SessionLocal() as db:
        g = BoardGame(
            title="Catan (RBGA copy)",
            bgg_link="https://boardgamegeek.com/boardgame/999/wrong-game",
            publisher="Stale Publisher",
            image="https://cf.geekdo-images.com/wrong.jpg",
        )
        db.add(g)
        db.commit()
        gid = g.id

    async def fake_fetch(bgg_id):
        assert bgg_id == 13
        return dict(_BGG_DATA)

    monkeypatch.setattr(bot_bg, "fetch_game", fake_fetch)
    ix = _FakeInteraction()
    asyncio.run(
        bot_bg.game_edit.callback(
            ix, gid, bgg_link="https://boardgamegeek.com/boardgame/13/catan"
        )
    )

    assert "Updated" in ix.followup.messages[0]
    with SessionLocal() as db:
        g = db.get(BoardGame, gid)
        assert g.bgg_link == "https://boardgamegeek.com/boardgame/13/catan"
        assert g.publisher == "KOSMOS"  # stale value replaced
        assert g.image == "https://cf.geekdo-images.com/catan.jpg"
        assert g.min_players == 3 and g.max_players == 4
        assert g.title == "Catan (RBGA copy)"  # hand-customised title kept


# --- donor-contact upkeep prompts -------------------------------------------
def _insert(title="Catan", owner=None) -> int:
    with SessionLocal() as db:
        g = BoardGame(title=title, owner=owner)
        db.add(g)
        db.commit()
        return g.id


def test_remove_last_game_offers_contact_cleanup():
    bot_bg.set_owner_contact("Quan", "quan@rmit.edu.au")
    gid = _insert(owner="Quan")

    ix = _FakeInteraction()
    asyncio.run(bot_bg.game_remove.callback(ix, gid))

    assert "Deleted" in ix.followup.messages[0]
    assert "no longer owns any games" in ix.followup.messages[1]
    view = ix.followup.views[1]
    assert isinstance(view, bot_bg.RemoveOwnerContactView)

    # Pressing "Remove contact" actually drops the row.
    press = _FakeInteraction()
    asyncio.run(view.remove_btn.callback(press))
    assert bot_bg.get_owner_contact("Quan") is None
    assert "Removed the saved contact" in press.response.edited[0]["content"]


def test_remove_keeps_contact_when_owner_still_has_games():
    bot_bg.set_owner_contact("Quan", "quan@rmit.edu.au")
    _insert(title="Catan", owner="Quan")
    gid = _insert(title="Azul", owner="Quan")

    ix = _FakeInteraction()
    asyncio.run(bot_bg.game_remove.callback(ix, gid))

    assert len(ix.followup.messages) == 1  # just "Deleted", no cleanup prompt
    assert bot_bg.get_owner_contact("Quan") == "quan@rmit.edu.au"


def test_add_first_time_owner_prompts_for_contact():
    ix = _FakeInteraction()
    asyncio.run(bot_bg.game_add.callback(ix, title="Wingspan", owner="Newbie"))

    assert "Added" in ix.followup.messages[0]
    assert "new owner with no saved contact" in ix.followup.messages[1]
    assert isinstance(ix.followup.views[1], bot_bg.AddOwnerContactView)


def test_add_for_contactless_bulk_owner_stays_quiet():
    # RBGA has no contact row but plenty of games; every add must not nag.
    _insert(title="Catan", owner="RBGA")

    ix = _FakeInteraction()
    asyncio.run(bot_bg.game_add.callback(ix, title="Azul", owner="RBGA"))

    assert len(ix.followup.messages) == 1


def test_edit_reassigning_owner_offers_cleanup_for_old_owner():
    bot_bg.set_owner_contact("Zac", "zac@example.com")
    gid = _insert(title="Catan", owner="Zac")
    _insert(title="Azul", owner="RBGA")

    ix = _FakeInteraction()
    asyncio.run(bot_bg.game_edit.callback(ix, gid, owner="RBGA"))

    assert "Updated" in ix.followup.messages[0]
    assert "Zac** no longer owns any games" in ix.followup.messages[1]
    assert isinstance(ix.followup.views[1], bot_bg.RemoveOwnerContactView)


def test_edit_with_bad_link_changes_nothing(monkeypatch):
    with SessionLocal() as db:
        g = BoardGame(title="Catan", publisher="KOSMOS")
        db.add(g)
        db.commit()
        gid = g.id

    ix = _FakeInteraction()
    asyncio.run(bot_bg.game_edit.callback(ix, gid, bgg_link="https://example.com/nope"))

    assert "Nothing was changed" in ix.followup.messages[0]
    with SessionLocal() as db:
        assert db.get(BoardGame, gid).bgg_link is None


# --- location: canonicalization, autocomplete, filter, bulk edit ------------
def _insert_loc(title: str, location: str | None) -> int:
    with SessionLocal() as db:
        g = BoardGame(title=title, location=location)
        db.add(g)
        db.commit()
        return g.id


def test_canonical_location_reuses_existing_spelling():
    _insert_loc("Catan", "City")
    assert bot_bg.canonical_location("city") == "City"  # case-folded match
    assert bot_bg.canonical_location("  CITY  ") == "City"  # trimmed too
    assert bot_bg.canonical_location("Bundoora") == "Bundoora"  # novel, kept as typed
    assert bot_bg.canonical_location("") is None
    assert bot_bg.canonical_location("   ") is None
    assert bot_bg.canonical_location(None) is None


def test_location_autocomplete_distinct_deduped_and_filtered():
    _insert_loc("Catan", "City")
    _insert_loc("Azul", "city")  # a lingering case-dupe
    _insert_loc("Wingspan", "Bundoora")
    _insert_loc("Uno", None)  # must not surface a blank choice

    ix = _FakeInteraction()
    names = [c.name for c in asyncio.run(bot_bg.location_autocomplete(ix, ""))]
    # 'City'/'city' collapse to a single suggestion; None is dropped.
    assert sorted(n.lower() for n in names) == ["bundoora", "city"]

    filtered = [c.value for c in asyncio.run(bot_bg.location_autocomplete(ix, "bun"))]
    assert filtered == ["Bundoora"]


def test_query_games_location_filter_is_case_insensitive():
    _insert_loc("Catan", "City")
    _insert_loc("Azul", "City")
    _insert_loc("Wingspan", "Bundoora")

    titles = sorted(g.title for g in bot_bg._query_games(None, None, None, None, "city"))
    assert titles == ["Azul", "Catan"]
    assert bot_bg._query_games(None, None, None, None, "nowhere") == []


def test_game_add_canonicalises_location():
    _insert_loc("Catan", "City")
    ix = _FakeInteraction()
    asyncio.run(bot_bg.game_add.callback(ix, title="Azul", location="city"))

    with SessionLocal() as db:
        azul = db.scalars(select(BoardGame).filter_by(title="Azul")).one()
        assert azul.location == "City"  # stored under the existing spelling


def test_game_edit_canonicalises_location():
    _insert_loc("Catan", "City")
    gid = _insert_loc("Azul", None)

    ix = _FakeInteraction()
    asyncio.run(bot_bg.game_edit.callback(ix, gid, location="city"))

    with SessionLocal() as db:
        assert db.get(BoardGame, gid).location == "City"


def test_bulk_edit_moves_matched_games_on_confirm():
    for t in ("Catan", "Azul", "Wingspan"):
        _insert_loc(t, "City")
    _insert_loc("Uno", "Bundoora")  # already there: must be left alone

    ix = _FakeInteraction()
    asyncio.run(
        bot_bg.game_bulk_edit.callback(ix, location="City", set_location="Bundoora")
    )
    # A confirmation view is offered, not an immediate write.
    assert "3 game(s)" in ix.followup.messages[0]
    view = ix.followup.views[0]
    assert isinstance(view, bot_bg.BulkEditConfirmView)
    with SessionLocal() as db:  # nothing written yet
        assert db.scalar(
            select(func.count()).select_from(BoardGame).where(BoardGame.location == "Bundoora")
        ) == 1

    press = _FakeInteraction()
    asyncio.run(view.confirm_btn.callback(press))
    assert "Updated **3 game(s)**" in press.response.edited[0]["content"]
    with SessionLocal() as db:
        assert db.scalar(
            select(func.count()).select_from(BoardGame).where(BoardGame.location == "Bundoora")
        ) == 4
        assert db.scalar(
            select(func.count()).select_from(BoardGame).where(BoardGame.location == "City")
        ) == 0


def test_bulk_edit_cancel_writes_nothing():
    _insert_loc("Catan", "City")
    ix = _FakeInteraction()
    asyncio.run(
        bot_bg.game_bulk_edit.callback(ix, location="City", set_location="Bundoora")
    )
    view = ix.followup.views[0]
    press = _FakeInteraction()
    asyncio.run(view.cancel_btn.callback(press))
    assert press.response.edited[0]["content"] == "Nothing changed."
    with SessionLocal() as db:
        assert db.scalars(select(BoardGame).filter_by(title="Catan")).one().location == "City"


def test_bulk_edit_requires_a_setter():
    _insert_loc("Catan", "City")
    ix = _FakeInteraction()
    asyncio.run(bot_bg.game_bulk_edit.callback(ix, location="City"))
    assert "Tell me what to change" in ix.followup.messages[0]
    assert ix.followup.views[0] is None  # no confirm view offered


def test_bulk_edit_reports_no_match():
    _insert_loc("Catan", "Bundoora")
    ix = _FakeInteraction()
    asyncio.run(
        bot_bg.game_bulk_edit.callback(ix, location="Nowhere", set_location="City")
    )
    assert "No games match that." in ix.followup.messages[0]
    assert ix.followup.views[0] is None


# --- stocktake: per-campus filter + set condition on Seen -------------------
def _insert_cond(title: str, condition: str | None) -> int:
    with SessionLocal() as db:
        g = BoardGame(title=title, condition=condition)
        db.add(g)
        db.commit()
        return g.id


def test_stocktake_location_filter_scopes_to_campus():
    city1 = _insert_loc("Catan", "City")
    city2 = _insert_loc("Azul", "City")
    _insert_loc("Wingspan", "Bundoora")
    _insert_loc("Uno", None)  # un-located: excluded from a campus walk

    ix = _FakeInteraction()
    asyncio.run(bot_bg.game_stocktake.callback(ix, location="city"))  # case-insensitive
    view = ix.followup.views[0]
    assert isinstance(view, bot_bg.StocktakeView)
    assert sorted(view.game_ids) == sorted([city1, city2])


def test_stocktake_condition_dropdown_sets_condition_and_marks_seen():
    gid = _insert_cond("Catan", "Like New")  # imported optimistically
    view = bot_bg.StocktakeView([gid])
    # the dropdown offers exactly the four Condition values
    assert [o.value for o in view.condition_select.options] == list(get_args(bot_bg.Condition))

    press = _FakeInteraction()
    asyncio.run(view._mark(press, missing=False, condition="Damaged"))

    with SessionLocal() as db:
        g = db.get(BoardGame, gid)
        assert g.condition == "Damaged"  # corrected
        assert g.missing is False
        assert g.last_seen_at is not None  # counts as seen
    assert view.recondition == 1
    assert view.seen == 1


def test_stocktake_seen_button_leaves_condition_untouched():
    gid = _insert_cond("Azul", "Like New")
    view = bot_bg.StocktakeView([gid])

    press = _FakeInteraction()
    asyncio.run(view._mark(press, missing=False))  # plain Seen ✔, no condition

    with SessionLocal() as db:
        assert db.get(BoardGame, gid).condition == "Like New"
    assert view.recondition == 0
    assert view.seen == 1
