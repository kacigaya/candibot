"""Suivi des offres en SQLite : une ligne par offre HelloWork, jamais traitée deux fois."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from candibot.hellowork import Offer

# Cycle de vie d'une offre :
#   new -> rejected            note trop basse
#   new -> external            candidature seulement sur le site du recruteur
#   new -> drafted             lettre prête, en attente d'envoi
#   drafted -> rejected        lettre refusée avec `candibot review`
#   drafted -> applied         candidature confirmée par HelloWork
#   drafted -> needs_review    étape imprévue ou erreur après un clic d'envoi : capture à vérifier
#   drafted -> needs_review    posé avant chaque envoi : un arrêt brutal ne cause pas de doublon
#   drafted -> failed          erreur technique avant tout clic d'envoi : rien n'est parti
#   failed, needs_review -> drafted (ou new sans lettre)   `candibot retry`
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

# Issue de chaque passage, pour l'alerte après plusieurs échecs d'affilée.
RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    started TEXT PRIMARY KEY,
    problem TEXT
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
        self.db.execute(RUNS_SCHEMA)

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

    def setLetter(self, offerId: str, letter: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE offers SET letter = ?, updated_at = ? WHERE id = ?", (letter, _now(), offerId)
            )

    def setStatus(self, offerId: str, status: str, detail: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(status)
        with self.db:
            self.db.execute(
                "UPDATE offers SET status = ?, detail = ?, updated_at = ? WHERE id = ?",
                (status, detail, _now(), offerId),
            )

    def retry(self, offerId: str) -> str | None:
        """Remet une offre `failed` ou `needs_review` dans la file d'envoi.

        Renvoie le nouveau statut (`drafted`, ou `new` sans lettre), ou None si
        l'offre n'existe pas ou n'a pas un statut relançable.
        """
        row = self.db.execute("SELECT status, letter FROM offers WHERE id = ?", (offerId,)).fetchone()
        if row is None or row["status"] not in ("failed", "needs_review"):
            return None
        status = "drafted" if row["letter"] else "new"
        self.setStatus(offerId, status, "relancée à la main")
        return status

    def since(self, iso: str) -> list[sqlite3.Row]:
        """Offres modifiées depuis `iso`, pour le récapitulatif d'un passage."""
        return self.db.execute(
            "SELECT * FROM offers WHERE updated_at >= ? ORDER BY status, score DESC", (iso,)
        ).fetchall()

    def recordRun(self, started: str, problem: str | None) -> None:
        """Enregistre l'issue d'un passage : `problem` vaut None s'il s'est bien passé."""
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO runs (started, problem) VALUES (?, ?)", (started, problem)
            )

    def failedStreak(self) -> int:
        """Nombre de passages en échec depuis le dernier passage réussi."""
        streak = 0
        for (problem,) in self.db.execute("SELECT problem FROM runs ORDER BY started DESC"):
            if problem is None:
                break
            streak += 1
        return streak

    def offers(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM offers").fetchall()

    def runs(self, limit: int) -> list[sqlite3.Row]:
        """Derniers passages, du plus récent au plus ancien."""
        return self.db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,)).fetchall()

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) FROM offers GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}
