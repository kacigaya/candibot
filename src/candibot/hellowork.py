"""Recherche et candidature sur HelloWork via un navigateur Patchright (WebSkrap).

Sélecteurs relevés sur le site le 24/09/2026. Si HelloWork change son balisage,
les fonctions `parse_*` et `apply` sont les seuls endroits à corriger.
"""

import html
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from patchright.async_api import Locator, Page
from patchright.async_api import TimeoutError as PlaywrightTimeout
from webskrap import WebSkrapSession

from candibot import questions

BASE = "https://www.hellowork.com"
# Bouton des offres dont la candidature se fait hors de HelloWork.
EXTERNAL = re.compile(r"Postuler sur le site du (recruteur|partenaire)")
# Fiche encore en ligne mais offre close : plus de bouton « Postuler ».
EXPIRED = re.compile(r"offre (n'est plus|a expiré|expirée)|plus disponible", re.I)
# Zone de candidature de la fiche. Toute action y est confinée : hors de cette
# zone, HelloWork propose des boutons « postuler en un clic » à d'autres offres.
APPLY = "#postuler"
LETTER_MAX = 2900  # au-delà, HelloWork tronque le message au recruteur
# Étape de validation de l'adresse email : code à 6 chiffres envoyé par HelloWork
# à la première candidature (création du compte).
OTP_STEP = re.compile(r"saisissez le code", re.I)


@dataclass(frozen=True)
class Offer:
    id: str
    url: str
    title: str
    company: str
    location: str
    description: str
    on_site: bool  # candidature possible sur HelloWork, sans passer par le site du recruteur


def search_url(query: str, location: str, page: int) -> str:
    params = {"k": query, "l": location, "c": "Alternance", "p": page}
    return f"{BASE}/fr-fr/emploi/recherche.html?{urlencode(params)}"


def offer_url(offer_id: str) -> str:
    return f"{BASE}/fr-fr/emplois/{offer_id}.html"


def parse_search(page_html: str) -> list[str]:
    """Identifiants des offres d'une page de résultats, dans l'ordre, sans doublon."""
    ids = re.findall(r'data-id-storage-item-id="(\d+)"', page_html)
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


def _parse_anonymous(offer_id: str, page_html: str) -> Offer | None:
    """Les offres d'entreprises anonymes n'ont pas de JSON-LD : on lit le HTML de la fiche."""
    title = re.search(r'data-cy="jobTitle"[^>]*>(.*?)<', page_html, re.S)
    start = re.search(r"Les missions du poste|Détail du poste", page_html)
    if not title or not start:
        return None
    # Le <title> se termine par « Ville (75) | Hellowork » : seul le département est fiable.
    department = re.search(r"\((\d{2,3}|2[AB])\)\s*\|\s*Hellowork</title>", page_html)
    return Offer(
        id=offer_id,
        url=offer_url(offer_id),
        title=html.unescape(title.group(1)).strip(),
        company="Entreprise anonyme",
        location=f"département {department.group(1)}" if department else "",
        description=_text(page_html[start.start() : start.start() + 15_000])[:8000],
        on_site=not EXTERNAL.search(page_html),
    )


def parse_offer(offer_id: str, page_html: str) -> Offer | None:
    """Lit le bloc JSON-LD JobPosting de la fiche, ou le HTML pour une offre anonyme.

    None si la fiche n'a ni l'un ni l'autre (offre expirée ou balisage changé).
    """
    for block in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page_html, re.S):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "JobPosting":
            continue
        company = (data.get("hiringOrganization") or {}).get("name") or ""
        return Offer(
            id=offer_id,
            url=offer_url(offer_id),
            title=html.unescape(data.get("title", "")).strip(),
            company=html.unescape(company).strip(),
            location=_location(data),
            description=_text(data.get("description", "")),
            on_site=not EXTERNAL.search(page_html),
        )
    return _parse_anonymous(offer_id, page_html)


