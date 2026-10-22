# candibot

Bot qui cherche des offres d'alternance sur HelloWork, les note par rapport au CV, rédige un message au recruteur pour chaque offre retenue et peut envoyer la candidature via le formulaire HelloWork.

## Fonctionnement

1. **Recherche** : parcourt les résultats HelloWork (contrat « Alternance ») pour chaque requête de `config.toml`, puis lit chaque nouvelle fiche. La fiche fournit un bloc JSON-LD `JobPosting`, sauf pour les offres anonymes, lues dans le HTML.
2. **Filtrage** : les offres publiées par des écoles, CFA et organismes de formation (`search.exclude_companies`) sont écartées sans appel à Claude. Ce sont des annonces qui servent à recruter des étudiants pour leurs propres cursus, et elles représentent la majorité des offres d'alternance sur HelloWork.
3. **Notation** : un modèle gratuit d'OpenCode (`llm.models`, essayés dans l'ordre) note l'offre sur 10 et rédige le message à partir de `data/cv.md`. OpenCode est un agent qui a des outils (shell, fichiers), et le texte des offres vient d'Internet : il tourne donc dans un bac à sable bubblewrap, avec le système en lecture seule, un `$HOME` vide et jetable, et seulement le réseau. Une lettre qui contient des caractères hors alphabet latin est refusée et confiée au modèle suivant. Les écoles qui recrutent pour leur propre cursus, les offres BTS ou bachelor et les postes hors sujet reçoivent une note basse.
4. **Envoi** (seulement avec `--send`) : remplit le formulaire HelloWork (identité, CV, message, CGU) et avance d'étape en étape. Les clics passent par `human_click` de WebSkrap (trajectoire de souris humaine). La preuve d'envoi est la page « Mes candidatures » du compte HelloWork. Le bot la consulte avant chaque envoi, pour ne jamais postuler deux fois à la même offre, puis après, pour confirmer que la candidature est partie. Il s'arrête en `needs_review`, avec une capture d'écran de chaque étape, dès qu'une étape contient un champ obligatoire qu'il ne sait pas remplir ou que la candidature n'apparaît pas dans la liste.
5. **Emails** (via [AgentMail](https://docs.agentmail.to), si `[mail]` est rempli) : une confirmation à la candidate après chaque candidature `applied` ou `needs_review`, avec l'offre et le message envoyé, puis le récapitulatif du passage. Une candidature `failed` ne déclenche pas de confirmation, puisque rien n'est parti. Ces emails ne servent jamais à écrire aux recruteurs. L'envoi porte une clé d'idempotence : un nouvel essai du même passage n'envoie pas de doublon.

Chaque offre n'est traitée qu'une fois : leur suivi est stocké dans `data/candibot.db`. Les offres qui renvoient vers le site du recruteur (`external`) sont listées dans le récapitulatif pour une candidature à la main.

## Installation

```bash
uv sync
cp config.example.toml config.toml   # puis compléter
# déposer data/cv.pdf (2 Mo max) et data/cv.md (version texte du CV)
```

Un agent de code (Claude Code ou autre) peut aussi faire cette configuration avec vous : le skill `.claude/skills/setup-candibot/SKILL.md` lui indique les questions à poser.

Prérequis : `opencode` et `bwrap` (bubblewrap), et le Chromium de Patchright (`uv run patchright install chromium` s'il manque).

## Utilisation

```bash
uv run candibot run            # simulation : recherche, notation, lettres ; rien n'est envoyé
uv run candibot run --send     # envoie aussi les candidatures prêtes
uv run candibot run --send --wait-code   # idem, et attend le code HelloWork dans data/otp.txt (15 min)
uv run candibot status         # nombre d'offres par statut
```

Pour recevoir le récapitulatif par email, mettre la clé AgentMail dans `.env` (ignoré par Git, permissions 600) et remplir `[mail]` : `inbox` pour la boîte d'envoi AgentMail, `to` pour le destinataire.

```bash
echo 'AGENTMAIL_API_KEY=am_...' > .env && chmod 600 .env
```

La boîte d'envoi se crée une fois avec un `client_id` fixe. Relancer la commande renvoie la même boîte au lieu d'en créer une autre :

```bash
curl -X POST https://api.agentmail.to/v0/inboxes -H "Authorization: Bearer $AGENTMAIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"username":"candibot","display_name":"Candibot","client_id":"candibot-recap-v1"}'
```

## Passage quotidien

Un timer systemd (`/etc/systemd/system/candibot.timer` et `candibot.service`) lance `candibot run --send` chaque jour à 07:30 UTC, avec jusqu'à 20 minutes de décalage aléatoire. Un passage manqué est rattrapé au démarrage suivant (`Persistent=true`). Un verrou `flock` sur `data/candibot.lock` empêche deux passages simultanés. Le journal est écrit dans `data/logs/daily.log`.

```bash
systemctl list-timers candibot.timer      # prochain passage
sudo systemctl start candibot.service     # passage immédiat
journalctl -u candibot.service -n 50      # état du dernier passage
```

Le passage automatique n'attend pas de code HelloWork. Si la session expire, les offres concernées passent en `needs_review` et la candidate est prévenue par email. Il faut alors relancer à la main avec `--wait-code`.

## Questions des recruteurs

Certaines offres posent des questions après l'identité et le CV. La section `[answers]` de `config.toml` fournit les réponses : civilité, disponibilité, et rémunération visée (la tranche proposée qui contient le salaire est choisie par calcul, en annuel ou en mensuel). Une question qui propose de « rejoindre le CFA » ou « intégrer notre formation » révèle une annonce d'école : l'offre passe en `rejected`. Toute autre question obligatoire laisse l'offre en `needs_review`.

## Statuts

| Statut | Sens |
|---|---|
| `new` | offre lue, pas encore notée |
| `rejected` | note inférieure à `min_score` |
| `external` | candidature uniquement sur le site du recruteur |
| `drafted` | lettre prête, en attente de `--send` |
| `applied` | HelloWork a affiché une confirmation |
| `needs_review` | envoi non confirmé (étape imprévue, erreur après envoi, arrêt brutal) : vérifier `data/shots/<id>.png` et l'espace HelloWork avant de repostuler |
| `failed` | erreur technique avant tout envoi : rien n'est parti |

## Limites connues

- Les sélecteurs HelloWork ont été relevés le 24/09/2026. Si le site change, les corrections se font dans `src/candibot/hellowork.py`.
- Première candidature : HelloWork crée le compte et envoie un code à 6 chiffres à l'adresse de la candidate. Il faut lancer `--wait-code` et écrire ce code dans `data/otp.txt`. Ensuite, le compte reste connecté dans `data/browser/` et les candidatures suivantes se font en une étape. Si la session expire, le code sera redemandé, et sans `--wait-code` l'offre passe en `needs_review`.
- La correspondance dans « Mes candidatures » se fait sur le titre et l'entreprise d'une même carte, car la liste n'affiche pas l'identifiant de l'offre. Seule la première page (les plus récentes) est lue.
- HelloWork interdit probablement l'automatisation dans ses CGU. Le risque est la suspension du compte candidat. Le bot limite le rythme (`max_applications`, `pause_seconds`) mais ne contourne aucun captcha ni blocage.
- Indeed bloque l'IP du VPS, et Welcome to the Jungle exige un compte pour chercher : ces deux sites ne sont pas gérés.
