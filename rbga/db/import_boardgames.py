"""Import the club's board-game inventory from the SharePoint CSV export.

    python -m rbga.db.import_boardgames <csv-path> [--replace]

The export has one wrinkle: line 1 is a SharePoint `ListSchema=…` metadata blob,
not CSV. We drop it and let the real header (line 2:
`Name,Image,BGG Link,Owner,Condition`) drive a csv.DictReader.

By default this refuses to run against a non-empty `board_games` table (so an
accidental re-run can't double the inventory). Pass --replace to wipe and reload
— note the CSV has *intentional* duplicate rows (e.g. Polyhedral Dice Set ×4),
so we never dedupe by title.
"""
import argparse
import csv
import sys

from sqlalchemy import delete, func, select

from .database import SessionLocal, engine
from .models import BoardGame


def _clean(value: str | None) -> str | None:
    """Trim whitespace; treat empty strings as NULL."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def parse_rows(path: str) -> list[BoardGame]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        lines = fh.readlines()
    # Drop the leading SharePoint schema blob if present.
    if lines and lines[0].startswith("ListSchema="):
        lines = lines[1:]
    reader = csv.DictReader(lines)
    games = []
    for row in reader:
        title = _clean(row.get("Name"))
        if not title:
            continue  # skip blank rows
        games.append(
            BoardGame(
                title=title,
                image=_clean(row.get("Image")),
                bgg_link=_clean(row.get("BGG Link")),
                owner=_clean(row.get("Owner")),
                condition=_clean(row.get("Condition")),
            )
        )
    return games


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import board games from the SharePoint CSV export.")
    parser.add_argument("csv_path", help="Path to the exported CSV")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete existing board_games rows before importing (required if the table is non-empty).",
    )
    args = parser.parse_args(argv)

    # Safe standalone: create just our table if the API hasn't yet. (Only this
    # table — not Base.metadata — so we never touch the complaints schema.)
    BoardGame.__table__.create(bind=engine, checkfirst=True)

    games = parse_rows(args.csv_path)
    if not games:
        print("No games found in the CSV — nothing to import.")
        return 1

    with SessionLocal() as db:
        existing = db.scalar(select(func.count()).select_from(BoardGame))
        if existing and not args.replace:
            print(
                f"board_games already has {existing} rows. Re-run with --replace to "
                "wipe and reload."
            )
            return 1
        if args.replace and existing:
            db.execute(delete(BoardGame))
        db.add_all(games)
        db.commit()

    print(f"Imported {len(games)} games.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
