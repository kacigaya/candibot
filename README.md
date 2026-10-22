<p align="center">
  <img src="assets/logo.svg" alt="Logo candibot" width="140">
</p>

<h1 align="center">candibot</h1>

<p align="center">
  <strong>Bot de candidature en alternance pour HelloWork.</strong><br>
  <em>Trouve les offres d'alternance, les note par rapport à votre CV, rédige un message pour chaque recruteur et peut envoyer la candidature à votre place.</em>
</p>

<p align="center">
  <a href="https://www.python.org/"><img alt="Python 3.12 ou plus récent" src="https://shieldcn.dev/badge/Python-3.12%2B-3776ab.svg?variant=secondary&amp;logo=python"></a>
  <a href="https://docs.astral.sh/uv/"><img alt="uv" src="https://shieldcn.dev/badge/uv-gestionnaire%20de%20paquets-de5fe9.svg?variant=secondary&amp;logo=uv"></a>
  <a href="https://github.com/kacigaya/webskrap"><img alt="Navigateur WebSkrap" src="https://shieldcn.dev/badge/WebSkrap-Patchright-2ead33.svg?variant=secondary&amp;logo=playwright"></a>
  <a href="https://opencode.ai/"><img alt="Modèles gratuits OpenCode" src="https://shieldcn.dev/badge/OpenCode-mod%C3%A8les%20gratuits-171717.svg?variant=secondary"></a>
  <img alt="Compatible Linux" src="https://shieldcn.dev/badge/Linux-compatible-171717.svg?variant=secondary&amp;logo=linux">
</p>

## Fonctionnalités

