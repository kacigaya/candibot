---
name: setup-candibot
description: Configure candibot with the user through a short interview. Use when the user wants to install, configure, or reconfigure the bot, create or fix config.toml, add or replace a CV, change search or contract types, or set up the AgentMail recap.
---

# Configurer candibot

Pose les questions à l'utilisateur, puis écris `config.toml` à partir de `config.example.toml`. Le fichier final doit se charger sans erreur avec `candibot.config.load`.

## Règles

- Parle dans la langue de l'utilisateur. Pose 3 ou 4 questions par message, pas plus.
- Propose des valeurs par défaut seulement pour les réglages techniques (`pages`, `min_score`, `max_scored`, `max_applications`, `pause_seconds`, `exclude_companies`, `llm.models`).
- Demande toujours les données personnelles : identité, email, civilité, disponibilité, salaire. Les valeurs de `config.example.toml` (`Madame`, `immédiate`, montant du SMIC) sont des exemples, pas des réponses. N'invente rien. Un champ facultatif sans réponse reste vide.
- Ne demande jamais la clé AgentMail dans la conversation. L'utilisateur la saisit lui-même dans son terminal (étape 7).
- Ne remplace jamais un fichier existant (`config.toml`, `.env`, CV) sans l'accord de l'utilisateur.
- `config.toml`, `.env` et `data/` sont ignorés par Git. Ne les ajoute jamais à un commit.

## Étapes

1. **Besoin.** Identifie la demande, puis fais seulement les étapes utiles :

   | Demande | Étapes |
   | --- | --- |
   | Installation complète | 2 à 10 |
   | Reconfiguration | 3, puis les étapes des valeurs à changer, puis 9 |
   | Nouveau CV | 3, 5, 9 |
   | Emails seulement | 3, 7, 9 |

2. **Prérequis.** Le bot tourne sous Linux seulement, car le bac à sable bubblewrap n'existe pas ailleurs. Sous Windows ou macOS, demande une machine Linux, une VM ou WSL 2, et arrête-toi là.
   1. Vérifie `uv`, `opencode` et `bwrap` avec `command -v`. Si un outil manque, dis-le et donne le lien ou la commande d'installation.
   2. Lance `uv sync`. Il installe Python 3.12 si besoin.
   3. Lance `uv run patchright install chromium` si le Chromium de Patchright manque.
   4. Ne continue pas sans `opencode` et `bwrap` : la notation en dépend.

3. **Config existante.** Si `config.toml` existe, lis-le d'abord.
   - Modifie seulement les valeurs demandées. Garde les autres clés et les commentaires.
   - Cherche les champs obligatoires absents, puis demande-les. Les champs obligatoires sont `candidate.first_name`, `last_name`, `email`, `cv_pdf`, `cv_text`, `search.queries`, `location` et `contracts`. Une config ancienne n'a souvent pas `search.contracts`.
   - Garde les chemins `cv_pdf` et `cv_text` existants, sauf demande contraire.

4. **Identité** (section `[candidate]`) :
   - prénom et nom (`first_name`, `last_name`). Ils signent les lettres et remplissent le formulaire HelloWork ;
   - email (`email`). HelloWork y envoie le code à 6 chiffres de la première candidature ;
   - date de début, et en alternance seulement, rythme (`rhythm`). Facultatifs : vides, les lettres n'en parlent pas. La date de début sert aussi à l'étape 6.

5. **CV.** Demande le chemin du CV.
   1. Vérifie que c'est un vrai PDF (`file` doit dire `PDF document`) et qu'il fait 2 097 152 octets au plus (limite HelloWork, vérifiée par le bot). Sinon, demande un autre fichier. Ne renomme pas un autre format en `.pdf`.
   2. Si le fichier cible (`data/cv.pdf` ou le chemin de `cv_pdf`) existe et diffère du nouveau, demande l'accord avant de le remplacer. Ne copie pas un fichier sur lui-même.
   3. Il faut aussi une version texte (`data/cv.md` ou le chemin de `cv_text`). Si l'utilisateur en a une, copie-la, avec la même règle de remplacement.
   4. Sinon, extrais le texte avec `pdftotext -layout` s'il est installé. Mets-le en Markdown simple et montre-le à l'utilisateur pour qu'il le valide.
   5. Si `pdftotext` manque ou si le texte est vide (PDF scanné), demande à l'utilisateur de coller ou de fournir une version texte.
   6. Ne termine pas avec un CV texte vide. Le modèle note les offres et rédige les lettres à partir de ce texte seulement. Un oubli dans ce fichier ne sera jamais cité.