async def dismiss_cookies(page: Page) -> None:
    """HelloWork utilise sa propre bannière, que le refus automatique de WebSkrap ne connaît pas."""
    try:
        await page.click(".hw-cc-notice-modal__btn-continue-without-accepting", timeout=5_000)
    except PlaywrightTimeout:
        pass  # déjà refusée dans ce profil, ou pas affichée


async def search(page: Page, query: str, location: str, pages: int) -> list[str]:
    ids: list[str] = []
    for n in range(1, pages + 1):
        await page.goto(search_url(query, location, n), wait_until="domcontentloaded")
        found = parse_search(await page.content())
        if not found:
            break
        ids.extend(found)
    return list(dict.fromkeys(ids))


async def fetch_offer(page: Page, offer_id: str) -> Offer | None:
    await page.goto(offer_url(offer_id), wait_until="domcontentloaded")
    return parse_offer(offer_id, await page.content())


@dataclass(frozen=True)
class ApplyResult:
    status: str  # applied | needs_review | external | rejected (annonce d'école, offre expirée)
    detail: str


async def _human_click(session: WebSkrapSession, page: Page, selector: str) -> None:
    """Clic à la souris sur une trajectoire humaine (WebSkrap).

    `human_click` clique à des coordonnées : si un calque recouvre la cible,
    c'est lui qui recevrait le clic. Le clic d'essai de Playwright vérifie
    d'abord que la cible est visible, stable et non masquée, sans cliquer.
    """
    await page.locator(selector).click(trial=True)
    await session.human_click(page, selector)


async def _answer_questions(page: Page, submit: Locator, answers: questions.Answers) -> str | None:
    """Répond aux questions obligatoires vides du formulaire de `submit`.

    Renvoie le libellé de la question si elle trahit une annonce d'école, sinon None.
    Les questions inconnues restent vides : `_blocking_fields` les signalera.
    """
    fields = await submit.evaluate(
        """b => b.form ? [...b.form.elements].filter(e => e.required && e.offsetParent !== null
                && !e.value && (e.tagName === 'SELECT' || ['text', 'number'].includes(e.type)))
            .map(e => ({
                name: e.name,
                label: ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || '').trim(),
                options: e.tagName === 'SELECT' ? [...e.options].map(o => [o.value, o.text.trim()]) : null,
            })) : []"""
    )
    for field in fields:
        options = [tuple(o) for o in field["options"]] if field["options"] is not None else None
        value = questions.answer(field["label"], options, answers)
        if value == questions.SCHOOL:
            return field["label"]
        if value is None or not field["name"]:
            continue
        target = page.locator(f"{APPLY} [name={json.dumps(field['name'])}]")
        if options is None:
            await target.fill(value)
        else:
            await target.select_option(value)
    return None


async def _fill_if_empty(zone: Locator, selector: str, value: str) -> None:
    field = zone.locator(selector)
    if await field.count() and await field.is_visible() and not await field.input_value():
        await field.fill(value)


async def _blocking_fields(submit: Locator) -> list[str]:
    """Champs obligatoires visibles et vides du formulaire de `submit`.

    On ne soumet jamais une étape incomplète : `form.elements` inclut aussi les
    champs rattachés par l'attribut `form=`, comme le fait HelloWork.
    """
    # Les cases à cocher sont masquées et stylées via leur <label> : un champ
    # compte comme visible si lui-même ou l'un de ses labels l'est.
    return await submit.evaluate(
        """b => b.form ? [...b.form.elements].filter(e => e.required
                && (e.offsetParent !== null || [...(e.labels || [])].some(l => l.offsetParent !== null))
                && (e.type === 'checkbox' || e.type === 'radio' ? !e.checked : !e.value))
            .map(e => e.name || e.id) : ['(bouton hors formulaire)']"""
    )


