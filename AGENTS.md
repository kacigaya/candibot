# candibot — notes projet

- Python 3.12, gestionnaire `uv`. Commandes : `uv run candibot run [--send]`, `uv run candibot status`.
- Validation : `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`.
- Navigateur : WebSkrap (Patchright), `channel="chromium"` obligatoire (VPS Linux ARM64, pas de canal chrome).
- Profil navigateur persistant : `data/browser/` (cookies et compte HelloWork). Ne jamais le committer.
- Clics du formulaire : `WebSkrapSession.human_click`, précédé d'un `click(trial=True)` qui vérifie qu'aucun calque ne masque la cible (human_click clique à des coordonnées).
- Notation et lettres : `opencode run` (modèles gratuits `opencode/*`) dans bubblewrap (`llm.sandbox_command`). L'offre gratuite refuse tout agent modifié (outils retirés, permissions) : on garde l'agent standard et on isole de l'extérieur. `run` exige stdin fermé (sinon blocage). Sortie : événements JSON, objet `{score, reason, letter}` extrait du texte.
- Emails : API AgentMail (`urllib`, sans SDK), boîte d'envoi dans `mail.inbox` de `config.toml`, clé `AGENTMAIL_API_KEY` dans `.env` (chargé par `config.load_env`, sans écraser l'environnement). Offre gratuite : 3 boîtes, 3 000 emails/mois.
- Données personnelles hors Git : `config.toml`, `data/` (CV, base SQLite, captures, logs).
- HelloWork : preuve d'envoi = page `candidat/mes-candidatures.html` (titre + entreprise sur une carte) ; étape code email (`data-cy=otp-input-1..6`, validation auto au 6ᵉ chiffre) à la première candidature ; bannière cookies maison (`.hw-cc-notice-modal__btn-continue-without-accepting`), offres anonymes sans JSON-LD, formulaire en plusieurs étapes (`[data-cy='submitButton']`).
- Indeed renvoie 403 (Cloudflare) sur l'IP du VPS ; Welcome to the Jungle exige un compte pour la recherche. Hors périmètre.
