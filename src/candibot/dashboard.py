"""Tableau de bord : une page HTML statique tirée de la base, sans JavaScript ni serveur.

La page contient des données personnelles (offres, candidatures) : elle reste dans `data/`.
"""

from collections import Counter
from datetime import date, datetime, timedelta
from html import escape
from sqlite3 import Row
from zoneinfo import ZoneInfo

from candibot.store import EXCLUDED_REASON

DAYS = 30  # fenêtre des graphiques par jour
LIST_LIMIT = 20  # lignes par tableau
# HelloWork ne publie que des offres en France. Le serveur tourne souvent en UTC :
# sans fuseau fixe, un envoi à 0 h 30 heure de Paris tomberait la veille.
TIMEZONE = ZoneInfo("Europe/Paris")

STATUS_LABELS = {
    "applied": "Envoyées",
    "drafted": "Lettres prêtes",
    "needs_review": "À vérifier",
    "failed": "Échecs techniques",
    "external": "Site du recruteur",
    "new": "À noter",
    "rejected": "Écartées",
}

STYLE = """
:root {
  color-scheme: light dark;
  --bg: #fcfcfb; --surface: #ffffff; --border: #e4e3df; --text: #0b0b0b;
  --muted: #52514e; --grid: #ecebe7; --good: #047857; --bad: #c4302b;
  /* Vert du logo : nuance 700 en clair, 400 en sombre, contraste 4,5:1 au moins. */
  --brand: #047857;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f0f0e; --surface: #1a1a19; --border: #2e2d2b; --text: #f0efec;
    --muted: #c3c2b7; --grid: #2a2a28; --good: #34d399; --bad: #e66767; --brand: #34d399;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
.skip { position: absolute; left: 1rem; top: -3rem; padding: 0.5rem 0.75rem; background: var(--surface);
  border: 1px solid var(--border); border-radius: 6px; color: var(--text); }
.skip:focus { top: 1rem; }
main { max-width: 72rem; margin: 0 auto; padding: 2rem 1rem 4rem; }
h1 { font-size: 1.75rem; letter-spacing: -0.02em; margin: 0; }
h2 { font-size: 1.05rem; margin: 0 0 0.75rem; }
.meta { color: var(--muted); margin: 0.25rem 0 2rem; }
.tiles { list-style: none; padding: 0; margin-top: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: 0.75rem;
  margin-bottom: 1.5rem; }
.tile, section { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; }
.tile { padding: 0.9rem 1rem; }
.tile b { display: block; font-size: 1.9rem; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.tile span { color: var(--muted); font-size: 0.875rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 22rem), 1fr)); gap: 0.75rem;
  margin-bottom: 0.75rem; }
section { padding: 1rem; min-width: 0; }
section p { color: var(--muted); margin: -0.5rem 0 0.75rem; font-size: 0.875rem; }
svg { display: block; width: 100%; height: auto; }
svg text { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.bar { fill: var(--brand); }
.bar:hover { opacity: 0.75; }
.gridline { stroke: var(--grid); stroke-width: 1; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
th, td { text-align: left; padding: 0.4rem 0.5rem; border-top: 1px solid var(--border); vertical-align: top; }
th { color: var(--muted); font-weight: 600; border-top: 0; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
code { font: 0.8125rem ui-monospace, SFMono-Regular, Menlo, monospace; }
a { color: var(--brand); }
:focus-visible { outline: 2px solid var(--brand); outline-offset: 2px; }
main:focus { outline: none; }
summary { cursor: pointer; color: var(--muted); font-size: 0.875rem; margin-top: 0.5rem; }
details table { margin-top: 0.5rem; }
.ok { color: var(--good); } .ko { color: var(--bad); }
.empty { color: var(--muted); margin: 0; }
"""


def _day(iso: str) -> date:
    """Jour local d'un horodatage ISO stocké en UTC."""
    return datetime.fromisoformat(iso).astimezone(TIMEZONE).date()