async def _enter_code(page: Page, zone: Locator, code: str) -> None:
    """Saisit le code dans les cases de l'étape de validation.

    HelloWork affiche une case par chiffre et valide seul au 6ᵉ : on tape dans la
    première case vide et le focus avance. « Valider » ne sert que si la
    validation automatique n'a pas eu lieu.
    """
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"code invalide : {code!r}")
    first = zone.locator("[data-cy='otp-input-1'], input[aria-label='Chiffre 1 du code']").first
    await first.click()
    await page.keyboard.type(code, delay=150)
    await page.wait_for_timeout(3_000)
    validate = zone.get_by_role("button", name="Valider")
    if await validate.count() and await validate.first.is_visible() and await validate.first.is_enabled():
        await validate.first.click()
    await _settle(page)


MY_APPLICATIONS = f"{BASE}/fr-fr/candidat/mes-candidatures.html"


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def is_listed(cards: list[str], title: str, company: str) -> bool:
    """Vrai si une même carte de « Mes candidatures » porte ce titre et cette entreprise.

    La liste n'affiche pas l'identifiant de l'offre : titre et entreprise doivent
    donc figurer ensemble sur une carte. Les offres anonymes n'y montrent pas de
    nom d'entreprise, d'où la comparaison sur le seul titre dans ce cas.
    """
    title, company = _norm(title), _norm(company)
    anonymous = company in ("", "entreprise anonyme")
    return any(title in card and (anonymous or company in card) for card in map(_norm, cards))


async def my_applications(page: Page) -> list[str]:
    """Texte de chaque carte de la page « Mes candidatures » (première page, les plus récentes)."""
    await page.goto(MY_APPLICATIONS, wait_until="domcontentloaded")
    if "mes-candidatures" not in page.url:
        raise RuntimeError(f"« Mes candidatures » inaccessible (session HelloWork expirée ?) : {page.url}")
    return await page.eval_on_selector_all(
        "a[href*='/candidat/mes-candidatures/']",
        """links => links.map(a => {
            let card = a;
            while (card.parentElement && card.innerText.trim().length < 40) card = card.parentElement;
            return card.innerText;
        })""",
    )