6. **Recherche et réponses** (sections `[search]` et `[answers]`) :
   - métiers recherchés (`queries`) : 2 à 4 requêtes courtes, comme sur HelloWork ;
   - lieu (`location`) : ville, département ou région ;
   - types de contrat (`contracts`, obligatoire) : un ou plusieurs parmi `CDI`, `CDD`, `Intérim`, `Stage`, `Alternance`, `Indépendant`, `Freelance`, `Franchise`, `Associé`, `Fonctionnaire`, `Stage de lycée`. Écris les valeurs exactement ainsi. Plusieurs contrats donnent les offres de chacun ;
   - `exclude_companies` écarte les écoles qui recrutent pour leur cursus. Garde la liste en alternance et en stage. Pour des CDI ou CDD seulement, propose de la vider. Propose d'ajouter des écoles si l'utilisateur en connaît ;
   - civilité (`civility`) : texte exact de l'option HelloWork, `Madame` ou `Monsieur` ;
   - salaire brut mensuel visé (`salary_monthly`, nombre) et réponse libre (`salary_text`). En alternance, le minimum légal dépend de l'âge et de l'année de formation : renvoie l'utilisateur vers service-public.gouv.fr s'il ne connaît pas son montant.

   **Disponibilité.** Déduis les deux champs de la même réponse de l'étape 4, puis confirme-les avec l'utilisateur :
   - `candidate.availability` : la phrase des lettres, par exemple « dès le 1er septembre » ;
   - `answers.availability` : le début du texte d'une option HelloWork, par exemple `immédiate`. Ne choisis pas `immédiate` si la date de début est future. Laisse le champ vide si aucune option ne correspond : la question passera en `needs_review`.

7. **Emails** (section `[mail]`, facultative). Demande si l'utilisateur veut un email après chaque passage. Ces emails contiennent les lettres envoyées.
   - Non : laisse `inbox` et `to` vides. Le récapitulatif s'affiche dans le terminal.
   - Oui : demande l'adresse de destination (`to`). Donne ensuite ces commandes à l'utilisateur. Il les lance lui-même dans son terminal. La clé n'apparaît ni à l'écran ni dans l'historique, et les autres variables de `.env` sont gardées :

     ```bash
     read -r -s -p 'Clé AgentMail : ' AGENTMAIL_API_KEY && echo
     export AGENTMAIL_API_KEY
     (umask 077; touch .env; { grep -v '^AGENTMAIL_API_KEY=' .env; printf 'AGENTMAIL_API_KEY=%s\n' "$AGENTMAIL_API_KEY"; } > .env.tmp && mv .env.tmp .env)
     ```

     Dans le même terminal, il lance la commande `curl` de création de boîte (README, section « Récapitulatif par email »), puis `unset AGENTMAIL_API_KEY`. Mets l'adresse de la boîte créée dans `inbox`.

8. **Écriture.** Écris `config.toml` en gardant la structure et les commentaires de `config.example.toml`. En reconfiguration, modifie le fichier existant au lieu de le recréer. Montre un résumé des valeurs à l'utilisateur, sans la clé API.

9. **Vérification.** Lance :

   ```bash
   uv run python -c "from pathlib import Path; from candibot.config import load; c = load(Path('config.toml')); print(c.candidate.firstName, c.search.queries, c.search.contracts, c.mail.enabled)"
   ```

   Corrige toute erreur et relance.

10. **Simulation.** Propose `uv run candibot run`. Avant de demander l'accord, explique ce qu'elle fait :
    - elle n'envoie aucune candidature ;
    - elle interroge HelloWork ;
    - elle transmet le texte du CV, le nom et la recherche au service de modèles d'OpenCode ;
    - si `[mail]` est rempli, elle envoie un email récapitulatif avec les lettres rédigées.

    Lance-la seulement si l'utilisateur accepte.

## Fin

Rappelle à l'utilisateur :

1. La première candidature réelle se lance avec `uv run candibot run --send --wait-code`.
2. HelloWork envoie alors un code à 6 chiffres. Il faut l'écrire dans `data/otp.txt` dans les 15 minutes.
3. HelloWork interdit probablement l'automatisation dans ses CGU. Le compte candidat peut être suspendu.