def _link(row: Row) -> str:
    title = escape(row["title"])
    url = row["url"]
    # Les liens viennent de HelloWork : seul le https est rendu cliquable.
    return f'<a href="{escape(url)}">{title}</a>' if url.startswith("https://") else title


def columns(data: list[tuple[str, int]], label: str, axis: str, everyNth: int = 1) -> str:
    """Histogramme en colonnes, une seule série, suivi de ses données en tableau.

    L'infobulle de chaque colonne sert à la souris ; le tableau sert au clavier et aux lecteurs d'écran.
    """
    width, height, top, bottom, left = 360, 160, 10, 20, 28
    plotH = height - top - bottom
    peak = max((v for _, v in data), default=0) or 1
    step = (width - left) / max(len(data), 1)
    barW = max(step - 2, 1)  # 2 px d'écart entre colonnes
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">',
        f'<line class="gridline" x1="{left}" x2="{width}" y1="{top}" y2="{top}"/>',
        f'<text x="{left - 6}" y="{top + 4}" text-anchor="end">{peak}</text>',
        f'<line class="gridline" x1="{left}" x2="{width}" y1="{top + plotH}" y2="{top + plotH}"/>',
        f'<text x="{left - 6}" y="{top + plotH + 4}" text-anchor="end">0</text>',
    ]
    for i, (name, value) in enumerate(data):
        x = left + i * step + 1
        if value:
            h = max(value / peak * plotH, 2)
            y = top + plotH - h
            r = min(4, barW / 2, h)
            # Coins arrondis en haut seulement : la colonne reste posée sur l'axe.
            parts.append(
                f'<path class="bar" d="M{x:.1f},{top + plotH} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}'
                f" H{x + barW - r:.1f} Q{x + barW:.1f},{y:.1f} {x + barW:.1f},{y + r:.1f} V{top + plotH} Z"
                f'"><title>{escape(name)} : {value}</title></path>'
            )
        if i % everyNth == 0:
            parts.append(
                f'<text x="{x + barW / 2:.1f}" y="{height - 6}" text-anchor="middle">{escape(name)}</text>'
            )
    parts.append("</svg>")
    rows = [[escape(name), str(value)] for name, value in data if value]
    parts.append(
        f"<details><summary>Données</summary>{_table([axis, '#Offres'], rows, 'Aucune donnée.')}</details>"
    )
    return "".join(parts)


