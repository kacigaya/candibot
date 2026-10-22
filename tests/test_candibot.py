import io
import json
import os
import urllib.error

import pytest

from candibot import hellowork, llm, mailer
from candibot.config import Search
from candibot.store import EXCLUDED_REASON, Store

POSTING = {
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Alternance Chargé de Communication H/F",
    "description": "<h2>Missions</h2><p>Rédiger des posts<br />Animer LinkedIn</p><ul><li>Canva</li></ul>",
    "hiringOrganization": {"@type": "Organization", "name": "ACME &amp; Co"},
    "jobLocation": [{"address": {"addressLocality": "Paris", "postalCode": "75011"}}],
}


def offerPage(posting: dict | None = POSTING, external: bool = False) -> str:
    blocks = ['<script type="application/ld+json">{"@type": "WebSite"}</script>']
    if posting is not None:
        blocks.append(f'<script type="application/ld+json">{json.dumps(posting)}</script>')
    button = "Postuler sur le site du recruteur" if external else "Postuler"
    return (
        f"<html><head>{''.join(blocks)}</head><body><a data-cy='applyButtonHeader'>{button}</a></body></html>"
    )


def testParseSearchKeepsOrderAndDedupes():
    page = "".join(f'<li data-id-storage-item-id="{i}">' for i in ("3", "1", "3", "2"))
    assert hellowork.parseSearch(page) == ["3", "1", "2"]


def testParseOfferReadsJobposting():
    offer = hellowork.parseOffer("42", offerPage())
    assert offer is not None
    assert offer.url == "https://www.hellowork.com/fr-fr/emplois/42.html"
    assert offer.company == "ACME & Co"
    assert offer.location == "Paris 75011"
    assert offer.description == "Missions\nRédiger des posts\nAnimer LinkedIn\n- Canva"
    assert offer.onSite


def testPartnerSiteOffersAreExternalToo():
    page = offerPage().replace("Postuler<", "Postuler sur le site du partenaire<")
    assert not hellowork.parseOffer("42", page).onSite


def testParseOfferFlagsExternalAndMissingPosting():
    external = hellowork.parseOffer("42", offerPage(external=True))
    assert external is not None and not external.onSite
    assert hellowork.parseOffer("42", offerPage(posting=None)) is None


def testParseOfferFallsBackToHtmlForAnonymousOffers():
    page = (
        "<html><head><title>Offre Alternance Paris (75) | Hellowork</title></head><body>"
        '<h1><span data-cy="jobTitle">Chargé de Communication H/F</span></h1>'
        "<h2>Les missions du poste</h2>\n   <p>Animer   les réseaux</p>\n\n\n\n<p>Postuler</p></body></html>"
    )
    offer = hellowork.parseOffer("7", page)
    assert offer is not None
    assert (offer.title, offer.company, offer.location) == (
        "Chargé de Communication H/F",
        "Entreprise anonyme",
        "département 75",
    )
    assert offer.description == "Les missions du poste\n\nAnimer   les réseaux\n\nPostuler"


