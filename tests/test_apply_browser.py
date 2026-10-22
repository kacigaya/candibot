"""Parcours `hellowork.apply` dans un vrai Chromium, contre des pages HelloWork simulées.

Les requêtes vers hellowork.com sont interceptées : aucun accès réseau. Le
formulaire reprend les sélecteurs relevés sur le site (voir hellowork.py).
"""

import asyncio
from urllib.parse import urlparse

from patchright.async_api import Route, async_playwright

from candibot import hellowork
from candibot.questions import Answers

HTML = "text/html; charset=utf-8"
TITLE, COMPANY = "Chargé de Communication H/F", "ACME"
ANSWERS = Answers("Madame", "immédiate", 1867.02, "100 % du SMIC")

OFFER = """<html><body>
<a data-cy="applyButtonHeader" href="#postuler">Postuler</a>
<div id="postuler">
  <form method="post" action="/fr-fr/postuler">
    <input name="Firstname" required>
    <input name="LastName" required>
    <input name="Email" required>
    <input type="hidden" name="JweHashResume" value="jeton-cv">
    <input type="checkbox" id="cover-letter-collapse-funnel">
    <label for="cover-letter-collapse-funnel">Ajouter un message</label>
    <textarea name="MotivationLetter"></textarea>
    <input type="checkbox" name="HasAcceptedCGU" required>
    {extra}
    <button type="submit" data-cy="submitButton">Postuler</button>
  </form>
</div>
</body></html>"""

CARD = (
    f'<div><a href="/fr-fr/candidat/mes-candidatures/1">{TITLE}</a>'
    f"<p>{COMPANY}</p><p>Paris - 75</p><p>Envoyée le 22 octobre</p></div>"
)


class FakeSession:
    """Remplace WebSkrapSession : le clic « humain » devient un clic Playwright."""

    async def human_click(self, page, selector: str) -> None:
        await page.click(selector)


def runApply(tmp_path, *, offerHtml: str):
    """Lance `apply` sur l'offre simulée. Renvoie le résultat et les envois reçus."""
    posts: list[str] = []

    async def handle(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if request.method == "POST":
            posts.append(request.post_data or "")
            await route.fulfill(content_type=HTML, body="<html><body>Merci</body></html>")
        elif path.endswith("/mes-candidatures.html"):
            await route.fulfill(content_type=HTML, body=f"<html><body>{CARD if posts else ''}</body></html>")
        elif path.startswith("/fr-fr/emplois/"):
            await route.fulfill(content_type=HTML, body=offerHtml)
        else:
            await route.fulfill(status=204, body="")

    async def main():
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--no-sandbox"])
            page = await browser.new_page()
            await page.route("**/*", handle)
            try:
                return await hellowork.apply(
                    FakeSession(),
                    page,
                    hellowork.offerUrl("1"),
                    title=TITLE,
                    company=COMPANY,
                    firstName="Maxime",
                    lastName="NOM",
                    email="maxime@example.com",
                    cvPdf=tmp_path / "cv.pdf",
                    letter="Bonjour,\nMaxime NOM",
                    screenshot=tmp_path / "1.png",
                    answers=ANSWERS,
                )
            finally:
                await browser.close()

    return asyncio.run(main()), posts


def testApplyFillsFormAndChecksMyApplications(tmp_path):
    result, posts = runApply(tmp_path, offerHtml=OFFER.replace("{extra}", ""))
    assert result.status == "applied", result.detail
    assert len(posts) == 1
    sent = posts[0]
    assert "Firstname=Maxime" in sent and "LastName=NOM" in sent and "maxime%40example.com" in sent
    assert "MotivationLetter=Bonjour" in sent and "HasAcceptedCGU=on" in sent
    assert (tmp_path / "1-etape1-avant.png").is_file()


def testApplyStopsOnUnknownRequiredField(tmp_path):
    extra = '<label for="q">Permis B ?</label><input id="q" name="Question" required>'
    result, posts = runApply(tmp_path, offerHtml=OFFER.replace("{extra}", extra))
    assert result.status == "needs_review" and "Question" in result.detail
    assert posts == []  # rien n'est soumis tant qu'un champ obligatoire reste vide


def testApplyRejectsSchoolQuestion(tmp_path):
    extra = (
        '<label for="q">Souhaitez-vous rejoindre le CFA ?</label>'
        '<select id="q" name="Cfa" required><option value="">Choisir</option><option value="1">Oui</option></select>'
    )
    result, posts = runApply(tmp_path, offerHtml=OFFER.replace("{extra}", extra))
    assert result.status == "rejected" and "CFA" in result.detail
    assert posts == []


def testApplySkipsExternalOffer(tmp_path):
    external = (
        "<html><body><a data-cy='applyButtonHeader'>Postuler sur le site du recruteur</a></body></html>"
    )
    result, posts = runApply(tmp_path, offerHtml=external)
    assert result.status == "external"
    assert posts == []