def _section(title: str, body: str, note: str = "") -> str:
    note = f"<p>{note}</p>" if note else ""
    return f"<section><h2>{title}</h2>{note}{body}</section>"


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="empty">{empty}</p>'
    head = "".join(f'<th class="num">{h[1:]}</th>' if h[0] == "#" else f"<th>{h}</th>" for h in headers)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="num">{c}</td>' if headers[i][0] == "#" else f"<td>{c}</td>"
            for i, c in enumerate(cells)
        )
        + "</tr>"
        for cells in rows
    )
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def render(offers: list[Row], runs: list[Row], now: datetime) -> str:
    """Page HTML complète. `now` fixe la fin de la fenêtre des graphiques."""
    counts = Counter(r["status"] for r in offers)
    excluded = sum(1 for r in offers if r["reason"] == EXCLUDED_REASON)
    toCheck = [r for r in offers if r["status"] in ("needs_review", "failed")]
    external = sorted(
        (r for r in offers if r["status"] == "external"), key=lambda r: r["seen_at"], reverse=True
    )
    applied = sorted(
        (r for r in offers if r["status"] == "applied"), key=lambda r: r["updated_at"], reverse=True
    )

    tiles = [
        (len(offers), "offres vues"),
        (counts["applied"], "candidatures envoyées"),
        (counts["drafted"], "lettres prêtes"),
        (len(toCheck), "à vérifier"),
        (counts["external"], "sur le site du recruteur"),
        (counts["rejected"], f"écartées, dont {excluded} écoles"),
    ]
    tilesHtml = "".join(f"<li class='tile'><b>{n}</b><span>{label}</span></li>" for n, label in tiles)

    today = now.astimezone(TIMEZONE).date()
    window = [today - timedelta(days=DAYS - 1 - i) for i in range(DAYS)]
    seenPerDay = Counter(_day(r["seen_at"]) for r in offers)
    # Une offre `applied` ne change plus : `updated_at` est le moment de l'envoi.
    appliedPerDay = Counter(_day(r["updated_at"]) for r in applied)
    seenData = [(d.strftime("%d/%m"), seenPerDay[d]) for d in window]
    appliedData = [(d.strftime("%d/%m"), appliedPerDay[d]) for d in window]

    # Les offres exclues portent une note 0 fictive : elles faussent la répartition.
    scores = Counter(r["score"] for r in offers if r["score"] is not None and r["reason"] != EXCLUDED_REASON)
    scoreData = [(str(s), scores[s]) for s in range(11)]
    statusRows = [
        [escape(label), f"<code>{status}</code>", str(counts[status])]
        for status, label in STATUS_LABELS.items()
        if counts[status]
    ]

    checkRows = [
        [
            f"<code>{escape(r['id'])}</code>",
            f"{_link(r)}<br>{escape(r['company'])}",
            escape(r["detail"] or ""),
        ]
        for r in toCheck
    ]
    externalRows = [
        [
            _day(r["seen_at"]).strftime("%d/%m/%Y"),
            _link(r),
            escape(r["company"]),
            escape(r["location"]),
        ]
        for r in external[:LIST_LIMIT]
    ]
    appliedRows = [
        [_day(r["updated_at"]).strftime("%d/%m/%Y"), str(r["score"]), _link(r), escape(r["company"])]
        for r in applied[:LIST_LIMIT]
    ]
    runRows = [
        [
            datetime.fromisoformat(r["started"]).astimezone(TIMEZONE).strftime("%d/%m/%Y %H:%M"),
            '<span class="ok">✓ réussi</span>'
            if r["problem"] is None
            else f'<span class="ko">✗ échec</span> : {escape(r["problem"])}',
        ]
        for r in runs
    ]

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>candibot · tableau de bord</title>
<style>{STYLE}</style>
</head>
<body>
<a class="skip" href="#main-content">Aller au contenu</a>
<main id="main-content" tabindex="-1">
<h1>candibot</h1>
<p class="meta">Tableau de bord généré le {now.astimezone(TIMEZONE):%d/%m/%Y à %H:%M}.</p>
<ul class="tiles">{tilesHtml}</ul>
<div class="grid">
{_section("Candidatures envoyées par jour", columns(appliedData, "Candidatures envoyées par jour", "Jour", 7), f"{DAYS} derniers jours")}
{_section("Offres découvertes par jour", columns(seenData, "Offres découvertes par jour", "Jour", 7), f"{DAYS} derniers jours")}
{_section("Répartition des notes", columns(scoreData, "Nombre d'offres par note sur 10", "Note"), "Note sur 10, hors écoles exclues")}
</div>
<div class="grid">
{_section("Offres par statut", _table(["Statut", "Code", "#Offres"], statusRows, "Aucune offre."))}
{_section("Derniers passages", _table(["Début", "Issue"], runRows, "Aucun passage enregistré."))}
</div>
<div class="grid">
{_section("À vérifier", _table(["ID", "Offre", "Détail"], checkRows, "Rien à vérifier."), "Vérifiez <code>data/shots/&lt;id&gt;.png</code>, puis <code>candibot retry &lt;id&gt;</code>.")}
</div>
<div class="grid">
{_section("À faire à la main", _table(["Vue le", "Offre", "Entreprise", "Lieu"], externalRows, "Aucune offre."), f"Candidature sur le site du recruteur. {LIST_LIMIT} plus récentes au plus.")}
{_section("Dernières candidatures", _table(["Date", "#Note", "Offre", "Entreprise"], appliedRows, "Aucune candidature envoyée."), f"{LIST_LIMIT} plus récentes au plus.")}
</div>
</main>
</body>
</html>
"""
