"""Board-game inventory endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_api_token
from ...db.database import get_session
from ...db.models import BoardGame

router = APIRouter(prefix="/board-games", tags=["board-games"])


class BoardGameIn(BaseModel):
    title: str
    publisher: str | None = None
    min_players: int | None = None
    max_players: int | None = None
    location: str | None = None
    notes: str | None = None
    owner: str | None = None
    condition: str | None = None
    bgg_link: str | None = None
    image: str | None = None
    price: float | None = None


class BoardGameOut(BoardGameIn):
    model_config = ConfigDict(from_attributes=True)

    id: int


@router.get("", response_model=list[BoardGameOut])
def list_games(db: Session = Depends(get_session)):
    return db.scalars(select(BoardGame).order_by(BoardGame.title)).all()


@router.post("", response_model=BoardGameOut, status_code=201, dependencies=[Depends(require_api_token)])
def add_game(data: BoardGameIn, db: Session = Depends(get_session)):
    game = BoardGame(**data.model_dump())
    db.add(game)
    db.commit()
    db.refresh(game)
    return game


@router.get("/{game_id}", response_model=BoardGameOut)
def get_game(game_id: int, db: Session = Depends(get_session)):
    game = db.get(BoardGame, game_id)
    if not game:
        raise HTTPException(404, "No such board game")
    return game


@router.delete("/{game_id}", status_code=204, dependencies=[Depends(require_api_token)])
def delete_game(game_id: int, db: Session = Depends(get_session)):
    game = db.get(BoardGame, game_id)
    if not game:
        raise HTTPException(404, "No such board game")
    db.delete(game)
    db.commit()
