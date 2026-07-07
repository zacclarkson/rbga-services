"""Fill in board-game data from BGG for every game that has a BGG link.

    python -m rbga.db.enrich_boardgames [--dry-run] [--delay SECONDS]

For each game whose `bgg_link` yields a BGG id, fetches the BGG record and
fills in ONLY what's missing or unusable:

  * image     — set when absent or not a URL (CSV imports stored bare
                SharePoint filenames the web page can't render)
  * tags      — set when empty (BGG categories)
  * publisher — set when empty
  * min/max players — set when empty

Never overwrites data someone entered by hand. Needs BGG_API_TOKEN, so run it
in the bot container on the box:

    docker compose run --rm bot python -m rbga.db.enrich_boardgames

Prints one line per change and a summary, then lists any games still lacking
an image URL (no BGG link, or BGG had no image) so they can be fixed by hand.
"""
import argparse
import asyncio

from sqlalchemy import select

from ..bgg import BGGNotConfigured, extract_bgg_id, fetch_game
from .database import SessionLocal
from .models import BoardGame


def needs_image(image: str | None) -> bool:
    """True when there is no usable image URL (empty, or a bare filename)."""
    return not (image and image.startswith(("http://", "https://")))


async def enrich(dry_run: bool = False, delay: float = 1.0) -> int:
    """Returns the number of games updated."""
    with SessionLocal() as db:
        games = list(db.scalars(select(BoardGame).order_by(BoardGame.id)))
        updated = failed = nolink = 0

        for g in games:
            bgg_id = extract_bgg_id(g.bgg_link or "")
            if bgg_id is None:
                nolink += 1
                continue
            try:
                data = await fetch_game(bgg_id)
            except BGGNotConfigured:
                raise SystemExit(
                    "BGG_API_TOKEN is not set. Run this in the bot container."
                )
            except Exception as e:
                print(f"#{g.id} {g.title}: fetch failed ({e!r})")
                data = None
            if not data:
                failed += 1
                continue

            changes = []
            if needs_image(g.image) and data.get("image"):
                g.image = data["image"]
                changes.append("image")
            if not g.tags and data.get("tags"):
                g.tags = data["tags"]
                changes.append("tags")
            if not g.publisher and data.get("publisher"):
                g.publisher = data["publisher"]
                changes.append("publisher")
            if g.min_players is None and data.get("min_players"):
                g.min_players = data["min_players"]
                changes.append("min_players")
            if g.max_players is None and data.get("max_players"):
                g.max_players = data["max_players"]
                changes.append("max_players")
            if changes:
                updated += 1
                print(f"#{g.id} {g.title}: {', '.join(changes)}")
            if delay:
                await asyncio.sleep(delay)  # be polite to BGG

        if dry_run:
            db.rollback()
            print("\n(dry run: nothing saved)")
        else:
            db.commit()

        print(f"\n{updated} updated, {failed} fetch failed, {nolink} without a BGG link.")
        missing = [g for g in games if needs_image(g.image)]
        if missing:
            print(f"{len(missing)} still lack an image URL:")
            for g in missing:
                print(f"  #{g.id} {g.title}")
        return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fill missing board-game data from BGG.")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without saving.")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between BGG calls.")
    args = parser.parse_args(argv)
    asyncio.run(enrich(dry_run=args.dry_run, delay=args.delay))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
