"""Key-tracker endpoints — the REST version of Owen's original CLI commands."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_api_token
from ...db.database import get_session
from ...db.models import Key

router = APIRouter(prefix="/keys", tags=["keys"])


class KeyIn(BaseModel):
    colour: str
    campus: str


class TakeIn(BaseModel):
    holder: str


class KeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    colour: str
    campus: str
    holder: str | None
    prev_holder: str | None
    transfer_time: datetime | None


@router.get("", response_model=list[KeyOut])
def list_keys(db: Session = Depends(get_session)):
    return db.scalars(select(Key)).all()


@router.post("", response_model=KeyOut, status_code=201, dependencies=[Depends(require_api_token)])
def add_key(data: KeyIn, db: Session = Depends(get_session)):
    if db.scalar(select(Key).where(Key.colour == data.colour)):
        raise HTTPException(409, f"A {data.colour} key already exists")
    key = Key(colour=data.colour, campus=data.campus)
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


@router.get("/{colour}", response_model=KeyOut)
def who_has(colour: str, db: Session = Depends(get_session)):
    key = db.scalar(select(Key).where(Key.colour == colour))
    if not key:
        raise HTTPException(404, f"There is no {colour} key")
    return key


@router.post("/{colour}/take", response_model=KeyOut, dependencies=[Depends(require_api_token)])
def take_key(colour: str, data: TakeIn, db: Session = Depends(get_session)):
    key = db.scalar(select(Key).where(Key.colour == colour))
    if not key:
        raise HTTPException(404, f"There is no {colour} key")
    key.prev_holder = key.holder
    key.holder = data.holder
    key.transfer_time = datetime.utcnow()
    db.commit()
    db.refresh(key)
    return key


@router.delete("/{colour}", status_code=204, dependencies=[Depends(require_api_token)])
def remove_key(colour: str, db: Session = Depends(get_session)):
    key = db.scalar(select(Key).where(Key.colour == colour))
    if not key:
        raise HTTPException(404, f"There is no {colour} key")
    db.delete(key)
    db.commit()
