"""Parcours `labonnealternance.apply` dans un vrai Chromium, contre une fiche simulée.

Les requêtes sont interceptées : aucun accès réseau. La fiche reprend les sélecteurs
relevés sur le site le 05/11/2026 (voir labonnealternance.py). Le formulaire s'ouvre
au clic, affiche le nom du CV chargé, et répond comme le site après l'envoi.
"""

import asyncio
import json

from patchright.async_api import Route, async_playwright

from candibot import labonnealternance

HTML = "text/html; charset=utf-8"
URL = labonnealternance.offerUrl("offres_emploi_lba", "abc123")

# `{reply}` : code JavaScript qui remplace le formulaire après l'envoi, comme le site.
OFFER = """<html><body>
<div data-testid="candidater-offres_emploi_lba">
  <button data-testid="postuler-button" style="display:none">Candidature simplifiée</button>
  <button data-testid="postuler-button" onclick="document.getElementById('modal').hidden = false">
    Candidature simplifiée</button>
</div>
<div id="modal" hidden>
  <form onsubmit="event.preventDefault(); send(this)">
    <input id="applicant_last_name" name="applicant_last_name">
    <input id="applicant_first_name" name="applicant_first_name">
    <input id="applicant_email" name="applicant_email" type="email">
    <input id="applicant_phone" name="applicant_phone" type="tel">
    <textarea name="applicant_message"></textarea>
    {extra}
    <input id="applicant_attachment_name" type="file"
      onchange="document.getElementById('cv').textContent = this.files[0].name">
    <span id="cv"></span>
    <button type="submit" data-testid="candidature-not-sent">J'envoie ma candidature</button>
  </form>
</div>
<script>
function send(form) {
  const values = Object.fromEntries([...form.elements].filter(e => e.name).map(e => [e.name, e.value]));
  values.cv = document.getElementById('cv').textContent;
  fetch('/api/v2/_private/application', {method: 'POST', body: JSON.stringify(values)});
  {reply}
}
</script>
</body></html>"""
SENT = "document.getElementById('modal').innerHTML = '<h1>Votre candidature a bien été envoyée à ACME</h1>'"
FAILED = (
    "document.getElementById('modal').innerHTML = '<div role=alert><h3"
    " data-testid=CandidatureSpontaneeFailedTooManyApplicationsPerCompany>Limite atteinte</h3></div>'"
)


class FakeSession:
    """Remplace WebSkrapSession : le clic « humain » devient un clic Playwright."""

    async def human_click(self, page, selector: str) -> None:
        await page.click(selector)


def runApply(tmp_path, *, reply: str = SENT, extra: str = "", applied: bool = False):
    """Lance `apply` sur la fiche simulée. Renvoie le résultat et les envois reçus."""
    posts: list[dict] = []
    offer = OFFER.replace("{extra}", extra).replace("{reply}", reply)
    (tmp_path / "cv.pdf").write_bytes(b"%PDF-1.4")

    async def handle(route: Route) -> None:
        request = route.request
        if request.method == "POST":
            posts.append(json.loads(request.post_data or "{}"))
            await route.fulfill(status=200, body="{}")
        elif request.url.startswith(URL):
            await route.fulfill(content_type=HTML, body=offer)
        else:
            await route.fulfill(status=204, body="")

    async def main():
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox"])
            page = await browser.new_page()
            await page.route("**/*", handle)
            if applied:  # date d'envoi gardée par le site après une candidature
                await page.goto(URL)
                await page.evaluate("localStorage.setItem('application-offres_emploi_lba-abc123', '1')")
            try:
                return await labonnealternance.apply(
                    FakeSession(),
                    page,
                    URL,
                    firstName="Maxime",
                    lastName="NOM",
                    email="maxime@example.com",
                    phone="0612345678",
                    cvPdf=tmp_path / "cv.pdf",
                    letter="Bonjour,\nMaxime NOM",
                    screenshot=tmp_path / "shots" / "1.png",
                )
            finally:
                await browser.close()

    return asyncio.run(main()), posts


def testApplyFillsFormAndReadsConfirmation(tmp_path):
    result, posts = runApply(tmp_path)
    assert result.status == "applied", result.detail
    assert posts == [
        {
            "applicant_last_name": "NOM",
            "applicant_first_name": "Maxime",
            "applicant_email": "maxime@example.com",
            "applicant_phone": "0612345678",
            "applicant_message": "Bonjour,\nMaxime NOM",
            "cv": "cv.pdf",
        }
    ]
    assert (tmp_path / "shots" / "1-form.png").is_file() and (tmp_path / "shots" / "1.png").is_file()


def testApplyReportsSiteRefusal(tmp_path):
    result, posts = runApply(tmp_path, reply=FAILED)
    assert result.status == "failed" and "Limite atteinte" in result.detail
    assert len(posts) == 1


def testApplyLeavesRecruiterQuestionsToTheCandidate(tmp_path):
    result, posts = runApply(tmp_path, extra='<textarea name="custom_question_0"></textarea>')
    assert result.status == "external"
    assert posts == []


def testApplySkipsOffersAlreadySentFromThisBrowser(tmp_path):
    result, posts = runApply(tmp_path, applied=True)
    assert result.status == "applied" and "déjà envoyée" in result.detail
    assert posts == []
