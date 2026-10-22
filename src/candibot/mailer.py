import json
import os
import time
import urllib.error
import urllib.request
from sqlite3 import Row
from urllib.parse import quote

from candibot.config import Mail
from candibot.store import EXCLUDED_REASON

SECTIONS = [
    ("applied", "Candidatures envoyées"),
    ("needs_review", "À vérifier (candidature non confirmée)"),
    ("failed", "Échecs techniques"),
    ("drafted", "Lettres prêtes, pas encore envoyées"),
    ("external", "À faire à la main (site du recruteur)"),
    ("rejected", "Écartées"),
]


def render(rows: list[Row], sent: bool) -> str:
    mode = "envoi réel" if sent else "simulation, rien n'a été envoyé"
    lines = [f"Passage du bot de candidature ({mode}).", ""]
    excluded = [r for r in rows if r["reason"] == EXCLUDED_REASON]
    rows = [r for r in rows if r["reason"] != EXCLUDED_REASON]
    for status, label in SECTIONS:
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        lines.append(f"== {label} ({len(group)}) ==")
        for r in group:
            score = f"[{r['score']}/10] " if r["score"] is not None else ""
            lines.append(f"- {score}{r['title']} · {r['company']} · {r['location']}")
            lines.append(f"  {r['url']}")
            if status != "external" and r["reason"]:
                lines.append(f"  {r['reason']}")
            if r["detail"]:
                lines.append(f"  -> {r['detail']}")
            if status in ("applied", "drafted") and r["letter"]:
                lines.extend("    " + line for line in r["letter"].splitlines())
        lines.append("")
    if excluded:
        lines.append(f"{len(excluded)} offre(s) d'organismes de formation exclus (search.exclude_companies).")
    if len(lines) == 2:
        lines.append("Aucune nouvelle offre.")
    return "\n".join(lines)


def confirmation(row: Row, status: str, detail: str) -> tuple[str, str]:
    """Sujet et corps de l'email envoyé à la candidate après une tentative de candidature."""
    offer = f"{row['title']} · {row['company']}"
    if status == "applied":
        subject = f"Candidature envoyée : {offer}"
        intro = "Ta candidature a été envoyée sur HelloWork."
    else:
        subject = f"Candidature à vérifier : {offer}"
        intro = (
            "Le bot n'a pas pu confirmer l'envoi de cette candidature. Vérifie dans ton espace "
            "HelloWork (Mes candidatures) avant de repostuler, pour éviter un doublon."
        )
    lines = [
        intro,
        "",
        f"Offre : {row['title']}",
        f"Entreprise : {row['company']}",
        f"Lieu : {row['location']}",
        f"Lien : {row['url']}",
    ]
    if row["score"] is not None:
        lines.append(f"Pertinence : {row['score']}/10. {row['reason'] or ''}".rstrip())
    if detail:
        lines.append(f"Détail : {detail}")
    if row["letter"]:
        lines += ["", "Message envoyé au recruteur :", "", row["letter"]]
    return subject, "\n".join(lines)


API = "https://api.agentmail.to/v0"


def send(mail: Mail, subject: str, body: str, idempotencyKey: str) -> str:
    """Envoie le récapitulatif via AgentMail et renvoie l'identifiant du message.

    `idempotencyKey` rend l'envoi rejouable sans doublon : AgentMail renvoie le
    message d'origine pour une clé déjà vue dans les dernières 24 h.
    """
    key = os.environ.get("AGENTMAIL_API_KEY", "")
    if not (mail.enabled and key):
        raise RuntimeError("AgentMail incomplet : mail.inbox, mail.to et AGENTMAIL_API_KEY sont requis")
    request = urllib.request.Request(
        f"{API}/inboxes/{quote(mail.inbox, safe='@.')}/messages/send",
        data=json.dumps({"to": [mail.to], "subject": subject, "text": body}).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotencyKey,
        },
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)["message_id"]
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                # Limite de débit : AgentMail indique l'attente dans Retry-After.
                time.sleep(float(exc.headers.get("Retry-After", "1")) * 2**attempt)
                continue
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"AgentMail {exc.code} : {detail}") from None
    raise AssertionError("unreachable")
