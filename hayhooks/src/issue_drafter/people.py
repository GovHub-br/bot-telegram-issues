"""Busca de pessoas por semelhança de nome (para escolher o responsável de uma issue).

Compara o que a pessoa digitou com o nome do perfil e com o login do GitHub, ignorando
acentos e maiúsculas: "lucas botino" acha "Lucas Bottino (@bottinolucas)".
O mesmo módulo existe em src/issues_bot/people.py (bot).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz, utils

MIN_SCORE = 60


@dataclass(frozen=True)
class Person:
    login: str
    name: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} (@{self.login})" if self.name else f"@{self.login}"


@dataclass(frozen=True)
class Match:
    person: Person
    score: int


def _norm(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return utils.default_process(ascii_text)


def score(query: str, person: Person) -> int:
    q = _norm(query.lstrip("@"))
    login = _norm(person.login)
    scores = [fuzz.ratio(q.replace(" ", ""), login), 0.9 * fuzz.partial_ratio(q.replace(" ", ""), login)]
    if person.name:
        name = _norm(person.name)
        # Cada palavra digitada procura a palavra mais parecida do nome: "lucas borges" bate
        # inteiro com "Lucas Guimarães Borges" e só pela metade com "João Lucas Siqueira".
        words = name.split()
        cover = sum(max(fuzz.ratio(t, w) for w in words) for t in q.split()) / max(len(q.split()), 1)
        scores += [cover, fuzz.token_sort_ratio(q, name)]
    return round(max(scores))


def find_people(query: str, people: list[Person], limit: int = 3, min_score: int = MIN_SCORE) -> list[Match]:
    """As pessoas mais parecidas com `query`, da mais para a menos parecida."""
    if not query.strip():
        return []
    matches = sorted((Match(p, score(query, p)) for p in people), key=lambda m: (-m.score, m.person.login.lower()))
    return [m for m in matches if m.score >= min_score][:limit]
