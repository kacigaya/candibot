"""Convention de nommage du projet, que Ruff ne sait pas vérifier.

Fonctions, méthodes, paramètres, variables et attributs : camelCase.
Classes : PascalCase. Constantes de module : MAJUSCULES.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAMEL = re.compile(r"_?[a-z][a-zA-Z0-9]*")
PASCAL = re.compile(r"_?[A-Z][a-zA-Z0-9]*")
CONSTANT = re.compile(r"_?[A-Z][A-Z0-9_]*")
# Noms imposés par du code externe.
EXTERNAL = {
    "tmp_path",  # fixture pytest
    "human_click",  # méthode de WebSkrapSession, imitée par les tests
}


def offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def check(name: str, line: int, pattern: re.Pattern, *alternatives: re.Pattern) -> None:
        if name == "_" or name in EXTERNAL or (name.startswith("__") and name.endswith("__")):
            return
        if not any(p.fullmatch(name) for p in (pattern, *alternatives)):
            found.append(f"{path.relative_to(ROOT)}:{line} {name}")

    moduleLevel = {id(node) for node in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            check(node.name, node.lineno, PASCAL)
            for field in node.body:  # attributs de dataclass
                if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name):
                    check(field.target.id, field.lineno, CAMEL)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            check(node.name, node.lineno, CAMEL)
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None:
                    check(arg.arg, arg.lineno, CAMEL)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [n for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]
            for name in names:
                # Les constantes de module peuvent rester en MAJUSCULES.
                extra = (CONSTANT,) if id(node) in moduleLevel else ()
                check(name.id, node.lineno, CAMEL, *extra)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            for name in ast.walk(node.target):
                if isinstance(name, ast.Name):
                    check(name.id, getattr(node, "lineno", 0), CAMEL)
    return found


def testNamesFollowProjectConvention():
    files = sorted([*ROOT.glob("src/candibot/*.py"), *ROOT.glob("tests/*.py")])
    assert files
    assert [o for f in files for o in offenders(f)] == []
