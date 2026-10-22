"""Notation des offres et rédaction des lettres via les modèles gratuits d'OpenCode.

`opencode run` est un agent qui dispose d'outils (shell, fichiers, web) et que
l'offre gratuite refuse de faire tourner sans eux. Le texte des offres venant du
web, une annonce piégée pourrait lui faire exécuter des commandes : il tourne donc
dans un bac à sable bubblewrap (système en lecture seule, $HOME vide et jetable,
réseau seul ouvert), sans accès au projet, au CV ni aux clés.
"""

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from candibot.config import Candidate, Search

# Tout caractère au-delà du latin étendu, hors ponctuation typographique française.
FOREIGN = re.compile(r"[^\x00-\u024f\u2010-\u2027\u202f\u2030-\u203a\u20ac]")

JSON_CONTRACT = (
    "Réponds uniquement avec un objet JSON, sans texte autour ni bloc de code : "
    '{"score": <entier de 0 à 10>, "reason": "<une phrase>", "letter": "<message au recruteur>"}'
)

SYSTEM = """Tu aides une personne à trouver un emploi. Pour chaque offre, tu rends une note, une raison et un message au recruteur. La recherche (métiers, zone, contrats) est donnée dans <recherche>.

Note de 0 à 10 : adéquation entre l'offre, le CV et la recherche.
- 0 à 2 : l'offre impose de suivre la formation de celui qui la publie. C'est le cas d'une école, d'un CFA ou d'un organisme de formation (ISCOD, Studi, AURLOM, IMC Alternance…) qui recrute pour son propre cursus, même quand le poste est chez une entreprise partenaire (« entreprise partenaire », « intégrez notre formation », « formation offerte », « inscription à notre programme »). La personne ne cherche pas de formation.
- 0 à 2 également : type de contrat absent des contrats recherchés, poste sans rapport avec les métiers recherchés, ou lieu hors de la zone recherchée.
- Une école ou un groupe d'écoles qui recrute pour son PROPRE fonctionnement (sans exiger d'inscription à son cursus) est un employeur normal.
- 3 à 4 : niveau d'études ou d'expérience exigé incompatible avec le CV, ou missions très éloignées du profil.
- 5 à 6 : pertinent mais peu aligné.
- 7 à 10 : missions proches des métiers recherchés et compatibles avec le CV. Plus la correspondance avec l'expérience réelle est forte, plus la note monte.

Raison : une phrase factuelle.

Message au recruteur (champ « letter ») :
- Français, vouvoiement, 120 à 200 mots, texte brut sans titre, sans objet, sans formule « Madame, Monsieur » en double.
- Commence par « Bonjour, » et finit par la signature « Prénom NOM ».
- Si des précisions (rythme d'alternance, disponibilité) sont fournies, mentionne-les toutes, en une phrase, vers la fin.
- Relie deux ou trois missions précises de l'offre à des expériences ou compétences réelles du CV. Cite l'entreprise.
- N'invente rien : aucun chiffre, outil, diplôme ou expérience absents du CV. Pas de crochets ni de texte à compléter.
- Ton direct et naturel. Évite les formules creuses (« passionné(e) », « dynamique », « je suis convaincu(e) que », « valeur ajoutée ») et les tirets cadratins.
- Si la note est inférieure à 5, renvoie une lettre vide."""


@dataclass(frozen=True)
class Evaluation:
    score: int
    reason: str
    letter: str


def buildPrompt(
    candidate: Candidate, search: Search, title: str, company: str, location: str, description: str
) -> str:
    extras = []
    if candidate.rhythm:
        extras.append(f"Rythme de l'alternance : {candidate.rhythm}")
    if candidate.availability:
        extras.append(f"Disponibilité : {candidate.availability}")
    extrasText = "\n".join(extras) or "Aucune (ne pas mentionner rythme ni date de début)."
    return f"""<cv>
{candidate.cvText}
</cv>

<recherche>
Métiers : {", ".join(search.queries)}
Zone : {search.location}
Contrats : {", ".join(search.contracts)}
</recherche>

<precisions>
Signature : {candidate.firstName} {candidate.lastName}
{extrasText}
</precisions>

<offre>
Titre : {title}
Entreprise : {company}
Lieu : {location}

{description[:8000]}
</offre>"""


def parseOutput(stdout: str) -> Evaluation:
    """Lit les événements de `opencode run --format json` et en extrait l'objet JSON rendu."""
    texts, error = [], None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "text":
            texts.append(event["part"]["text"])
        elif event.get("type") == "error":
            error = (event.get("error") or {}).get("data", {}).get("message") or str(event.get("error"))
    if error:
        raise RuntimeError(f"opencode : {error}")
    match = re.search(r"\{.*\}", "".join(texts), re.S)
    if not match:
        raise RuntimeError("opencode n'a pas rendu d'objet JSON")
    out = json.loads(match.group(0))
    score = int(out["score"])
    if not 0 <= score <= 10:
        raise RuntimeError(f"note hors bornes : {score}")
    letter = str(out["letter"]).strip()
    if score >= 5 and not letter:
        raise RuntimeError(f"note {score} sans lettre")
    if stray := FOREIGN.findall(letter):
        # Les modèles gratuits glissent parfois un mot chinois ou cyrillique en pleine phrase.
        raise RuntimeError(f"lettre avec caractères hors alphabet latin : {''.join(stray)[:20]!r}")
    return Evaluation(score=score, reason=str(out["reason"]).strip(), letter=letter)


def sandboxCommand(opencode: Path, home: Path, args: list[str]) -> list[str]:
    """`opencode` lancé par bubblewrap : seul son binaire est visible dans le $HOME."""
    binaryDir = opencode.parent.parent  # ~/.opencode, qui contient bin/opencode
    return [
        "bwrap",
        "--ro-bind", "/", "/",
        "--tmpfs", str(home),
        "--ro-bind", str(binaryDir), str(binaryDir),
        "--tmpfs", "/tmp",
        "--tmpfs", "/var/tmp",
        "--dev", "/dev",
        "--proc", "/proc",
        "--unshare-all",
        "--share-net",  # l'API du modèle ; /run reste visible pour le DNS
        "--die-with-parent",
        "--new-session",
        "--dir", str(home / "work"),
        "--chdir", str(home / "work"),
        "--setenv", "HOME", str(home),
        "--unsetenv", "AGENTMAIL_API_KEY",
        str(opencode),
        *args,
    ]  # fmt: skip


def evaluate(
    candidate: Candidate,
    search: Search,
    models: list[str],
    *,
    title: str,
    company: str,
    location: str,
    description: str,
) -> Evaluation:
    """Note l'offre avec le premier modèle qui répond correctement (les suivants servent de secours)."""
    found = shutil.which("opencode")
    if not found or not shutil.which("bwrap"):
        raise RuntimeError("`opencode` et `bwrap` sont requis")
    opencode = Path(os.path.realpath(found))
    prompt = f"{SYSTEM}\n\n{buildPrompt(candidate, search, title, company, location, description)}\n\n{JSON_CONTRACT}"
    errors = []
    for model in models:
        cmd = sandboxCommand(
            opencode, Path.home(), ["run", "--pure", "-m", model, "--format", "json", prompt]
        )
        try:
            # stdin fermé : sans cela `opencode run` attend une entrée et ne rend jamais la main.
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300, check=False
            )
            return parseOutput(proc.stdout)
        except (RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{model} : {exc}")
    raise RuntimeError(" ; ".join(errors))
