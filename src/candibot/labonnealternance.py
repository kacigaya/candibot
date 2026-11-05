"""La bonne alternance (alternance seulement) : recherche par l'API publique, candidature par le site.

API : https://api.apprentissage.beta.gouv.fr/fr/documentation-technique (relevée le
05/11/2026). La recherche demande une clé d'API (LBA_API_KEY dans .env), sans habilitation.
L'envoi par l'API exige l'habilitation `applications:write`, accordée sur demande : la
candidature passe donc par le formulaire « Candidature simplifiée » du site, sans compte.

Sélecteurs relevés le 05/11/2026 sur le site et dans son code source
(github.com/mission-apprentissage/labonnealternance, ui/components/ItemDetail/CandidatureLba).
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from patchright.async_api import Page
from patchright.async_api import TimeoutError as PlaywrightTimeout
from webskrap import WebSkrapSession

from candibot.application import ApplyResult, humanClick
from candibot.hellowork import Offer, htmlText

SITE = "labonnealternance"
API = "https://api.apprentissage.beta.gouv.fr/api"
BASE = "https://labonnealternance.apprentissage.beta.gouv.fr"
PREFIX = "lba-"  # préfixe des identifiants en base, pour les distinguer des offres HelloWork
ROME = re.compile(r"^[A-N]\d{4}$")
# Bouton du formulaire. La fiche en contient deux (mise en page mobile et bureau) : seul un est visible.
APPLY = "[data-testid='postuler-button']:visible >> nth=0"
SUBMIT = "[data-testid='candidature-not-sent']"
SENT = "Votre candidature a bien été envoyée"
FAILED = "[data-testid^='CandidatureSpontaneeFailed']"


def _key() -> str:
    key = os.environ.get("LBA_API_KEY", "")
    if not key:
        raise RuntimeError("LBA_API_KEY absente de .env : créez une clé sur api.apprentissage.beta.gouv.fr")
    return key


def _get(path: str) -> dict:
    request = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"Bearer {_key()}"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # La documentation annonce 429, le schéma de réponse 419 : les deux signalent la limite de débit.
            if exc.code in (419, 429) and attempt < 2:
                time.sleep(float(exc.headers.get("Retry-After", "10")))
                continue
            raise
    raise AssertionError("unreachable")


def searchParams(romes: tuple[str, ...], departements: tuple[str, ...]) -> str:
    """Paramètres de recherche. Sans département, la recherche couvre toute la France."""
    params = [("romes", ",".join(romes)), *(("departements", d) for d in departements)]
    return urlencode(params)


def offerUrl(partnerLabel: str, jobId: str) -> str:
    """Fiche de l'offre sur le site. Le dernier segment (intitulé) est libre : le site l'ignore."""
    kind = "offres_emploi_lba" if partnerLabel == "offres_emploi_lba" else "offres_emploi_partenaires"
    return f"{BASE}/emploi/{kind}/{jobId}/offre"


def _list(title: str, items) -> str:
    items = [str(i).strip() for i in items or [] if str(i).strip()]
    return f"\n\n{title} :\n" + "\n".join(f"- {i}" for i in items) if items else ""


def parseJob(job: dict) -> Offer:
    ident, place, offer = job["identifier"], job["workplace"], job["offer"]
    jobId = ident.get("id")
    questions = offer.get("to_applicant_questions") or []
    description = htmlText(offer.get("description") or "")
    description += _list("Compétences attendues", offer.get("desired_skills"))
    description += _list("Compétences à acquérir", offer.get("to_be_acquired_skills"))
    description += _list("Conditions d'accès", offer.get("access_conditions"))
    description += _list("Questions du recruteur", questions)
    if job.get("is_delegated"):
        description += "\n\nRecrutement géré par une école ou un CFA partenaire."
    # `id` manque pour certains partenaires : leur identifiant n'est unique que chez eux, et
    # l'offre n'a pas de fiche sur le site.
    raw = jobId or f"{ident['partner_label']}-{ident['partner_job_id']}"
    return Offer(
        id=PREFIX + re.sub(r"[^\w.-]+", "_", raw),
        url=offerUrl(ident["partner_label"], jobId) if jobId else job["apply"]["url"],
        title=offer["title"].strip(),
        company=(
            place.get("name") or place.get("brand") or place.get("legal_name") or "Entreprise anonyme"
        ).strip(),
        location=((place.get("location") or {}).get("address") or "").strip(),
        description=description.strip()[:8000],
        # Sans `recipient_id`, le site n'a pas de formulaire : candidature chez le recruteur. Les
        # questions du recruteur sont obligatoires dans le formulaire : réponse à la main.
        onSite=bool(jobId and job["apply"].get("recipient_id") and not questions),
        site=SITE,
    )


def search(romes: tuple[str, ...], departements: tuple[str, ...]) -> list[Offer]:
    """Offres d'alternance des métiers `romes`. L'API ne pagine pas : 150 offres au plus par source."""
    data = _get(f"/job/v1/search?{searchParams(romes, departements)}")
    offers = [parseJob(job) for job in data["jobs"]]
    return list({o.id: o for o in offers}.values())


async def _openForm(session: WebSkrapSession | None, page: Page, url: str) -> ApplyResult | None:
    """Ouvre le formulaire de la fiche. Renvoie un résultat si la candidature est impossible ou déjà faite."""
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.locator("[data-testid^='candidater-']").first.wait_for(state="attached", timeout=15_000)
    except PlaywrightTimeout:
        return ApplyResult("failed", "bloc de candidature introuvable (offre retirée ou site changé)")
    # Le site garde la date d'envoi dans le navigateur (`application-<type>-<id>`) et remplace
    # alors le bouton par le statut de la candidature.
    path = url.removeprefix(BASE).split("/")
    if await page.evaluate("k => localStorage.getItem(k) !== null", f"application-{path[2]}-{path[3]}"):
        return ApplyResult("applied", "déjà envoyée depuis ce navigateur, rien de renvoyé")
    if not await page.locator(APPLY).count():
        return ApplyResult("rejected", "offre expirée ou sans candidature simplifiée")
    if session is None:
        await page.click(APPLY)
    else:
        await humanClick(session, page, APPLY)
    await page.locator(SUBMIT).wait_for(state="visible", timeout=15_000)
    return None


async def apply(
    session: WebSkrapSession,
    page: Page,
    url: str,
    *,
    firstName: str,
    lastName: str,
    email: str,
    phone: str,
    cvPdf: Path,
    letter: str,
    screenshot: Path,
) -> ApplyResult:
    """Remplit et envoie le formulaire « Candidature simplifiée » de la fiche.

    La preuve d'envoi est le message de confirmation du site. Tout échec avant le clic
    d'envoi renvoie `failed` (rien n'est parti) ; un envoi sans réponse claire, `needs_review`.
    """
    stopped = await _openForm(session, page, url)
    if stopped is not None:
        return stopped
    form = page.locator("form").filter(has=page.locator(SUBMIT))
    if await form.locator("textarea[name^='custom_question_']").count():
        return ApplyResult("external", "questions du recruteur obligatoires : à faire à la main")
    await form.locator("#applicant_last_name").fill(lastName)
    await form.locator("#applicant_first_name").fill(firstName)
    await form.locator("#applicant_email").fill(email)
    await form.locator("#applicant_phone").fill(phone)
    await form.locator("textarea[name='applicant_message']").fill(letter)
    await form.locator("input[type='file']").set_input_files(cvPdf)
    try:
        # Le site lit le fichier puis affiche son nom : avant cela, le CV manque à l'envoi.
        await form.get_by_text(cvPdf.name).first.wait_for(timeout=10_000)
    except PlaywrightTimeout:
        return ApplyResult("failed", "CV non chargé dans le formulaire")
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(screenshot.with_name(f"{screenshot.stem}-form.png")), full_page=True)

    await humanClick(session, page, SUBMIT)
    outcome = page.get_by_text(SENT).or_(page.locator(FAILED))
    try:
        await outcome.first.wait_for(timeout=30_000)
    except PlaywrightTimeout:
        await page.screenshot(path=str(screenshot), full_page=True)
        # Formulaire encore là avec un champ en erreur : le site a bloqué l'envoi avant la requête.
        invalid = await form.locator("[aria-invalid='true']").evaluate_all(
            "els => els.map(e => e.name || e.id)"
        )
        if invalid:
            return ApplyResult("failed", f"champs refusés par le formulaire : {', '.join(invalid)}")
        return ApplyResult("needs_review", "pas de confirmation du site après l'envoi")
    await page.screenshot(path=str(screenshot), full_page=True)
    if await page.get_by_text(SENT).count():
        return ApplyResult("applied", "confirmée par le site")
    error = (await page.locator(FAILED).first.inner_text()).strip()
    if "expirée" in error:
        return ApplyResult("rejected", "offre expirée")
    # Les autres erreurs (limite de candidatures, fichier refusé, erreur technique) arrivent
    # en réponse à la requête : le site n'a pas transmis la candidature.
    return ApplyResult("failed", f"refusée par le site : {error}")


async def check(page: Page, offers: list[Offer]) -> list[str]:
    """Ouvre le formulaire de la première offre à candidature simplifiée, sans le soumettre."""
    for offer in offers[:10]:
        if not offer.onSite:
            continue
        try:
            stopped = await _openForm(None, page, offer.url)
        except PlaywrightTimeout:
            return [f"La bonne alternance, {offer.url} : formulaire introuvable ({SUBMIT})"]
        if stopped is not None:
            continue
        missing = [
            selector
            for selector in (
                "#applicant_last_name",
                "#applicant_first_name",
                "#applicant_email",
                "#applicant_phone",
                "textarea[name='applicant_message']",
                "input[type='file']",
            )
            if not await page.locator(selector).count()
        ]
        return [f"La bonne alternance, {offer.url} : champs absents {missing}"] if missing else []
    return ["La bonne alternance : aucune candidature simplifiée parmi les 10 premières offres"]