def testStoreLifecycle(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    onSite = hellowork.parseOffer("1", offerPage())
    external = hellowork.parseOffer("2", offerPage(external=True))
    store.add(onSite)
    store.add(onSite)  # un doublon ne remplace pas l'existant
    store.add(external)
    assert store.counts() == {"new": 1, "external": 1}

    store.scored("1", 8, "bon profil", "Bonjour, ...", minScore=7)
    assert [r["id"] for r in store.byStatus("drafted")] == ["1"]
    store.setStatus("1", "applied", "confirmée à l'étape 1")
    assert store.counts() == {"applied": 1, "external": 1}
    with pytest.raises(ValueError):
        store.setStatus("1", "inconnu")
    store.close()


def testStoreRejectsLowScores(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add(hellowork.parseOffer("1", offerPage()))
    store.scored("1", 4, "école", "", minScore=7)
    assert store.counts() == {"rejected": 1}
    store.close()


def events(*texts: str, error: str | None = None) -> str:
    """Sortie de `opencode run --format json` : un événement JSON par ligne."""
    lines = [json.dumps({"type": "step_start", "part": {}})]
    lines += [json.dumps({"type": "text", "part": {"text": t}}) for t in texts]
    if error:
        lines.append(json.dumps({"type": "error", "error": {"data": {"message": error}}}))
    return "\n".join(lines)


def testParseOutputReadsJsonFromTextEvents():
    stdout = events('Voici :\n{"score": 8, "reason": " ok ",', ' "letter": " Bonjour "}')
    assert llm.parseOutput(stdout) == llm.Evaluation(8, "ok", "Bonjour")
    french = events(
        '{"score": 7, "reason": "ok", "letter": "Disponible dès l’été — « 3 semaines », 1 000 € œuvre…"}'
    )
    assert llm.parseOutput(french).score == 7


@pytest.mark.parametrize(
    "stdout",
    [
        events(error="OpenCode's free tier can only be used from within OpenCode"),
        events("texte libre sans JSON"),
        events('{"score": 12, "reason": "", "letter": ""}'),
        events('{"score": 8, "reason": "ok", "letter": "  "}'),
        events('{"score": 9, "reason": "ok", "letter": "Bonjour, pour 构建 la notoriété"}'),
    ],
)
def testParseOutputRejectsBadResults(stdout):
    with pytest.raises(RuntimeError):
        llm.parseOutput(stdout)


def testSandboxHidesHomeAndKeepsOnlyNetwork():
    from pathlib import Path

    cmd = llm.sandboxCommand(Path("/h/.opencode/bin/opencode"), Path("/h"), ["run", "x"])
    assert cmd[:4] == ["bwrap", "--ro-bind", "/", "/"]
    assert ["--tmpfs", "/h"] == cmd[4:6]  # $HOME vide : ni projet, ni .env, ni clés SSH
    assert ["--ro-bind", "/h/.opencode", "/h/.opencode"] == cmd[6:9]
    assert "--unshare-all" in cmd and "--share-net" in cmd
    assert cmd[cmd.index("--unsetenv") + 1] == "AGENTMAIL_API_KEY"
    assert cmd[-3:] == ["/h/.opencode/bin/opencode", "run", "x"]


def testRenderGroupsByStatus(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add(hellowork.parseOffer("1", offerPage()))
    store.add(hellowork.parseOffer("2", offerPage(external=True)))
    store.scored("1", 9, "très proche du CV", "Bonjour,\nMaxime", minScore=7)
    body = mailer.render(store.since("2000-01-01"), sent=False)
    assert "simulation" in body
    assert "Lettres prêtes" in body and "[9/10]" in body and "    Bonjour," in body
    assert "À faire à la main" in body
    store.close()


def testExclusionIsCaseInsensitiveSubstring():
    search = Search([], "", ("CDI",), 1, 7, 1, 1, (0, 0), ("iscod", "Studi CFA"))
    assert search.excluded("ISCOD") and search.excluded("studi cfa Paris")
    assert not search.excluded("MGEN")


def testRenderCountsExcludedOffersWithoutListingThem(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add(hellowork.parseOffer("1", offerPage()))
    store.scored("1", 0, EXCLUDED_REASON, "", minScore=7)
    body = mailer.render(store.since("2000-01-01"), sent=False)
    assert "1 offre(s) d'organismes de formation exclus" in body and "Écartées" not in body
    store.close()


def testLoadEnvNeverOverridesExistingVariables(tmp_path, monkeypatch):
    from candibot.config import loadEnv

    (tmp_path / ".env").write_text("# commentaire\nA_KEY='secret'\nB_KEY=deja\n\n", encoding="utf-8")
    monkeypatch.delenv("A_KEY", raising=False)
    monkeypatch.setenv("B_KEY", "shell")
    loadEnv(tmp_path / ".env")
    assert os.environ["A_KEY"] == "secret"
    assert os.environ["B_KEY"] == "shell"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def testSendPostsToAgentmailWithIdempotencyKey(monkeypatch):
    from candibot.config import Mail

    calls = []

    def fakeUrlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:  # premier essai : limite de débit
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many", {"Retry-After": "0"}, None)
        return FakeResponse(b'{"message_id": "m1", "thread_id": "t1"}')

    monkeypatch.setenv("AGENTMAIL_API_KEY", "am_test")
    monkeypatch.setattr(mailer.urllib.request, "urlopen", fakeUrlopen)
    monkeypatch.setattr(mailer.time, "sleep", lambda s: None)
    mail = Mail(inbox="bot@agentmail.to", to="moi@example.com")
    assert mailer.send(mail, "Sujet", "Corps", "recap-1") == "m1"

    request = calls[-1]
    assert len(calls) == 2
    assert request.full_url == "https://api.agentmail.to/v0/inboxes/bot@agentmail.to/messages/send"
    assert request.get_header("Idempotency-key") == "recap-1"
    assert request.get_header("Authorization") == "Bearer am_test"
    assert json.loads(request.data) == {"to": ["moi@example.com"], "subject": "Sujet", "text": "Corps"}


def testSendRefusesIncompleteConfig(monkeypatch):
    from candibot.config import Mail

    monkeypatch.delenv("AGENTMAIL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AGENTMAIL_API_KEY"):
        mailer.send(Mail(inbox="bot@agentmail.to", to="moi@example.com"), "s", "b", "k")


def testConfirmationDistinguishesSentAndUnconfirmed(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add(hellowork.parseOffer("1", offerPage()))
    store.scored("1", 9, "très proche du CV", "Bonjour,\nMaxime", minScore=7)
    row = store.byStatus("drafted")[0]
    subject, body = mailer.confirmation(row, "applied", "confirmée à l'étape 2")
    assert subject.startswith("Candidature envoyée : Alternance Chargé de Communication")
    assert "Pertinence : 9/10" in body and body.endswith("Bonjour,\nMaxime")
    subject, body = mailer.confirmation(row, "needs_review", "pas de message de confirmation reconnu")
    assert subject.startswith("Candidature à vérifier") and "avant de repostuler" in body
    store.close()


def testCodeWaiterReadsThenDeletesCodeFile(tmp_path, monkeypatch):
    import asyncio

    from candibot import cli

    path = tmp_path / "otp.txt"
    path.write_text("999999", encoding="utf-8")  # code périmé d'une tentative précédente

    async def fakeSleep(_):
        path.write_text(" 123456\n", encoding="utf-8")

    monkeypatch.setattr(cli.asyncio, "sleep", fakeSleep)
    assert asyncio.run(cli.codeWaiter(path, timeoutS=9)()) == "123456"
    assert not path.exists()


def testIsListedMatchesTitleAndCompanyOnTheSameCard():
    cards = [
        "En cours d'envoi\nAlternant  Production de Contenus H/F\n\nMGEN\nParis - 75\nEnvoyée le 26 septembre",
        "Envoyée\nChargé de Communication H/F\nEgis Groupe\nGuyancourt",
    ]
    assert hellowork.isListed(cards, "Alternant Production de Contenus H/F", "MGEN")
    assert not hellowork.isListed(cards, "Alternant Production de Contenus H/F", "Egis Groupe")
    assert not hellowork.isListed(cards, "Community Manager H/F", "MGEN")
    assert hellowork.isListed(cards, "Chargé de Communication H/F", "Entreprise anonyme")


def answers():
    from candibot.questions import Answers

    return Answers("Madame", "immédiate", 1867.02, "100 % du SMIC")


GMBA_SALARY = [
    ("", "Choisir une option"),
    ("0-15000", "inférieur à 15 000€"),
    ("15000-20000", "15 000€ - 20 000€"),
]
GMBA_SALARY += [("20000-25000", "20 000€ - 25 000€"), ("85000-100000", "supérieur à 85 000€")]


def testAnswersToGmbaQuestions():
    from candibot.questions import answer

    civ = [("", "Choisir une option"), ("1", "Madame"), ("2", "Monsieur")]
    dispo = [("", "Choisir"), ("AVAILABILITY_NOW", "immédiate"), ("AVAILABILITY_2WEEKS", "15 jours")]
    assert answer("Civilité", civ, answers()) == "1"
    assert answer("Disponibilité", dispo, answers()) == "AVAILABILITY_NOW"
    assert answer("Attentes salariales", GMBA_SALARY, answers()) == "20000-25000"  # 22 404 €/an


def testSalaryRangesCanBeMonthlyAndFreeTextGetsSentence():
    from candibot.questions import answer

    monthly = [("", "—"), ("a", "Moins de 1 500 €"), ("b", "1 500 € - 2 000 €"), ("c", "Plus de 2 000 €")]
    assert answer("Prétentions salariales (brut mensuel)", monthly, answers()) == "b"
    assert answer("Vos prétentions de rémunération ?", None, answers()) == "100 % du SMIC"


def testSchoolQuestionsAndUnknownQuestions():
    from candibot.questions import SCHOOL, answer

    yesNo = [("", "Choisir ma réponse"), ("1", "Oui"), ("2", "Non")]
    assert answer("Souhaitez-vous rejoindre le CFA ITIS *", yesNo, answers()) == SCHOOL
    assert answer("Voulez-vous intégrer notre formation Bachelor ?", yesNo, answers()) == SCHOOL
    assert answer("Avez-vous le permis B ?", yesNo, answers()) is None


def testSearchUrlRepeatsContractFilter():
    from urllib.parse import parse_qs, urlparse

    url = hellowork.searchUrl("marketing", "Paris", ("CDI", "Intérim"), 2)
    params = parse_qs(urlparse(url).query)
    assert params["c"] == ["CDI", "Travail_temp"]
    assert params["k"] == ["marketing"] and params["p"] == ["2"]


def writeConfig(tmp_path, contracts: str):
    (tmp_path / "cv.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "cv.md").write_text("CV", encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(
        '[candidate]\nfirst_name = "A"\nlast_name = "B"\nemail = "a@example.com"\n'
        'cv_pdf = "cv.pdf"\ncv_text = "cv.md"\n'
        f'[search]\nqueries = ["x"]\nlocation = "Paris"\n{contracts}\n',
        encoding="utf-8",
    )
    return path


def testConfigRequiresKnownContracts(tmp_path):
    from candibot.config import load

    assert load(writeConfig(tmp_path, 'contracts = ["CDI", "CDD"]')).search.contracts == ("CDI", "CDD")
    for bad in ("", "contracts = []", 'contracts = ["CDI", "Temps plein"]'):
        with pytest.raises(SystemExit, match="search.contracts"):
            load(writeConfig(tmp_path, bad))


def testPromptStatesSearchAndContracts():
    from pathlib import Path

    from candibot.config import Candidate

    candidate = Candidate("A", "B", "a@example.com", Path("cv.pdf"), "CV", "", "")
    search = Search(["data analyst"], "Lyon", ("CDI", "CDD"), 1, 7, 1, 1, (0, 0), ())
    prompt = llm.buildPrompt(candidate, search, "Titre", "ACME", "Lyon", "Description")
    assert "Métiers : data analyst\nZone : Lyon\nContrats : CDI, CDD" in prompt