- Cherche les offres d'alternance sur HelloWork pour chacune de vos requêtes
- Écarte les offres des écoles, CFA et organismes de formation qui recrutent pour leur propre cursus, avant tout appel au modèle
- Note chaque offre sur 10 par rapport à votre CV avec un modèle gratuit d'OpenCode, isolé dans bubblewrap
- Rédige un message de 120 à 200 mots au recruteur, à partir des seuls faits de votre CV
- Remplit et envoie le formulaire HelloWork étape par étape, avec des mouvements de souris humains (seulement avec `--send`)
- Consulte la page « Mes candidatures » avant et après chaque envoi, pour ne jamais postuler deux fois
- Répond aux questions courantes des recruteurs : civilité, disponibilité et salaire
- Envoie par email une confirmation par candidature et un récapitulatif du passage, via [AgentMail](https://docs.agentmail.to)
- Tourne une fois par jour avec un timer systemd

> [!IMPORTANT]
> HelloWork interdit probablement l'automatisation dans ses CGU. Votre compte candidat peut être suspendu. candibot limite son rythme (`max_applications`, `pause_seconds`), mais ne contourne aucun captcha ni blocage.

## Installation

### Prérequis

| Outil | Rôle |
| --- | --- |
| [uv](https://docs.astral.sh/uv/) | Python 3.12 et dépendances |
| [opencode](https://opencode.ai/) | Notation et lettres, avec les modèles gratuits `opencode/*` |
| [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) | Bac à sable pour OpenCode |
| Chromium de Patchright | Navigateur. Lancer `uv run patchright install chromium` s'il manque. |

### Mise en place

```bash
git clone https://github.com/kacigaya/candibot.git
cd candibot
uv sync
cp config.example.toml config.toml
```

Ensuite :

1. Complétez `config.toml`. Les commentaires du fichier expliquent chaque champ.
2. Déposez votre CV dans `data/cv.pdf` (2 Mo max, limite HelloWork).
3. Déposez une version texte de votre CV dans `data/cv.md`. Le modèle lit seulement ce fichier pour noter les offres et rédiger les lettres.

`config.toml`, `.env` et `data/` sont ignorés par Git.

### Mise en place avec un agent

Un agent de code peut faire la configuration avec vous. Le skill [`setup-candibot`](.claude/skills/setup-candibot/SKILL.md) lui indique les questions à poser. L'agent écrit ensuite `config.toml` et vérifie qu'il se charge. Claude Code trouve le skill automatiquement.

## Utilisation

```bash
uv run candibot run
```

| Commande | Description |
| --- | --- |
| `run` | Simulation : recherche, notation et lettres. Rien n'est envoyé. |
| `run --send` | Envoie aussi les candidatures prêtes |
| `run --send --wait-code` | Idem, et attend le code HelloWork dans `data/otp.txt` pendant 15 minutes au plus |
| `status` | Nombre d'offres par statut |

### Première candidature

1. Lancez `uv run candibot run --send --wait-code`.
2. HelloWork crée votre compte et envoie un code à 6 chiffres par email.
3. Écrivez ce code dans `data/otp.txt`.

Le compte reste ensuite connecté grâce au profil navigateur de `data/browser/`. Si la session expire, HelloWork redemande un code. Sans `--wait-code`, l'offre passe en `needs_review`.

### Récapitulatif par email

1. Mettez votre clé AgentMail dans `.env` :

   ```bash
   echo 'AGENTMAIL_API_KEY=am_...' > .env && chmod 600 .env
   ```

2. Créez la boîte d'envoi une seule fois. Le `client_id` fixe rend l'appel idempotent : le relancer renvoie la même boîte.

   ```bash
   curl -X POST https://api.agentmail.to/v0/inboxes -H "Authorization: Bearer $AGENTMAIL_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"username":"candibot","display_name":"Candibot","client_id":"candibot-recap-v1"}'
   ```

3. Remplissez la section `[mail]` de `config.toml` : `inbox` pour la boîte d'envoi, `to` pour votre adresse.

Ces emails vous sont destinés. candibot n'écrit jamais aux recruteurs.

## Fonctionnement

1. **Recherche.** Pour chaque requête, candibot lit les résultats HelloWork filtrés sur le contrat « Alternance », puis ouvre chaque nouvelle offre. La fiche contient un bloc JSON-LD `JobPosting`, sauf pour les offres anonymes, lues dans le HTML.
2. **Filtrage.** Les offres des entreprises de `search.exclude_companies` sont écartées sans appel au modèle. Ce sont surtout des écoles qui publient des offres pour recruter des étudiants dans leurs cursus. Elles forment la majorité des offres d'alternance sur HelloWork.
3. **Notation.** Un modèle OpenCode (`llm.models`, essayés dans l'ordre) note l'offre et rédige le message à partir de `data/cv.md`. Une lettre qui contient des caractères hors alphabet latin est refusée et confiée au modèle suivant.
4. **Envoi** (seulement avec `--send`). candibot remplit l'identité, le CV, le message et les CGU, puis avance d'étape en étape. Il s'arrête en `needs_review`, avec une capture d'écran de chaque étape, si une étape contient un champ obligatoire qu'il ne sait pas remplir ou si la candidature n'apparaît pas dans « Mes candidatures ».
5. **Compte rendu.** Un email par candidature `applied` ou `needs_review`, avec l'offre et le message envoyé, puis le récapitulatif du passage. Chaque envoi porte une clé d'idempotence : un nouvel essai du même passage n'envoie pas de doublon.

Chaque offre n'est traitée qu'une fois. Le suivi est stocké dans `data/candibot.db`. Les offres qui renvoient vers le site du recruteur (`external`) sont listées dans le récapitulatif, pour une candidature à la main.

### Bac à sable

OpenCode est un agent qui a des outils (shell, fichiers), et le texte des offres vient d'Internet. Il tourne donc dans bubblewrap, avec le système en lecture seule, un `$HOME` vide et jetable, et seulement le réseau. La clé AgentMail est retirée de son environnement.

### Questions des recruteurs

Certaines offres posent des questions après l'identité et le CV. La section `[answers]` fournit les réponses : civilité, disponibilité et salaire visé. La tranche de salaire est choisie par calcul, en mensuel ou en annuel. Une question qui propose de « rejoindre le CFA » ou d'« intégrer notre formation » révèle une annonce d'école : l'offre passe en `rejected`. Toute autre question obligatoire laisse l'offre en `needs_review`.

### Statuts

| Statut | Sens |
| --- | --- |
| `new` | Offre lue, pas encore notée |
| `rejected` | Note inférieure à `min_score`, entreprise exclue ou annonce d'école |
| `external` | Candidature uniquement sur le site du recruteur |
| `drafted` | Lettre prête, en attente de `--send` |
| `applied` | Candidature confirmée dans « Mes candidatures » |
| `needs_review` | Envoi non confirmé. Vérifiez `data/shots/<id>.png` et votre espace HelloWork avant de repostuler. |
| `failed` | Erreur technique avant tout envoi : rien n'est parti |

## Passage quotidien

Un timer systemd peut lancer `candibot run --send` chaque jour. Remplacez `/chemin/vers/candibot`, `vous` et les entrées de `PATH` par vos valeurs.

`/etc/systemd/system/candibot.service` :

```ini
[Unit]
Description=Passage quotidien de candibot
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=vous
WorkingDirectory=/chemin/vers/candibot
# uv, opencode et bwrap doivent être dans le PATH.
Environment=PATH=/home/vous/.local/bin:/home/vous/.opencode/bin:/usr/local/bin:/usr/bin:/bin
# Le verrou empêche deux passages de partager le profil navigateur.
ExecStart=/usr/bin/flock -n /chemin/vers/candibot/data/candibot.lock uv run candibot run --send
StandardOutput=append:/chemin/vers/candibot/data/logs/daily.log
StandardError=append:/chemin/vers/candibot/data/logs/daily.log
TimeoutStartSec=3h
```

`/etc/systemd/system/candibot.timer` :

```ini
[Unit]
Description=Timer quotidien de candibot

[Timer]
OnCalendar=*-*-* 07:30:00 UTC
RandomizedDelaySec=20m
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now candibot.timer
systemctl list-timers candibot.timer      # prochain passage
journalctl -u candibot.service -n 50      # dernier passage
```

Le passage quotidien n'attend pas de code HelloWork. Si la session expire, les offres passent en `needs_review` et vous recevez un email. Relancez alors à la main avec `--wait-code` pour vous reconnecter.

## Développement

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

## Limites

- Le prompt de notation (`SYSTEM` dans [`src/candibot/llm.py`](src/candibot/llm.py)) vise un profil précis : Master 1, marketing et communication, Île-de-France. Adaptez-le pour un autre domaine ou une autre région.
- Les sélecteurs HelloWork ont été vérifiés le 24/09/2026. Si le site change, corrigez-les dans [`src/candibot/hellowork.py`](src/candibot/hellowork.py).
- « Mes candidatures » n'affiche pas l'identifiant de l'offre. La correspondance se fait donc sur le titre et l'entreprise d'une même carte. Seule la première page (les plus récentes) est lue.
- Indeed renvoie 403 (Cloudflare) depuis les IP de datacenter, et Welcome to the Jungle exige un compte pour chercher. Ces deux sites ne sont pas gérés.

## Avertissement

Ce projet est fourni à des fins éducatives. L'envoi automatique de candidatures peut enfreindre les CGU de HelloWork. Vous êtes responsable de son usage et de chaque candidature envoyée en votre nom.
