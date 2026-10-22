# Contribuer

## Mise en place

```bash
uv sync
uv run patchright install chromium
```

## Vérifications

Lancez ces trois commandes avant chaque commit. La CI les lance aussi sur chaque pull request.

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

## Nommage

Le projet ne suit pas PEP 8 pour les noms :

| Élément | Convention | Exemple |
| --- | --- | --- |
| Fonctions, méthodes, paramètres, variables, attributs | camelCase | `searchUrl`, `firstName` |
| Classes | PascalCase | `ApplyResult` |
| Constantes de module | MAJUSCULES | `LETTER_MAX` |

Exceptions :

- les clés de `config.toml` et les colonnes SQLite restent en snake_case ;
- les noms imposés par une bibliothèque (Patchright, argparse, pytest) restent tels quels. Avec argparse, utilisez `dest=` pour obtenir un nom en camelCase.

Ruff ne sait pas vérifier cette convention. `tests/test_naming.py` le fait. Pour un nouveau nom imposé par du code externe, ajoutez-le à `EXTERNAL` dans ce test.

## Données personnelles

Ne commitez jamais `config.toml`, `.env` ni `data/`. Ils sont ignorés par Git. Dans les tests, utilisez des noms et des adresses fictifs (`example.com`).
