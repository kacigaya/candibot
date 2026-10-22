---
name: setup-candibot
description: Configure candibot with the user through a short interview. Use when the user wants to install, configure, or reconfigure the bot, create config.toml, add a CV, or set up the AgentMail recap.
---

# Configurer candibot

Pose les questions à l'utilisateur, puis écris `config.toml` à partir de `config.example.toml`. Le fichier final doit se charger sans erreur avec `candibot.config.load`.

## Règles

- Parle dans la langue de l'utilisateur. Pose 3 ou 4 questions par message, pas plus.
- Propose la valeur de `config.example.toml` comme défaut. L'utilisateur peut répondre « défaut ».
- N'invente aucune donnée personnelle. Une réponse manquante reste vide si le champ est facultatif.
- Ne demande jamais la clé AgentMail dans la conversation. Donne la commande à l'utilisateur pour qu'il l'écrive lui-même dans `.env`.
- `config.toml`, `.env` et `data/` sont ignorés par Git. Ne les ajoute jamais à un commit.
- Si `config.toml` existe déjà, lis-le d'abord. Demande seulement les valeurs à changer, et garde les autres.

## Étapes

1. **Prérequis.** Vérifie `uv`, `opencode` et `bwrap` avec `command -v`. Lance `uv sync`. Si un outil manque, dis-le et donne le lien ou la commande d'installation. Ne continue pas sans `opencode` et `bwrap` : la notation en dépend.

2. **Identité** (section `[candidate]`) :
   - prénom et nom (`first_name`, `last_name`). Ils signent les lettres et remplissent le formulaire HelloWork ;
   - email (`email`). HelloWork y envoie le code à 6 chiffres de la première candidature ;
   - date de début (`availability`) et, en alternance seulement, rythme (`rhythm`). Facultatifs : vides, les lettres n'en parlent pas.

3. **CV.** Demande le chemin du CV PDF. Copie-le dans `data/cv.pdf`. Vérifie qu'il fait 2 Mo au plus (limite HelloWork). Il faut aussi une version texte dans `data/cv.md` :
   - si l'utilisateur en a une, copie-la ;
   - sinon, extrais le texte du PDF (`pdftotext -layout` s'il est installé), mets-le en Markdown simple et montre-le à l'utilisateur pour qu'il le valide. Le modèle note les offres et rédige les lettres à partir de ce texte seulement. Un oubli dans ce fichier ne sera jamais cité.

4. **Recherche** (section `[search]`) :
   - métiers recherchés (`queries`) : 2 à 4 requêtes courtes, comme sur HelloWork ;
   - lieu (`location`) : ville, département ou région ;
   - types de contrat (`contracts`, obligatoire) : un ou plusieurs parmi `CDI`, `CDD`, `Intérim`, `Stage`, `Alternance`, `Indépendant`, `Freelance`, `Franchise`, `Associé`, `Fonctionnaire`, `Stage de lycée`. Écris les valeurs exactement ainsi ;
   - note minimale (`min_score`, défaut 7) et nombre maximal de candidatures par passage (`max_applications`, défaut 10) ;
   - garde `pages`, `max_scored`, `pause_seconds` et `exclude_companies` par défaut, sauf demande contraire. Propose d'ajouter des écoles à `exclude_companies` si l'utilisateur en connaît.

5. **Réponses aux recruteurs** (section `[answers]`) :
   - civilité (`civility`) : texte exact de l'option HelloWork, `Madame` ou `Monsieur` ;
   - disponibilité (`availability`) : début du texte de l'option, par exemple `immédiate` ;
   - salaire brut mensuel visé (`salary_monthly`, nombre) et réponse libre (`salary_text`). En alternance, le minimum légal dépend de l'âge et de l'année de formation : renvoie l'utilisateur vers service-public.gouv.fr s'il ne connaît pas son montant.

6. **Emails** (section `[mail]`, facultative). Demande si l'utilisateur veut un récapitulatif par email après chaque passage :
   - non : laisse `inbox` et `to` vides. Le récapitulatif s'affiche dans le terminal ;
   - oui : demande l'adresse de destination (`to`). Donne ensuite ces commandes, à lancer par l'utilisateur lui-même, puis mets l'adresse de la boîte créée dans `inbox` :

     ```bash
     echo 'AGENTMAIL_API_KEY=am_...' > .env && chmod 600 .env
     ```

     La commande `curl` de création de boîte est dans la section « Utilisation » du README.

7. **Exclusions.** `exclude_companies` écarte les écoles qui recrutent pour leur cursus. Cette liste sert surtout en alternance et en stage. Pour un CDI ou un CDD seulement, propose de la vider.

8. **Écriture.** Écris `config.toml` en gardant la structure et les commentaires de `config.example.toml`. Montre un résumé des valeurs à l'utilisateur, sans la clé API.

9. **Vérification.** Lance :

   ```bash
   uv run python -c "from pathlib import Path; from candibot.config import load; c = load(Path('config.toml')); print(c.candidate.first_name, c.search.queries, c.mail.enabled)"
   ```

   Corrige toute erreur et relance. Propose ensuite une simulation, qui n'envoie rien : `uv run candibot run`. Lance-la seulement si l'utilisateur accepte, car elle interroge HelloWork et le modèle.

## Fin

Rappelle à l'utilisateur :

1. La première candidature réelle se lance avec `uv run candibot run --send --wait-code`.
2. HelloWork envoie alors un code à 6 chiffres. Il faut l'écrire dans `data/otp.txt` dans les 15 minutes.
3. HelloWork interdit probablement l'automatisation dans ses CGU. Le compte candidat peut être suspendu.
