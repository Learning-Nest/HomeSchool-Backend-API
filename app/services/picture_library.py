"""The picture library: AI pictures that educators chose to use, kept so others can find and reuse them.

Two parts:

* ``add_from_generation`` is called when an AI picture is uploaded into an activity. The library keeps its own copy of
  the bytes, so it does not depend on that activity.
* ``search`` finds pictures by what they were described as. Each search METHOD is one small class with the same
  ``find`` method, registered in ``SEARCH_METHODS`` under the name callers send as ``mode``. Today there is only
  ``keyword``. To add a search by meaning later: write a class whose ``find`` embeds the query and ranks pictures by
  distance (its vectors live in its own table, filled by ``add_from_generation`` or a backfill job), register it under
  ``"similar"``, and add ``"similar"`` to ``LibrarySearchMode`` in schemas.py. The endpoint, the response shape and the
  web screens stay as they are.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import AiGeneration, LibraryPicture
from app.services.storage import get_storage

LIBRARY_PREFIX = "library"
MAX_QUERY_TOKENS = 8

# Words that say nothing about what a picture shows.
_STOP_WORDS = frozenset(
    "a an and are as at be by for from has have in into is it its of on or that the then there this to was with without"
    " picture image photo drawing illustration showing shows show".split()
)


def library_blob_path(sha256: str) -> str:
    return f"{LIBRARY_PREFIX}/{sha256}.webp"


@dataclass(frozen=True)
class Found:
    picture: LibraryPicture
    score: float | None  # 0-1, or None when the search did not rank (browsing)


class SearchMethod(Protocol):
    def find(self, db: Session, query: str, *, style: str | None, limit: int, offset: int) -> list[Found]: ...


def query_tokens(query: str) -> list[str]:
    """The words of a query that are worth matching: lower case, no filler words, a plural 's' removed."""
    seen: dict[str, None] = {}
    for word in re.findall(r"\w+", query.lower()):
        if len(word) < 3 or word in _STOP_WORDS:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        seen.setdefault(word, None)
    return list(seen)[:MAX_QUERY_TOKENS]


def _like(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class KeywordSearch:
    """Pictures whose description contains most of the query's words, best match first.

    One or two words must all appear; for longer queries a bit over half of them must. Pictures that are used more
    often, and newer ones, come first among equal matches. An empty query just lists the newest and most used."""

    def find(self, db: Session, query: str, *, style: str | None, limit: int, offset: int) -> list[Found]:
        stmt = select(LibraryPicture)
        if style:
            stmt = stmt.where(LibraryPicture.style == style)
        if not query.strip():
            rows = db.scalars(
                stmt.order_by(LibraryPicture.uses.desc(), LibraryPicture.created_at.desc(), LibraryPicture.id)
                .limit(limit)
                .offset(offset)
            )
            return [Found(p, None) for p in rows]

        tokens = query_tokens(query) or [query.strip().lower()]
        hits = [case((LibraryPicture.description.ilike(_like(t), escape="\\"), 1), else_=0) for t in tokens]
        matched = sum(hits[1:], hits[0])
        needed = len(tokens) if len(tokens) <= 2 else math.ceil(len(tokens) * 0.6)
        rows = db.execute(
            stmt.add_columns(matched.label("matched"))
            .where(matched >= needed)
            .order_by(matched.desc(), LibraryPicture.uses.desc(), LibraryPicture.created_at.desc(), LibraryPicture.id)
            .limit(limit)
            .offset(offset)
        ).all()
        return [Found(p, round(m / len(tokens), 3)) for p, m in rows]


SEARCH_METHODS: dict[str, SearchMethod] = {"keyword": KeywordSearch()}


def search(db: Session, mode: str, query: str, *, style: str | None, limit: int, offset: int) -> list[Found]:
    return SEARCH_METHODS[mode].find(db, query.strip(), style=style, limit=limit, offset=offset)


def add_from_generation(
    db: Session, gen: AiGeneration, *, data: bytes, sha256: str, width: int, height: int, user_id: uuid.UUID
) -> LibraryPicture | None:
    """Keeps a copy of a picture that was just uploaded from `gen`. Does nothing if the library already has it."""
    if db.scalar(select(func.count()).select_from(LibraryPicture).where(LibraryPicture.sha256 == sha256)):
        return None
    path = library_blob_path(sha256)
    get_storage().put(path, data, "image/webp")
    picture = LibraryPicture(
        description=gen.description,
        style=gen.style,
        size=gen.size,
        quality=gen.quality,
        blob_path=path,
        content_type="image/webp",
        bytes=len(data),
        width=width,
        height=height,
        sha256=sha256,
        created_by=user_id,
        ai_generation_id=gen.id,
        uses=1,
    )
    db.add(picture)
    db.flush()
    return picture


__all__ = ["SEARCH_METHODS", "Found", "add_from_generation", "library_blob_path", "query_tokens", "search"]
