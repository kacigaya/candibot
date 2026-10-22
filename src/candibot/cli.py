"""Bot de candidature sur HelloWork (CDI, CDD, alternance, stage…).

candibot run            cherche, note et rédige, sans rien envoyer
candibot run --send     envoie aussi les candidatures prêtes
candibot status         compte les offres par statut
candibot check          vérifie les sélecteurs HelloWork, sans rien envoyer
candibot review         relit les lettres prêtes : garder, modifier ou refuser
candibot retry ID…      remet des offres failed ou needs_review dans la file d'envoi
"""

import argparse
import asyncio
import hashlib
import os
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from webskrap import SessionConfig, WebSkrapClient, WebSkrapSession

from candibot import hellowork, llm, mailer
from candibot.config import Config, load
from candibot.store import EXCLUDED_REASON, Store


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)


MAX_CONSECUTIVE_ERRORS = 5  # au-delà, HelloWork bloque ou est en panne : inutile d'insister


async def collect(page, cfg: Config, store: Store) -> int:
    """Enregistre les nouvelles offres. Une page en erreur n'est pas stockée et sera retentée au passage suivant.

    Renvoie le nombre d'offres trouvées dans les résultats, nouvelles ou non.
    """
    errors = found = 0
    for query in cfg.search.queries:
        try:
            ids = await hellowork.search(
                page, query, cfg.search.location, cfg.search.contracts, cfg.search.pages
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            log(f"« {query} » : recherche impossible ({str(exc).splitlines()[0]})")
            if errors >= MAX_CONSECUTIVE_ERRORS:
                break
            continue
        errors = 0
        found += len(ids)
        fresh = [i for i in ids if not store.known(i)]
        log(f"« {query} » : {len(ids)} offres, {len(fresh)} nouvelles")
        for offerId in fresh:
            try:
                offer = await hellowork.fetchOffer(page, offerId)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log(f"  {offerId} : fiche inaccessible ({str(exc).splitlines()[0]})")
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    log(f"{errors} erreurs d'affilée : collecte interrompue")
                    return found
                continue
            errors = 0
            if offer is None:
                log(f"  {offerId} : fiche illisible (expirée ou balisage changé), ignorée")
                continue
            store.add(offer)
            await asyncio.sleep(random.uniform(1, 3))
    return found


def score(cfg: Config, store: Store) -> None:
    for tool in ("opencode", "bwrap"):
        if shutil.which(tool) is None:
            raise SystemExit(f"`{tool}` introuvable dans le PATH (ajouter ~/.local/bin pour cron)")
    # Les offres exclues ne coûtent pas d'appel au modèle : on les écarte avant la limite max_scored.
    # Les lettres prêtes sont revérifiées aussi, pour qu'un nom ajouté à la liste prenne effet
    # avant le prochain envoi.
    for row in [*store.byStatus("new"), *store.byStatus("drafted")]:
        if cfg.search.excluded(row["company"]):
            store.scored(row["id"], 0, EXCLUDED_REASON, "", cfg.search.minScore)
    rows = store.byStatus("new", cfg.search.maxScored)
    log(f"notation de {len(rows)} offres")
    for row in rows:
        try:
            result = llm.evaluate(
                cfg.candidate,
                cfg.search,
                cfg.models,
                title=row["title"],
                company=row["company"],
                location=row["location"],
                description=row["description"],
            )
        except Exception as exc:  # noqa: BLE001 - l'offre reste "new" et sera retentée au prochain passage
            log(f"  {row['id']} : notation impossible ({exc})")
            continue
        store.scored(row["id"], result.score, result.reason, result.letter, cfg.search.minScore)
        log(f"  {result.score}/10 {row['title']} · {row['company']}")


def codeWaiter(path: Path, timeoutS: int = 900):
    """Fournisseur de code HelloWork : attend que les 6 chiffres soient écrits dans `path`."""

    async def wait() -> str | None:
        path.unlink(missing_ok=True)  # jamais un vieux code d'une tentative précédente
        log(f"HelloWork demande le code envoyé par email : écrire les 6 chiffres dans {path}")
        for _ in range(timeoutS // 3):
            if path.is_file():
                code = path.read_text(encoding="utf-8").strip()
                path.unlink()
                log("code reçu, saisie")
                return code
            await asyncio.sleep(3)
        log("aucun code reçu à temps")
        return None

    return wait


async def sendApplications(session: WebSkrapSession, page, cfg: Config, store: Store, otp=None) -> None:
    rows = store.byStatus("drafted", cfg.search.maxApplications)
    shots = cfg.dataDir / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    c = cfg.candidate
    for n, row in enumerate(rows):
        if n:
            await asyncio.sleep(random.uniform(*cfg.search.pauseSeconds))
        # Marqué avant l'envoi : si le processus meurt en cours de route, l'offre
        # n'est pas renvoyée au passage suivant (pas de candidature en double).
        store.setStatus(row["id"], "needs_review", "envoi interrompu avant confirmation")
        try:
            result = await hellowork.apply(
                session,
                page,
                row["url"],
                title=row["title"],
                company=row["company"],
                firstName=c.firstName,
                lastName=c.lastName,
                email=c.email,
                cvPdf=c.cvPdf,
                letter=row["letter"],
                screenshot=shots / f"{row['id']}.png",
                answers=cfg.answers,
                otp=otp,
            )
            store.setStatus(row["id"], result.status, result.detail)
            log(f"  {result.status} : {row['title']} · {row['company']} ({result.detail})")
        except Exception as exc:  # noqa: BLE001 - une offre cassée ne doit pas arrêter les suivantes
            store.setStatus(row["id"], "failed", str(exc).splitlines()[0][:300])
            log(f"  échec : {row['title']} · {row['company']} ({exc})")
            continue  # "failed" : rien n'est parti, pas de confirmation
        if result.status in ("applied", "needs_review"):  # sinon rien n'est parti
            notify(cfg, row, result.status, result.detail)


ALERT_AFTER = 3  # passages en échec d'affilée avant l'email d'alerte


def runProblem(found: int, rows: list, send: bool) -> str | None:
    """Raison pour laquelle un passage a échoué, ou None s'il s'est bien passé.

    Aucune offre trouvée signale une recherche cassée (site changé, IP bloquée).
    Avec --send, des envois tentés sans aucun `applied` signalent un formulaire
    cassé ou une session expirée.
    """
    if not found:
        return "aucune offre trouvée dans les résultats de recherche"
    attempted = [r for r in rows if r["status"] in ("applied", "needs_review", "failed")]
    if send and attempted and not any(r["status"] == "applied" for r in attempted):
        return f"{len(attempted)} envoi(s) tenté(s), aucun confirmé"
    return None


def alert(cfg: Config, store: Store, started: str, problem: str | None) -> None:
    """Enregistre l'issue du passage et prévient par email au 3ᵉ échec d'affilée, une fois par série."""
    store.recordRun(started, problem)
    if not problem:
        return
    streak = store.failedStreak()
    log(f"passage en échec ({problem}), {streak} d'affilée")
    if streak != ALERT_AFTER or not cfg.mail.enabled:
        return
    body = (
        f"Les {streak} derniers passages de candibot ont échoué.\n"
        f"Dernière cause : {problem}\n\n"
        "Lancez `uv run candibot check` pour vérifier les sélecteurs HelloWork, "
        "et consultez data/logs/ et data/shots/."
    )
    try:
        mailer.send(cfg.mail, f"candibot : {streak} passages en échec", body, f"alert-{started}")
    except Exception as exc:  # noqa: BLE001 - l'alerte ne doit pas masquer l'erreur du passage
        log(f"alerte email impossible ({exc})")


def notify(cfg: Config, row, status: str, detail: str) -> None:
    """Confirmation par email à la candidate. Un échec d'email n'annule pas la candidature."""
    if not cfg.mail.enabled:
        return
    subject, body = mailer.confirmation(row, status, detail)
    try:
        # Clé dérivée du contenu : un nouvel essai du même email n'envoie rien de plus, et un
        # email différent pour la même offre (nouveau statut, nouveau détail) part normalement.
        digest = hashlib.sha256(f"{subject}\n{body}".encode()).hexdigest()[:16]
        mailer.send(cfg.mail, subject, body, f"confirm-{row['id']}-{digest}")
    except Exception as exc:  # noqa: BLE001
        log(f"  confirmation email impossible ({exc})")


def browserConfig(cfg: Config) -> SessionConfig:
    return SessionConfig(
        driver="patchright",
        channel="chromium",  # le canal "chrome" n'existe pas sur Linux ARM64
        headless=True,
        # Profil persistant : cookies et compte HelloWork conservés d'un passage à l'autre.
        user_data_dir=cfg.dataDir / "browser",
    )


async def openPage(session: WebSkrapSession):
    return session.context.pages[0] if session.context.pages else await session.context.new_page()


async def check(cfg: Config) -> bool:
    """Vérifie les sélecteurs HelloWork sur la première requête. Vrai si tout est trouvé."""
    async with WebSkrapClient(default_config=browserConfig(cfg)) as client:
        page = await openPage(await client.session("hellowork"))
        problems = await hellowork.check(
            page, cfg.search.queries[0], cfg.search.location, cfg.search.contracts
        )
    for problem in problems:
        log(f"problème : {problem}")
    if not problems:
        log("sélecteurs HelloWork OK")
    return not problems


async def run(cfg: Config, send: bool, waitCode: bool = False) -> None:
    started = datetime.now(UTC).isoformat(timespec="seconds")
    store = Store(cfg.dataDir / "candibot.db")
    problem: str | None = "passage interrompu par une erreur"
    try:
        async with WebSkrapClient(default_config=browserConfig(cfg)) as client:
            session = await client.session("hellowork")
            page = await openPage(session)
            await page.goto(hellowork.BASE + "/fr-fr/", wait_until="domcontentloaded")
            await hellowork.dismissCookies(page)
            found = await collect(page, cfg, store)
            score(cfg, store)
            if send:
                otp = codeWaiter(cfg.dataDir / "otp.txt") if waitCode else None
                await sendApplications(session, page, cfg, store, otp)

        body = mailer.render(store.since(started), send)
        print(body)
        if cfg.mail.enabled:
            applied = sum(1 for r in store.since(started) if r["status"] == "applied")
            # Clé dérivée du début du passage : un nouvel essai du même passage n'envoie rien de plus.
            key = "recap-" + started.replace(":", "").replace("+", "")
            mailer.send(cfg.mail, f"candibot : {applied} candidature(s) envoyée(s)", body, key)
            log(f"récapitulatif envoyé à {cfg.mail.to}")
        problem = runProblem(found, store.since(started), send)
    finally:
        alert(cfg, store, started, problem)
        store.close()


REVIEW_REJECTED = "lettre refusée à la relecture"


def editText(text: str) -> str:
    """Ouvre `text` dans l'éditeur de $EDITOR (nano par défaut) et renvoie le texte enregistré."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as file:
        file.write(text)
    path = Path(file.name)
    try:
        subprocess.run([os.environ.get("EDITOR", "nano"), str(path)], check=True)
        return path.read_text(encoding="utf-8").strip()
    finally:
        path.unlink(missing_ok=True)


def review(store: Store, ask=input, edit=editText) -> None:
    """Relit les lettres prêtes une par une, avant `run --send`."""
    rows = store.byStatus("drafted")
    if not rows:
        print("aucune lettre prête")
        return
    for n, row in enumerate(rows, 1):
        print(f"\n[{n}/{len(rows)}] {row['score']}/10 {row['title']} · {row['company']} ({row['location']})")
        print(f"{row['url']}\n{row['reason']}\n\n{row['letter']}\n")
        while True:
            choice = ask("[g]arder, [m]odifier, [r]efuser, [q]uitter ? ").strip().lower()[:1]
            if choice == "m":
                letter = edit(row["letter"])
                if letter:
                    store.setLetter(row["id"], letter)
                    print("lettre modifiée")
                    break
                print("lettre vide : modification ignorée")
            elif choice == "r":
                store.setStatus(row["id"], "rejected", REVIEW_REJECTED)
                print("offre écartée")
                break
            elif choice == "g":
                break
            elif choice == "q":
                return


def retry(store: Store, ids: list[str]) -> None:
    for offerId in ids:
        status = store.retry(offerId)
        print(f"{offerId} : {status}" if status else f"{offerId} : introuvable, ou ni failed ni needs_review")
    # « Mes candidatures » est relu avant chaque envoi : une offre déjà partie n'est pas renvoyée.
    print("Vérifiez votre espace HelloWork avant `run --send` pour les offres needs_review.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="candibot", description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    sub = parser.add_subparsers(dest="command", required=True)
    runP = sub.add_parser("run", help="chercher, noter, rédiger (et envoyer avec --send)")
    runP.add_argument("--send", action="store_true", help="envoyer réellement les candidatures prêtes")
    runP.add_argument(
        "--wait-code",
        dest="waitCode",
        action="store_true",
        help="si HelloWork demande le code reçu par email, l'attendre dans data/otp.txt (15 min)",
    )
    sub.add_parser("status", help="nombre d'offres par statut")
    sub.add_parser("check", help="vérifier les sélecteurs HelloWork, sans rien envoyer")
    sub.add_parser("review", help="relire les lettres prêtes : garder, modifier ou refuser")
    retryP = sub.add_parser("retry", help="remettre des offres failed ou needs_review dans la file d'envoi")
    retryP.add_argument("ids", nargs="+", metavar="ID", help="identifiant HelloWork de l'offre")
    args = parser.parse_args()

    cfg = load(args.config)
    if args.command in ("status", "review", "retry"):
        store = Store(cfg.dataDir / "candibot.db")
        try:
            if args.command == "review":
                review(store)
            elif args.command == "retry":
                retry(store, args.ids)
            else:
                for status, count in sorted(store.counts().items()):
                    print(f"{status:13} {count}")
        except (KeyboardInterrupt, EOFError):
            sys.exit(130)
        finally:
            store.close()
        return
    try:
        if args.command == "check":
            sys.exit(0 if asyncio.run(check(cfg)) else 1)
        asyncio.run(run(cfg, args.send, args.waitCode))
    except KeyboardInterrupt:
        sys.exit(130)
