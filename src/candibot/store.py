"""Suivi des offres en SQLite : une ligne par offre HelloWork, jamais traitée deux fois."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from candibot.hellowork import Offer

# Cycle de vie d'une offre :
#   new -> rejected            note trop basse
#   new -> external            candidature seulement sur le site du recruteur
#   new -> drafted             lettre prête, en attente d'envoi
#   drafted -> applied         candidature confirmée par HelloWork
#   drafted -> needs_review    étape imprévue ou erreur après un clic d'envoi : capture à vérifier
#   drafted -> needs_review    posé avant chaque envoi : un arrêt brutal ne cause pas de doublon
#   drafted -> failed          erreur technique avant tout clic d'envoi : rien n'est parti
EXCLUDED_REASON = "organisme de formation exclu (config)"
STATUSES = ("new", "rejected", "external", "drafted", "applied", "needs_review", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL,
    company     TEXT NOT NULL,
    location    TEXT NOT NULL,
    description TEXT NOT NULL,
    on_site     INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new',
    score       INTEGER,
    reason      TEXT,
    letter      TEXT,
    detail      TEXT,
    seen_at     TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def known(self, offerId: str) -> bool:
        return self.db.execute("SELECT 1 FROM offers WHERE id = ?", (offerId,)).fetchone() is not None

    def add(self, offer: Offer) -> None:
        now = _now()
        status = "new" if offer.onSite else "external"
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO offers"
                " (id, url, title, company, location, description, on_site, status, seen_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    offer.id,
                    offer.url,
                    offer.title,
                    offer.company,
                    offer.location,
                    offer.description,
                    int(offer.onSite),
                    status,
                    now,
                    now,
                ),
            )

    def byStatus(self, status: str, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM offers WHERE status = ? ORDER BY score DESC, seen_at"
        params: tuple = (status,)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        return self.db.execute(sql, params).fetchall()

    def scored(self, offerId: str, score: int, reason: str, letter: str, minScore: int) -> None:
        status = "drafted" if score >= minScore else "rejected"
        with self.db:
            self.db.execute(
                "UPDATE offers SET status = ?, score = ?, reason = ?, letter = ?, updated_at = ? WHERE id = ?",
                (status, score, reason, letter, _now(), offerId),
            )

    def setStatus(self, offerId: str, status: str, detail: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(status)
        with self.db:
            self.db.execute(
                "UPDATE offers SET status = ?, detail = ?, updated_at = ? WHERE id = ?",
                (status, detail, _now(), offerId),
            )

    def since(self, iso: str) -> list[sqlite3.Row]:
        """Offres modifiées depuis `iso`, pour le récapitulatif d'un passage."""
        return self.db.execute(
            "SELECT * FROM offers WHERE updated_at >= ? ORDER BY status, score DESC", (iso,)
        ).fetchall()

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) FROM offers GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}
