"""Réponses aux questions complémentaires des recruteurs (étape après l'identité et le CV).

HelloWork affiche ces questions en listes déroulantes ou en champs texte, avec des
libellés libres. Seules les questions reconnues ici reçoivent une réponse ; une
question obligatoire inconnue laisse la candidature en `needs_review`.
"""

import re
from dataclasses import dataclass

# Une question qui propose d'intégrer un cursus trahit une annonce d'école ou de CFA.
SCHOOL = "school"
_SCHOOL = re.compile(
    r"(rejoindre|int[ée]grer|inscri|candidater? (à|a) (notre|la) formation)"
    r".{0,40}(cfa|[ée]cole|formation|campus|programme|cursus|bachelor|bts)",
    re.I,
)
_CIVILITY = re.compile(r"civilit", re.I)
_AVAILABILITY = re.compile(r"disponib", re.I)
_SALARY = re.compile(r"salai|r[ée]mun[ée]ration|pr[ée]tention|attentes? (salariale|financi)", re.I)


@dataclass(frozen=True)
class Answers:
    civility: str  # texte de l'option, ex. "Madame"
    availability: str  # début du texte de l'option, ex. "immédiate"
    salary_monthly: float  # brut mensuel visé, ex. 1867.02 (100 % du SMIC)
    salary_text: str  # réponse aux champs libres sur la rémunération


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _range(text: str) -> tuple[float, float] | None:
    """« 20 000€ - 25 000€ » -> (20000, 25000) ; « inférieur à 15 000€ » -> (0, 15000)."""
    numbers = [float(n.replace(" ", "").replace(" ", "")) for n in re.findall(r"\d[\d  ]*", text)]
    if not numbers:
        return None
    lowered = text.casefold()
    if len(numbers) == 1:
        if re.search(r"inf[ée]rieur|moins|jusqu", lowered):
            return 0.0, numbers[0]
        if re.search(r"sup[ée]rieur|plus|au-del", lowered):
            return numbers[0], float("inf")
        return numbers[0], numbers[0]
    return min(numbers[:2]), max(numbers[:2])


def _salary_option(options: list[tuple[str, str]], monthly: float) -> str | None:
    """Tranche qui contient le salaire visé, en annuel ou en mensuel selon l'échelle des tranches."""
    ranges = [(value, r) for value, text in options if value and (r := _range(text))]
    if not ranges:
        return None
    # Des bornes de plusieurs milliers signalent des tranches annuelles.
    annual = max(hi if hi != float("inf") else lo for _, (lo, hi) in ranges) > 5000
    target = monthly * 12 if annual else monthly
    for value, (lo, hi) in ranges:
        if lo <= target <= hi:
            return value
    return None


def answer(label: str, options: list[tuple[str, str]] | None, answers: Answers) -> str | None:
    """Valeur à saisir pour la question `label`, `SCHOOL` pour une annonce d'école, ou None si inconnue.

    `options` vaut None pour un champ texte, sinon la liste (valeur, texte) d'une liste déroulante.
    """
    if _SCHOOL.search(label):
        return SCHOOL
    if options is None:
        if _SALARY.search(label):
            return answers.salary_text
        if _AVAILABILITY.search(label):
            return f"Disponible {answers.availability}"
        return None
    choices = [(value, _norm(text)) for value, text in options if value]
    if _CIVILITY.search(label):
        wanted = _norm(answers.civility)
        return next((value for value, text in choices if text == wanted), None)
    if _AVAILABILITY.search(label):
        wanted = _norm(answers.availability)
        return next((value for value, text in choices if text.startswith(wanted)), None)
    if _SALARY.search(label):
        return _salary_option(options, answers.salary_monthly)
    return None
