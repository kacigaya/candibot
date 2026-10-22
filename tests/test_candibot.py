import dataclasses
import io
import json
import os
import urllib.error
from datetime import UTC, datetime

import pytest

from candibot import application, dashboard, hellowork, llm, mailer
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


def testDashboardEscapesOfferTextAndSkipsExcludedScores(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    # Le texte vient d'Internet : il ne doit jamais devenir du HTML actif.
    store.add(dataclasses.replace(hellowork.parseOffer("1", offerPage()), title="<script>alert(1)</script>"))
    store.add(hellowork.parseOffer("2", offerPage()))
    store.scored("1", 9, "bon profil", "Bonjour", minScore=7)
    store.setStatus("1", "applied")
    store.scored("2", 0, EXCLUDED_REASON, "", minScore=7)
    store.recordRun("2026-10-01T07:30:00+00:00", "aucune offre trouvée")
    page = dashboard.render(store.offers(), store.runs(10), datetime.now(UTC))
    store.close()
    assert "<script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "écartées, dont 1 écoles" in page
    assert "<title>0 : 0</title>" not in page  # note 0 fictive des écoles exclues
    assert "<title>9 : 1</title>" in page
    assert "✗ échec</span> : aucune offre trouvée" in page
    assert "Passages arrêtés ?" in page  # dernier passage le 01/10, page générée aujourd'hui
    # 07:30 UTC le 1er octobre = 09:30 à Paris (heure d'été).
    assert "01/10/2026 09:30" in page


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
    assert application.isListed(cards, "Alternant Production de Contenus H/F", "MGEN")
    assert not application.isListed(cards, "Alternant Production de Contenus H/F", "Egis Groupe")
    assert not application.isListed(cards, "Community Manager H/F", "MGEN")
    assert application.isListed(cards, "Chargé de Communication H/F", "Entreprise anonyme")


def testIsListedToleratesCompanyNameVariants():
    # Relevé le 22/10/2026 : la liste ne reprend pas toujours le nom de la fiche.
    cards = [
        "Envoyée\nAlternant Assistant Marketing Digital H/F\n\nSARL LE LU\n\nParis - 75",
        "Envoyée\nAlternant Chargé de Communication H/F\n\nOnet Airport Services\n\nRoissy-en-France - 95",
    ]
    assert application.isListed(cards, "Alternant Assistant Marketing Digital H/F", "Lelu France")
    assert application.isListed(cards, "Alternant Chargé de Communication H/F", "Groupe ONET")
    # Les mots courants ne suffisent pas : « Groupe » ou « France » seuls ne désignent personne.
    assert not application.isListed(cards, "Alternant Chargé de Communication H/F", "Groupe Paris France")
    # Un mot du titre ne vaut pas nom d'entreprise.
    analyst = ["Envoyée\nBusiness Analyst H/F\n\nContoso\n\nOrange - 84"]
    assert not application.isListed(analyst, "Business Analyst H/F", "Orange Business")
    # Le lieu ne vaut pas nom d'entreprise, et un titre plus long n'est pas le même poste.
    assert not application.isListed(analyst, "Analyst H/F", "Contoso")
    # Un seul mot en commun ne fait pas la même entreprise.
    decision = ["Envoyée\nBusiness Analyst H/F\n\nBusiness & Decision\n\nLyon - 69"]
    assert not application.isListed(decision, "Business Analyst H/F", "Orange Business")
    # Un sigle court doit être identique : « AI » n'est pas « Mistral AI ».
    short = ["Envoyée\nData Scientist H/F\n\nMistral AI\n\nParis - 75"]
    assert not application.isListed(short, "Data Scientist H/F", "AI")
    assert application.isListed(
        ["Envoyée\nData Scientist H/F\n\nAI\n\nParis - 75"], "Data Scientist H/F", "AI"
    )


def testClaimTakesADraftOnlyOnce(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add(hellowork.parseOffer("1", offerPage()))
    store.scored("1", 9, "bon profil", "Bonjour", minScore=7)
    assert store.claim("1", "envoi en cours") == "needs_review"
    assert store.claim("1", "envoi en cours") is None  # un second passage ne la renvoie pas
    assert store.counts() == {"needs_review": 1}
    store.close()


def testClaimRejectsACopyClaimedByAnotherRun(tmp_path):
    first, second = Store(tmp_path / "db.sqlite"), Store(tmp_path / "db.sqlite")
    for offerId in ("1", "2"):  # la même annonce sous deux identifiants
        first.add(hellowork.parseOffer(offerId, offerPage()))
        first.scored(offerId, 9, "bon profil", "Bonjour", minScore=7)
    assert first.claim("1", "envoi en cours") == "needs_review"
    assert second.claim("2", "envoi en cours") == "rejected"  # autre connexion, autre passage
    assert second.counts() == {"needs_review": 1, "rejected": 1}
    first.close()
    second.close()


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
    # Relevé sur une annonce « Centre de formation » : la question vient après le cursus.
    assert answer("Quel campus souhaitez-vous intégrer ?", yesNo, answers()) == SCHOOL
    assert answer("Quelle formation souhaitez-vous suivre ?", yesNo, answers()) == SCHOOL
    assert answer("Quel poste souhaitez-vous intégrer ?", yesNo, answers()) is None


def testPhoneAnswerComesFromConfigOnly():
    from candibot.questions import Answers, answer

    assert answer("Téléphone", None, Answers("", "", 0, "", "06 12 34 56 78")) == "06 12 34 56 78"
    assert answer("Téléphone", None, Answers("", "", 0, "")) is None
    withPhone = Answers("", "", 0, "", "06 12 34 56 78")
    assert answer("Numéro de portable", None, withPhone) == "06 12 34 56 78"
    assert answer("Êtes-vous mobile ?", None, withPhone) is None
    assert answer("Avez-vous un ordinateur portable ?", None, withPhone) is None


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


def testRunProblemFlagsEmptySearchAndUnconfirmedSends():
    from candibot.cli import runProblem

    assert runProblem(0, [], send=False) == "aucune offre trouvée dans les résultats de recherche"
    assert runProblem(30, [{"status": "drafted"}], send=False) is None
    failed = [{"status": "failed"}, {"status": "needs_review"}]
    assert runProblem(30, failed, send=True) == "2 envoi(s) tenté(s), aucun confirmé"
    assert runProblem(30, failed, send=False) is None  # rien n'est envoyé en simulation
    assert runProblem(30, [*failed, {"status": "applied"}], send=True) is None


def testAlertMailsOncePerFailedStreak(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from candibot import cli
    from candibot.config import Mail

    sent = []
    monkeypatch.setattr(cli.mailer, "send", lambda mail, subject, body, key: sent.append((subject, key)))
    cfg = SimpleNamespace(mail=Mail(inbox="bot@agentmail.to", to="moi@example.com"))
    store = Store(tmp_path / "db.sqlite")
    cli.alert(cfg, store, "2026-10-01T07:30:00+00:00", None)
    for day in range(2, 7):
        cli.alert(cfg, store, f"2026-10-0{day}T07:30:00+00:00", "aucune offre trouvée")
    assert store.failedStreak() == 5
    assert sent == [("candibot : 3 passages en échec", "alert-2026-10-04T07:30:00+00:00")]
    cli.alert(cfg, store, "2026-10-07T07:30:00+00:00", None)
    assert store.failedStreak() == 0
    store.close()


def testReviewKeepsEditsAndRejectsLetters(tmp_path, capsys):
    from candibot import cli

    store = Store(tmp_path / "db.sqlite")
    for offerId, score in (("1", 10), ("2", 9), ("3", 8), ("4", 7)):  # relues par note décroissante
        store.add(hellowork.parseOffer(offerId, offerPage()))
        store.scored(offerId, score, "proche du CV", f"Lettre {offerId}", minScore=7)
    # 1 : modifiée après un premier essai vide ; 2 : refusée ; 3 : gardée ; 4 : jamais vue (quitter).
    answers = iter(["m", "m", "x", "r", "g", "q"])
    edits = iter(["", "Lettre 1 corrigée"])
    cli.review(store, ask=lambda _: next(answers), edit=lambda _: next(edits))
    rows = {r["id"]: r for r in store.db.execute("SELECT * FROM offers")}
    assert rows["1"]["status"] == "drafted" and rows["1"]["letter"] == "Lettre 1 corrigée"
    assert rows["2"]["status"] == "rejected" and rows["2"]["detail"] == cli.REVIEW_REJECTED
    assert rows["3"]["letter"] == "Lettre 3" and rows["4"]["status"] == "drafted"
    assert "modification ignorée" in capsys.readouterr().out
    store.close()


def testEditTextUsesEditorFromEnvironment(monkeypatch):
    from pathlib import Path

    from candibot import cli

    calls = []

    def fakeRun(cmd, check):
        calls.append(cmd[0])
        Path(cmd[1]).write_text(Path(cmd[1]).read_text(encoding="utf-8") + " modifié\n", encoding="utf-8")

    monkeypatch.setenv("EDITOR", "vim")
    monkeypatch.setattr(cli.subprocess, "run", fakeRun)
    assert cli.editText("Bonjour") == "Bonjour modifié"
    assert calls == ["vim"]


def testDuplicateOfMatchesSameTitleCompanyAndPlace(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    for offerId in ("1", "2", "3"):
        store.add(hellowork.parseOffer(offerId, offerPage()))
    store.setStatus("1", "applied")
    store.setStatus("3", "rejected")
    assert store.duplicateOf("2", ("applied", "needs_review")) == "1"
    assert store.duplicateOf("1", ("applied", "needs_review")) is None  # jamais soi-même
    assert store.duplicateOf("2", ("drafted",)) is None
    other = dataclasses.replace(hellowork.parseOffer("4", offerPage()), location="Lyon 69001")
    store.add(other)
    assert store.duplicateOf("4", ("applied",)) is None
    # Même intitulé au même endroit, mais un autre poste : le texte les départage.
    distinct = dataclasses.replace(
        hellowork.parseOffer("5", offerPage()), description="Poste en entrepôt, horaires de nuit."
    )
    store.add(distinct)
    assert store.duplicateOf("5", ("applied",)) is None
    store.close()


def testSendSkipsCopiesOfAnOfferAlreadySent(tmp_path, monkeypatch):
    import asyncio

    from candibot import cli

    store = Store(tmp_path / "db.sqlite")
    for offerId in ("1", "2"):
        store.add(hellowork.parseOffer(offerId, offerPage()))
        store.scored(offerId, 9, "bon profil", "Bonjour", minScore=7)
    sent = []

    async def fakeApply(session, page, url, **kwargs):
        sent.append(url)
        return application.ApplyResult("applied", "vérifiée")

    monkeypatch.setattr(cli.application, "apply", fakeApply)
    monkeypatch.setattr(cli, "notify", lambda *args: None)
    cfg = type("Cfg", (), {})()
    cfg.dataDir = tmp_path
    # Une seule candidature permise : le doublon écarté ne doit pas la consommer.
    store.add(dataclasses.replace(hellowork.parseOffer("3", offerPage()), title="Autre poste H/F"))
    store.scored("3", 8, "bon profil", "Bonjour", minScore=7)
    cfg.search = Search([], "", ("CDI",), 1, 7, 1, 1, (0, 0), ())
    cfg.candidate = type("C", (), {"firstName": "", "lastName": "", "email": "", "cvPdf": None})()
    cfg.answers = None
    store.setStatus("1", "applied")  # déjà envoyée lors d'un passage précédent
    asyncio.run(cli.sendApplications(None, None, cfg, store))
    assert sent == [hellowork.offerUrl("3")]
    statuses = store.counts()
    assert statuses == {"applied": 2, "rejected": 1}
    duplicate = [r for r in store.byStatus("rejected")][0]
    assert duplicate["detail"].startswith("doublon de l'offre ")
    store.close()


def testLastRunLineWarnsWhenTimerLooksStopped():
    from candibot import cli

    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    assert cli.lastRunLine(None, now) == "dernier passage : aucun enregistré"
    assert "vérifiez le timer" not in cli.lastRunLine("2026-10-22T07:30:00+00:00", now)
    assert "vérifiez le timer" in cli.lastRunLine("2026-10-12T07:30:00+00:00", now)


def testRetryRequeuesOnlyFailedOrUnconfirmedOffers(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    for offerId in ("1", "2", "3"):
        store.add(hellowork.parseOffer(offerId, offerPage()))
    store.scored("1", 9, "ok", "Lettre", minScore=7)
    store.setStatus("1", "needs_review", "étape 2 : champs obligatoires vides")
    store.setStatus("2", "failed", "timeout")  # jamais notée : pas de lettre
    store.scored("3", 9, "ok", "Lettre", minScore=7)
    store.setStatus("3", "applied", "vérifiée")
    assert store.retry("1") == "drafted"
    assert store.retry("2") == "new"
    assert store.retry("3") is None and store.retry("404") is None
    assert store.counts() == {"drafted": 1, "new": 1, "applied": 1}
    store.close()


def testEmptyAnswersLeaveQuestionsUnanswered():
    from candibot.questions import Answers, answer

    empty = Answers("", "", 0, "")
    civ = [("", "Choisir"), ("1", "Madame"), ("2", "Monsieur")]
    when = [("", "Choisir"), ("a", "Immédiate"), ("b", "Dans 1 mois")]
    pay = [("", "Choisir"), ("a", "Moins de 1 500 €"), ("b", "1 500 € - 2 000 €")]
    assert answer("Civilité *", civ, empty) is None
    assert answer("Disponibilité *", when, empty) is None
    assert answer("Salaire souhaité *", pay, empty) is None
    assert answer("Prétentions salariales *", None, empty) is None
    assert answer("Votre disponibilité *", None, empty) is None


def testStoreAddsNewColumnsToAnOldDatabase(tmp_path):
    import sqlite3

    path = tmp_path / "db.sqlite"
    old = sqlite3.connect(path)  # schéma d'avant les colonnes query et outcome
    old.execute(
        "CREATE TABLE offers (id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,"
        " company TEXT NOT NULL, location TEXT NOT NULL, description TEXT NOT NULL,"
        " on_site INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'new', score INTEGER, reason TEXT,"
        " letter TEXT, detail TEXT, seen_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    old.execute("INSERT INTO offers VALUES ('9','u','t','c','l','d',1,'applied',8,'','','','x','x')")
    old.commit()
    old.close()
    store = Store(path)
    assert store.offers()[0]["query"] is None and store.offers()[0]["outcome"] is None
    store.add(hellowork.parseOffer("1", offerPage()), "community manager")
    assert [r["query"] for r in store.offers() if r["id"] == "1"] == ["community manager"]
    store.close()


def testExternalOffersAreScoredButStayManual(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    for day, offerId in enumerate(("1", "2", "3"), 1):
        store.add(hellowork.parseOffer(offerId, offerPage(external=True)))
        store.db.execute(
            "UPDATE offers SET seen_at = ? WHERE id = ?", (f"2026-10-0{day}T08:00:00+00:00", offerId)
        )
    assert [r["id"] for r in store.unscoredExternal()][:2] == ["3", "2"]  # les plus récentes d'abord
    store.scored("1", 8, "bon profil", "Bonjour", minScore=7, passing="external")
    store.scored("2", 3, "hors profil", "", minScore=7, passing="external")
    assert store.counts() == {"external": 2, "rejected": 1}
    assert [r["id"] for r in store.unscoredExternal()] == ["3"]
    store.close()


def testScoreSpendsLeftoverBudgetOnExternalOffers(tmp_path, monkeypatch):
    from candibot import cli

    store = Store(tmp_path / "db.sqlite")
    store.add(hellowork.parseOffer("1", offerPage()))
    for offerId in ("2", "3"):  # deux offres externes distinctes
        offer = hellowork.parseOffer(offerId, offerPage(external=True))
        store.add(dataclasses.replace(offer, title=f"Poste {offerId} H/F"))
    monkeypatch.setattr(cli.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(cli.llm, "evaluate", lambda *a, **k: llm.Evaluation(9, "bon profil", "Bonjour"))
    cfg = type("Cfg", (), {})()
    cfg.search = Search([], "", ("CDI",), 1, 7, 2, 10, (0, 0), ())  # max_scored = 2
    cfg.candidate = cfg.models = None
    cli.score(cfg, store)
    assert store.counts() == {"drafted": 1, "external": 2}
    assert len(store.unscoredExternal()) == 1  # 1 offre notée + 1 externe = budget de 2
    # Un doublon écarté sans appel au modèle laisse son budget aux offres externes.
    store.add(hellowork.parseOffer("4", offerPage()))
    cli.score(cfg, store)
    assert store.counts() == {"drafted": 1, "external": 2, "rejected": 1}
    assert store.unscoredExternal() == []
    store.close()


def testExternalCopiesAreScoredOnce(tmp_path, monkeypatch):
    from candibot import cli

    store = Store(tmp_path / "db.sqlite")
    for offerId in ("1", "2"):  # la même annonce externe sous deux identifiants
        store.add(hellowork.parseOffer(offerId, offerPage(external=True)))
    calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        cli.llm, "evaluate", lambda *a, **k: calls.append(1) or llm.Evaluation(9, "bon profil", "Bonjour")
    )
    cfg = type("Cfg", (), {})()
    cfg.search = Search([], "", ("CDI",), 1, 7, 1, 10, (0, 0), ())  # une seule notation permise
    cfg.candidate = cfg.models = None
    cli.score(cfg, store)
    assert len(calls) == 1  # la copie écartée n'a pas pris la seule place
    assert store.counts() == {"external": 1, "rejected": 1}
    store.close()


def testOutcomeAndFollowUps(tmp_path):
    from datetime import timedelta

    from candibot.store import FOLLOW_UP_DAYS

    store = Store(tmp_path / "db.sqlite")
    for offerId in ("1", "2", "3"):
        store.add(hellowork.parseOffer(offerId, offerPage()))
        store.setStatus(offerId, "applied")
    later = datetime.now(UTC) + timedelta(days=FOLLOW_UP_DAYS)
    assert store.followUps(datetime.now(UTC)) == []
    assert store.setOutcome("1", "entretien")
    assert not store.setOutcome("404", "refus")
    store.add(hellowork.parseOffer("4", offerPage()))
    assert not store.setOutcome("4", "refus")  # pas envoyée
    with pytest.raises(ValueError):
        store.setOutcome("2", "peut-être")
    assert [r["id"] for r in store.followUps(later)] == ["2", "3"]
    body = mailer.render([], sent=True, followUps=store.followUps(later))
    assert "À relancer" in body and "id 2" in body and "candibot outcome" in body
    page = dashboard.render(store.offers(), [], later)
    assert "Entretien" in page and "<b>1</b><span>entretiens ou acceptations" in page
    store.close()


def testRecapShowsLetterForManualOffers(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add(hellowork.parseOffer("1", offerPage(external=True)))
    store.scored("1", 8, "bon profil", "Bonjour,\nMaxime", minScore=7, passing="external")
    body = mailer.render(store.since("2000-01-01"), sent=False)
    assert "À faire à la main" in body and "[8/10]" in body and "    Bonjour," in body
    page = dashboard.render(store.offers(), [], datetime.now(UTC))
    assert '<summary>Lettre</summary><p class="letter">Bonjour,\nMaxime</p>' in page
    store.close()


def testDashboardGroupsOffersByQuery(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add(hellowork.parseOffer("1", offerPage()), "community manager")
    store.scored("1", 8, "bon profil", "Bonjour", minScore=7)
    store.setStatus("1", "applied")
    store.add(hellowork.parseOffer("2", offerPage()), "community manager")
    store.scored("2", 3, "hors profil", "", minScore=7)
    store.add(hellowork.parseOffer("3", offerPage()))
    page = dashboard.render(store.offers(), [], datetime.now(UTC))
    assert (
        '<td>community manager</td><td class="num">2</td><td class="num">5,5</td><td class="num">1</td>'
        in page
    )
    assert dashboard.NO_QUERY in page
    store.close()


def testDuplicatesDoNotUseTheBudgetOfNewOffers(tmp_path, monkeypatch):
    from candibot import cli

    store = Store(tmp_path / "db.sqlite")
    store.add(hellowork.parseOffer("1", offerPage()))
    store.setStatus("1", "applied")
    store.add(hellowork.parseOffer("2", offerPage()))  # copie de l'offre 1, vue en premier
    store.db.execute("UPDATE offers SET seen_at = '2026-01-01T00:00:00+00:00' WHERE id = '2'")
    store.add(dataclasses.replace(hellowork.parseOffer("3", offerPage()), title="Autre poste H/F"))
    monkeypatch.setattr(cli.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(cli.llm, "evaluate", lambda *a, **k: llm.Evaluation(9, "bon profil", "Bonjour"))
    cfg = type("Cfg", (), {})()
    cfg.search = Search([], "", ("CDI",), 1, 7, 1, 10, (0, 0), ())  # une seule notation permise
    cfg.candidate = cfg.models = None
    cli.score(cfg, store)
    assert store.counts() == {"applied": 1, "rejected": 1, "drafted": 1}
    store.close()
