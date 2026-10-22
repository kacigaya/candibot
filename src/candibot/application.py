"""Candidature sur HelloWork : formulaire en plusieurs étapes et vérification de l'envoi.

Sélecteurs relevés sur le site le 22/10/2026. Si HelloWork change son formulaire,
les corrections se font ici. `candibot check` vérifie ces sélecteurs sans rien envoyer.
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from patchright.async_api import Locator, Page
from patchright.async_api import TimeoutError as PlaywrightTimeout
from webskrap import WebSkrapSession

from candibot import questions
from candibot.hellowork import BASE, EXTERNAL, dismissCookies, fetchOffer, parseSearch, searchUrl

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
class ApplyResult:
    status: str  # applied | needs_review | external | rejected (annonce d'école, offre expirée)
    detail: str


async def _humanClick(session: WebSkrapSession, page: Page, selector: str) -> None:
    """Clic à la souris sur une trajectoire humaine (WebSkrap).

    `human_click` clique à des coordonnées : si un calque recouvre la cible,
    c'est lui qui recevrait le clic. Le clic d'essai de Playwright vérifie
    d'abord que la cible est visible, stable et non masquée, sans cliquer.
    """
    await page.locator(selector).click(trial=True)
    await session.human_click(page, selector)


async def _answerQuestions(page: Page, submit: Locator, answers: questions.Answers) -> str | None:
    """Répond aux questions obligatoires vides du formulaire de `submit`.

    Renvoie le libellé de la question si elle trahit une annonce d'école, sinon None.
    Les questions inconnues restent vides : `_blockingFields` les signalera.
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


async def _fillIfEmpty(zone: Locator, selector: str, value: str) -> None:
    field = zone.locator(selector)
    if await field.count() and await field.is_visible() and not await field.input_value():
        await field.fill(value)


async def _blockingFields(submit: Locator) -> list[str]:
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


async def _enterCode(page: Page, zone: Locator, code: str) -> None:
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


def isListed(cards: list[str], title: str, company: str) -> bool:
    """Vrai si une même carte de « Mes candidatures » porte ce titre et cette entreprise.

    La liste n'affiche pas l'identifiant de l'offre : titre et entreprise doivent
    donc figurer ensemble sur une carte. Les offres anonymes n'y montrent pas de
    nom d'entreprise, d'où la comparaison sur le seul titre dans ce cas.
    """
    title, company = _norm(title), _norm(company)
    anonymous = company in ("", "entreprise anonyme")
    return any(title in card and (anonymous or company in card) for card in map(_norm, cards))


