import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from candibot.hellowork import CONTRACTS
from candibot.labonnealternance import ROME
from candibot.questions import Answers

# Sites de recherche acceptés dans `search.sites` -> nom affiché.
SITES = {"hellowork": "HelloWork", "labonnealternance": "La bonne alternance"}


@dataclass(frozen=True)
class Candidate:
    firstName: str
    lastName: str
    email: str
    cvPdf: Path
    cvText: str
    rhythm: str
    availability: str


@dataclass(frozen=True)
class Search:
    queries: list[str]
    location: str
    contracts: tuple[str, ...]  # clés de hellowork.CONTRACTS, ex. ("CDI", "CDD")
    pages: int
    minScore: int
    maxScored: int
    maxApplications: int
    pauseSeconds: tuple[int, int]
    excludeCompanies: tuple[str, ...]
    sites: tuple[str, ...] = ("hellowork",)  # clés de SITES

    def excluded(self, company: str) -> bool:
        """Vrai si le nom de l'entreprise contient un des noms exclus (sans tenir compte de la casse)."""
        name = company.casefold()
        return any(pattern.casefold() in name for pattern in self.excludeCompanies)


@dataclass(frozen=True)
class Lba:
    romes: tuple[str, ...]  # codes ROME des métiers recherchés, ex. ("E1103",)
    departements: tuple[str, ...]  # vide : toute la France


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
    lba: Lba
    models: list[str]  # OpenCode, dans l'ordre d'essai
    mail: Mail

    @property
    def dataDir(self) -> Path:
        return self.root / "data"


def loadEnv(path: Path) -> None:
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
    loadEnv(root / ".env")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    c, s, m = raw["candidate"], raw["search"], raw.get("mail", {})

    cvPdf = root / c["cv_pdf"]
    if not cvPdf.is_file():
        raise SystemExit(f"CV introuvable : {cvPdf}")
    if cvPdf.stat().st_size > 2 * 1024 * 1024:
        raise SystemExit(f"CV trop lourd pour HelloWork (2 Mo max) : {cvPdf}")
    contracts = tuple(s.get("contracts", []))
    unknown = [c for c in contracts if c not in CONTRACTS]
    if not contracts or unknown:
        raise SystemExit(
            f"search.contracts doit lister un ou plusieurs contrats parmi : {', '.join(CONTRACTS)}"
        )
    sites = tuple(s.get("sites", ["hellowork"]))
    if not sites or any(site not in SITES for site in sites):
        raise SystemExit(f"search.sites doit lister un ou plusieurs sites parmi : {', '.join(SITES)}")
    a = raw.get("answers", {})
    lba = raw.get("labonnealternance", {})
    romes = tuple(r.strip().upper() for r in lba.get("romes", []))
    if "labonnealternance" in sites:
        # La bonne alternance ne publie que des offres d'alternance.
        if "Alternance" not in contracts:
            raise SystemExit(
                'search.sites contient "labonnealternance" : ajoutez "Alternance" à search.contracts'
            )
        if not romes or not all(ROME.match(r) for r in romes):
            raise SystemExit('labonnealternance.romes doit lister un ou plusieurs codes ROME, ex. ["E1103"]')
        if not a.get("phone", "").strip():
            raise SystemExit(
                "La bonne alternance exige un téléphone pour candidater : remplissez answers.phone"
            )
    low, high = s.get("pause_seconds", [60, 180])
    if not 0 <= low <= high:
        raise SystemExit("search.pause_seconds doit valoir [min, max] avec 0 <= min <= max")

    return Config(
        answers=Answers(
            civility=a.get("civility", ""),
            availability=a.get("availability", ""),
            salaryMonthly=float(a.get("salary_monthly", 0)),
            salaryText=a.get("salary_text", ""),
            phone=a.get("phone", "").strip(),
        ),
        root=root,
        candidate=Candidate(
            firstName=c["first_name"],
            lastName=c["last_name"],
            email=c["email"],
            cvPdf=cvPdf,
            cvText=(root / c["cv_text"]).read_text(encoding="utf-8"),
            rhythm=c.get("rhythm", ""),
            availability=c.get("availability", ""),
        ),
        search=Search(
            queries=list(s["queries"]),
            location=s["location"],
            contracts=contracts,
            sites=sites,
            pages=int(s.get("pages", 2)),
            minScore=int(s.get("min_score", 7)),
            maxScored=int(s.get("max_scored", 40)),
            maxApplications=int(s.get("max_applications", 10)),
            pauseSeconds=(int(low), int(high)),
            excludeCompanies=tuple(p.strip() for p in s.get("exclude_companies", []) if p.strip()),
        ),
        lba=Lba(romes=romes, departements=tuple(str(d).strip() for d in lba.get("departements", []))),
        models=list(raw.get("llm", {}).get("models", ["opencode/big-pickle"])),
        mail=Mail(inbox=m.get("inbox", ""), to=m.get("to", "")),
    )