async def apply(
    session: WebSkrapSession,
    page: Page,
    offer_url_: str,
    *,
    title: str,
    company: str,
    first_name: str,
    last_name: str,
    email: str,
    cv_pdf: Path,
    letter: str,
    screenshot: Path,
    answers: questions.Answers,
    otp: Callable[[], Awaitable[str | None]] | None = None,
    max_steps: int = 4,
) -> ApplyResult:
    """Remplit et soumet le formulaire HelloWork, étape par étape.

    La preuve d'envoi est la présence de l'offre dans « Mes candidatures » :
    vérifiée avant (jamais deux candidatures à la même offre) et après l'envoi.
    S'arrête en `needs_review` dès qu'une étape contient un champ obligatoire
    inconnu ou que l'envoi n'est pas vérifiable : mieux vaut une candidature à
    finir à la main qu'une candidature envoyée incomplète ou en double.

    `otp` fournit le code de validation email quand HelloWork le demande. Sans
    lui, cette étape arrête la candidature en `needs_review`.
    """
    if len(letter) > LETTER_MAX:
        return ApplyResult(
            "needs_review", f"lettre de {len(letter)} caractères, HelloWork coupe à {LETTER_MAX}"
        )
    try:
        if is_listed(await my_applications(page), title, company):
            return ApplyResult("applied", "déjà dans « Mes candidatures », rien de renvoyé")
    except RuntimeError:
        pass  # pas encore de compte connecté : la première candidature le créera

    await page.goto(offer_url_, wait_until="domcontentloaded")
    await dismiss_cookies(page)
    content = await page.content()
    if EXTERNAL.search(content):
        return ApplyResult("external", "candidature sur le site du recruteur ou d'un partenaire")
    if not await page.locator("[data-cy='applyButtonHeader']").count() and EXPIRED.search(content):
        return ApplyResult("rejected", "offre expirée")

    await _human_click(session, page, "[data-cy='applyButtonHeader'] >> nth=0")
    zone = page.locator(APPLY)
    await zone.locator("[data-cy='submitButton']").wait_for(state="attached", timeout=15_000)

    # Profil déjà connecté : HelloWork pré-remplit ou masque ces champs.
    await _fill_if_empty(zone, "input[name='Firstname']", first_name)
    await _fill_if_empty(zone, "input[name='LastName']", last_name)
    await _fill_if_empty(zone, "input[name='Email']", email)

    resume = zone.locator("input[name='JweHashResume']")
    if await resume.count() and not await resume.input_value():
        # Le champ fichier est envoyé tout seul à /uploadcv ; HelloWork renvoie
        # un jeton dans JweHashResume une fois le CV accepté.
        await zone.locator("input[name='upload']").set_input_files(cv_pdf)
        await page.wait_for_function(
            "() => document.querySelector(\"#postuler input[name='JweHashResume']\")?.value", timeout=30_000
        )

    if letter:
        if not await zone.locator("#cover-letter-collapse-funnel").is_checked():
            await _human_click(session, page, f"{APPLY} label[for='cover-letter-collapse-funnel']")
        await zone.locator("textarea[name='MotivationLetter']").fill(letter)

    cgu = zone.locator("input[name='HasAcceptedCGU']")
    if await cgu.count() and not await cgu.is_checked():
        await cgu.check()

    shot = lambda suffix: screenshot.with_name(f"{screenshot.stem}-{suffix}.png")  # noqa: E731
    clicked = 0  # étapes soumises : dès la première, une erreur ne doit plus donner "failed"
    try:
        for step in range(1, max_steps + 1):
            # Chaque étape a son bouton (« Continuer », « Postuler », « Envoyer ma candidature »).
            submit_selector = f"{APPLY} button[type=submit]:visible >> nth=0"
            submit = page.locator(submit_selector)
            if not await submit.count():
                break  # plus de formulaire : envoyé, ou page inattendue ; la vérification tranche
            school = await _answer_questions(page, submit, answers)
            if school:
                await page.screenshot(path=screenshot, full_page=True)
                return ApplyResult("rejected", f"annonce d'école : « {school} »")
            missing = await _blocking_fields(submit)
            if missing:
                await page.screenshot(path=screenshot, full_page=True)
                return ApplyResult("needs_review", f"étape {step} : champs obligatoires vides {missing}")
            await page.screenshot(path=shot(f"etape{step}-avant"), full_page=True)
            await _human_click(session, page, submit_selector)
            clicked = step
            await _settle(page)
            await page.screenshot(path=shot(f"etape{step}-apres"), full_page=True)
            # Page entière : après l'envoi, HelloWork quitte la fiche et la zone n'existe plus.
            if OTP_STEP.search(await page.inner_text("body")):
                screenshot.with_suffix(".html").write_text(await zone.inner_html(), encoding="utf-8")
                code = await otp() if otp else None
                if not code:
                    await page.screenshot(path=screenshot, full_page=True)
                    return ApplyResult("needs_review", "code de validation HelloWork demandé, non saisi")
                await _enter_code(page, zone, code)
                await page.screenshot(path=shot(f"etape{step}-code"), full_page=True)
                if OTP_STEP.search(await page.inner_text("body")):
                    return ApplyResult("needs_review", "code de validation refusé ou expiré")
        await page.screenshot(path=screenshot, full_page=True)
        if is_listed(await my_applications(page), title, company):
            return ApplyResult("applied", f"vérifiée dans « Mes candidatures » après {clicked} étape(s)")
    except Exception as exc:
        if not clicked:
            raise  # rien n'est parti : l'appelant marque l'offre "failed"
        return ApplyResult("needs_review", f"erreur après l'étape {clicked} : {exc}".splitlines()[0])
    return ApplyResult("needs_review", "absente de « Mes candidatures » après l'envoi")


async def _settle(page: Page) -> None:
    """Les étapes arrivent par Turbo (fetch) : laisser la requête partir, puis attendre
    le calme réseau, que les traqueurs peuvent empêcher."""
    await page.wait_for_timeout(2_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeout:
        pass
