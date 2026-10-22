import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from candibot.questions import Answers


@dataclass(frozen=True)
class Candidate:
    first_name: str
    last_name: str
    email: str
    cv_pdf: Path
    cv_text: str
    rhythm: str
    availability: str


@dataclass(frozen=True)
class Search:
    queries: list[str]
    location: str
    pages: int
    min_score: int
    max_scored: int
    max_applications: int
    pause_seconds: tuple[int, int]
    exclude_companies: tuple[str, ...]

    def excluded(self, company: str) -> bool:
        """Vrai si le nom de l'entreprise contient un des noms exclus (sans tenir compte de la casse)."""
        name = company.casefold()
        return any(pattern.casefold() in name for pattern in self.exclude_companies)


@dataclass(frozen=True)
class Mail:
    inbox: str  # boîte AgentMail d'envoi, ex. candibot@agentmail.to
    to: str  # destinataire du récapitulatif

    @property
    def enabled(self) -> bool:
        return bool(self.inbox and self.to)


@dataclass(frozen=True)
class Config:
    answers: Answers
    root: Path
    candidate: Candidate
    search: Search
    models: list[str]  # OpenCode, dans l'ordre d'essai
    mail: Mail

    @property
    def data_dir(self) -> Path:
        return self.root / "data"


def load_env(path: Path) -> None:
    """Charge les lignes `CLE=valeur` d'un fichier .env sans écraser l'environnement existant."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load(path: Path) -> Config:
    path = path.resolve()
    root = path.parent
    load_env(root / ".env")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    c, s, m = raw["candidate"], raw["search"], raw.get("mail", {})

    cv_pdf = root / c["cv_pdf"]
    if not cv_pdf.is_file():
        raise SystemExit(f"CV introuvable : {cv_pdf}")
    if cv_pdf.stat().st_size > 2 * 1024 * 1024:
        raise SystemExit(f"CV trop lourd pour HelloWork (2 Mo max) : {cv_pdf}")
    low, high = s.get("pause_seconds", [60, 180])
    if not 0 <= low <= high:
        raise SystemExit("search.pause_seconds doit valoir [min, max] avec 0 <= min <= max")

    a = raw.get("answers", {})
    return Config(
        answers=Answers(
            civility=a.get("civility", ""),
            availability=a.get("availability", ""),
            salary_monthly=float(a.get("salary_monthly", 0)),
            salary_text=a.get("salary_text", ""),
        ),
        root=root,
        candidate=Candidate(
            first_name=c["first_name"],
            last_name=c["last_name"],
            email=c["email"],
            cv_pdf=cv_pdf,
            cv_text=(root / c["cv_text"]).read_text(encoding="utf-8"),
            rhythm=c.get("rhythm", ""),
            availability=c.get("availability", ""),
        ),
        search=Search(
            queries=list(s["queries"]),
            location=s["location"],
            pages=int(s.get("pages", 2)),
            min_score=int(s.get("min_score", 7)),
            max_scored=int(s.get("max_scored", 40)),
            max_applications=int(s.get("max_applications", 10)),
            pause_seconds=(int(low), int(high)),
            exclude_companies=tuple(p.strip() for p in s.get("exclude_companies", []) if p.strip()),
        ),
        models=list(raw.get("llm", {}).get("models", ["opencode/big-pickle"])),
        mail=Mail(inbox=m.get("inbox", ""), to=m.get("to", "")),
    )
