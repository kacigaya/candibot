"""Recherche et lecture des offres HelloWork via un navigateur Patchright (WebSkrap).

Sélecteurs relevés sur le site le 22/10/2026. Si HelloWork change son balisage,
les fonctions `parse*` sont les seuls endroits à corriger ici. Le formulaire de
candidature est dans `application.py`.
"""

import html
import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode

from patchright.async_api import Page
from patchright.async_api import TimeoutError as PlaywrightTimeout

BASE = "https://www.hellowork.com"
# Types de contrat acceptés dans config.toml -> valeur du filtre `c` de la recherche HelloWork.
CONTRACTS = {
    "CDI": "CDI",
    "CDD": "CDD",
    "Intérim": "Travail_temp",
    "Stage": "Stage",
    "Alternance": "Alternance",
    "Indépendant": "Independant",
    "Freelance": "Freelance",
    "Franchise": "Franchise",
    "Associé": "Associe",
    "Fonctionnaire": "Fonctionnaire",
    "Stage de lycée": "Stage_de_lycee",
}
# Bouton des offres dont la candidature se fait hors de HelloWork.
EXTERNAL = re.compile(r"Postuler sur le site du (recruteur|partenaire)")


@dataclass(frozen=True)
class Offer:
    id: str
    url: str
    title: str
    company: str
    location: str
    description: str
    onSite: bool  # candidature possible sur HelloWork, sans passer par le site du recruteur


def searchUrl(query: str, location: str, contracts: tuple[str, ...], page: int) -> str:
    """URL d'une page de résultats. Plusieurs contrats donnent un paramètre `c` répété (OU logique)."""
    params = [("k", query), ("l", location), *(("c", CONTRACTS[c]) for c in contracts), ("p", page)]
    return f"{BASE}/fr-fr/emploi/recherche.html?{urlencode(params)}"


def offerUrl(offerId: str) -> str:
    return f"{BASE}/fr-fr/emplois/{offerId}.html"


def parseSearch(pageHtml: str) -> list[str]:
    """Identifiants des offres d'une page de résultats, dans l'ordre, sans doublon."""
    ids = re.findall(r'data-id-storage-item-id="(\d+)"', pageHtml)
    return list(dict.fromkeys(ids))


def _text(fragment: str) -> str:
    """HTML de description -> texte brut lisible."""
    fragment = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<li[^>]*>", "- ", fragment, flags=re.I)
    fragment = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    lines = "\n".join(line.strip() for line in fragment.splitlines())
    return re.sub(r"\n{3,}", "\n\n", lines).strip()


def _location(posting: dict) -> str:
    places = posting.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    labels = []
    for place in places:
        address = place.get("address") or {}
        label = " ".join(
            str(part) for part in (address.get("addressLocality"), address.get("postalCode")) if part
        )
        if label:
            labels.append(label)
    return ", ".join(labels)


def _parseAnonymous(offerId: str, pageHtml: str) -> Offer | None:
    """Les offres d'entreprises anonymes n'ont pas de JSON-LD : on lit le HTML de la fiche."""
    title = re.search(r'data-cy="jobTitle"[^>]*>(.*?)<', pageHtml, re.S)
    start = re.search(r"Les missions du poste|Détail du poste", pageHtml)
    if not title or not start:
        return None
    # Le <title> se termine par « Ville (75) | Hellowork » : seul le département est fiable.
    department = re.search(r"\((\d{2,3}|2[AB])\)\s*\|\s*Hellowork</title>", pageHtml)
    return Offer(
        id=offerId,
        url=offerUrl(offerId),
        title=html.unescape(title.group(1)).strip(),
        company="Entreprise anonyme",
        location=f"département {department.group(1)}" if department else "",
        description=_text(pageHtml[start.start() : start.start() + 15_000])[:8000],
        onSite=not EXTERNAL.search(pageHtml),
    )


def parseOffer(offerId: str, pageHtml: str) -> Offer | None:
    """Lit le bloc JSON-LD JobPosting de la fiche, ou le HTML pour une offre anonyme.

    None si la fiche n'a ni l'un ni l'autre (offre expirée ou balisage changé).
    """
    for block in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', pageHtml, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "JobPosting":
            continue
        company = (data.get("hiringOrganization") or {}).get("name") or ""
        return Offer(
            id=offerId,
            url=offerUrl(offerId),
            title=html.unescape(data.get("title", "")).strip(),
            company=html.unescape(company).strip(),
            location=_location(data),
            description=_text(data.get("description", "")),
            onSite=not EXTERNAL.search(pageHtml),
        )
    return _parseAnonymous(offerId, pageHtml)


async def dismissCookies(page: Page) -> None:
    """HelloWork utilise sa propre bannière, que le refus automatique de WebSkrap ne connaît pas."""
    try:
        await page.click(".hw-cc-notice-modal__btn-continue-without-accepting", timeout=5_000)
    except PlaywrightTimeout:
        pass  # déjà refusée dans ce profil, ou pas affichée


async def search(page: Page, query: str, location: str, contracts: tuple[str, ...], pages: int) -> list[str]:
    ids: list[str] = []
    for n in range(1, pages + 1):
        await page.goto(searchUrl(query, location, contracts, n), wait_until="domcontentloaded")
        found = parseSearch(await page.content())
        if not found:
            break
        ids.extend(found)
    return list(dict.fromkeys(ids))


async def fetchOffer(page: Page, offerId: str) -> Offer | None:
    await page.goto(offerUrl(offerId), wait_until="domcontentloaded")
    return parseOffer(offerId, await page.content())