async def myApplications(page: Page) -> list[str]:
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
    url: str,
    *,
    title: str,
    company: str,
    firstName: str,
    lastName: str,
    email: str,
    cvPdf: Path,
    letter: str,
    screenshot: Path,
    answers: questions.Answers,
    otp: Callable[[], Awaitable[str | None]] | None = None,
    maxSteps: int = 4,
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
        if isListed(await myApplications(page), title, company):
            return ApplyResult("applied", "déjà dans « Mes candidatures », rien de renvoyé")
    except RuntimeError:
        pass  # pas encore de compte connecté : la première candidature le créera

    await page.goto(url, wait_until="domcontentloaded")
    await dismissCookies(page)
    content = await page.content()
    if EXTERNAL.search(content):
        return ApplyResult("external", "candidature sur le site du recruteur ou d'un partenaire")
    if not await page.locator("[data-cy='applyButtonHeader']").count() and EXPIRED.search(content):
        return ApplyResult("rejected", "offre expirée")

    await _humanClick(session, page, "[data-cy='applyButtonHeader'] >> nth=0")
    zone = page.locator(APPLY)
    await zone.locator("[data-cy='submitButton']").wait_for(state="attached", timeout=15_000)

    # Profil déjà connecté : HelloWork pré-remplit ou masque ces champs.
    await _fillIfEmpty(zone, "input[name='Firstname']", firstName)
    await _fillIfEmpty(zone, "input[name='LastName']", lastName)
    await _fillIfEmpty(zone, "input[name='Email']", email)

    resume = zone.locator("input[name='JweHashResume']")
    if await resume.count() and not await resume.input_value():
        # Le champ fichier est envoyé tout seul à /uploadcv ; HelloWork renvoie
        # un jeton dans JweHashResume une fois le CV accepté.
        await zone.locator("input[name='upload']").set_input_files(cvPdf)
        await page.wait_for_function(
            "() => document.querySelector(\"#postuler input[name='JweHashResume']\")?.value", timeout=30_000
        )

    if letter:
        if not await zone.locator("#cover-letter-collapse-funnel").is_checked():
            await _humanClick(session, page, f"{APPLY} label[for='cover-letter-collapse-funnel']")
        await zone.locator("textarea[name='MotivationLetter']").fill(letter)

    cgu = zone.locator("input[name='HasAcceptedCGU']")
    if await cgu.count() and not await cgu.is_checked():
        await cgu.check()

    shot = lambda suffix: screenshot.with_name(f"{screenshot.stem}-{suffix}.png")  # noqa: E731
    clicked = 0  # étapes soumises : dès la première, une erreur ne doit plus donner "failed"
    try:
        for step in range(1, maxSteps + 1):
            # Chaque étape a son bouton (« Continuer », « Postuler », « Envoyer ma candidature »).
            submitSelector = f"{APPLY} button[type=submit]:visible >> nth=0"
            submit = page.locator(submitSelector)
            if not await submit.count():
                break  # plus de formulaire : envoyé, ou page inattendue ; la vérification tranche
            school = await _answerQuestions(page, submit, answers)
            if school:
                await page.screenshot(path=screenshot, full_page=True)
                return ApplyResult("rejected", f"annonce d'école : « {school} »")
            missing = await _blockingFields(submit)
            if missing:
                await page.screenshot(path=screenshot, full_page=True)
                return ApplyResult("needs_review", f"étape {step} : champs obligatoires vides {missing}")
            await page.screenshot(path=shot(f"etape{step}-avant"), full_page=True)
            await _humanClick(session, page, submitSelector)
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
                await _enterCode(page, zone, code)
                await page.screenshot(path=shot(f"etape{step}-code"), full_page=True)
                if OTP_STEP.search(await page.inner_text("body")):
                    return ApplyResult("needs_review", "code de validation refusé ou expiré")
        await page.screenshot(path=screenshot, full_page=True)
        if isListed(await myApplications(page), title, company):
            return ApplyResult("applied", f"vérifiée dans « Mes candidatures » après {clicked} étape(s)")
    except Exception as exc:
        if not clicked:
            raise  # rien n'est parti : l'appelant marque l'offre "failed"
        return ApplyResult("needs_review", f"erreur après l'étape {clicked} : {exc}".splitlines()[0])
    return ApplyResult("needs_review", "absente de « Mes candidatures » après l'envoi")


async def check(page: Page, query: str, location: str, contracts: tuple[str, ...]) -> list[str]:
    """Vérifie les sélecteurs sur le site réel, sans rien envoyer. Renvoie les problèmes trouvés.

    Lit une page de résultats, puis la première fiche à candidature HelloWork
    parmi les 5 premières, et ouvre son formulaire sans le soumettre.
    """
    await page.goto(searchUrl(query, location, contracts, 1), wait_until="domcontentloaded")
    await dismissCookies(page)
    ids = parseSearch(await page.content())
    if not ids:
        return ["recherche : aucune offre trouvée (data-id-storage-item-id)"]
    problems = []
    for offerId in ids[:5]:
        offer = await fetchOffer(page, offerId)
        if offer is None:
            problems.append(f"fiche {offerId} : illisible (ni JSON-LD JobPosting, ni repli HTML)")
            continue
        if not offer.onSite:
            continue
        zone = page.locator(APPLY)
        try:
            await page.click("[data-cy='applyButtonHeader'] >> nth=0", timeout=15_000)
            await zone.locator("[data-cy='submitButton']").wait_for(state="attached", timeout=15_000)
        except PlaywrightTimeout:
            return [*problems, f"fiche {offerId} : formulaire introuvable ({APPLY} [data-cy='submitButton'])"]
        if not await zone.locator("textarea[name='MotivationLetter']").count():
            problems.append(f"fiche {offerId} : champ du message absent (textarea[name='MotivationLetter'])")
        return problems
    return [*problems, f"aucune candidature sur HelloWork parmi les offres {', '.join(ids[:5])}"]


async def _settle(page: Page) -> None:
    """Les étapes arrivent par Turbo (fetch) : laisser la requête partir, puis attendre
    le calme réseau, que les traqueurs peuvent empêcher."""
    await page.wait_for_timeout(2_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeout:
        pass
