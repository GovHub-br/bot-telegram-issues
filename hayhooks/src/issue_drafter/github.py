"""Acesso somente leitura ao GitHub usado pelas tools do Agent."""

from __future__ import annotations

import time
from typing import Any

import httpx

LABELS_TTL = 300  # segundos


class GitHubReader:
    def __init__(self, token: str, org: str, only_repos: tuple[str, ...] = (), *,
                 transport: httpx.BaseTransport | None = None):
        self.org = org
        self.only_repos = only_repos
        self._http = httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "bot-telegram-issues/drafter",
            },
            timeout=20,
            transport=transport,
        )
        self._labels: dict[str, tuple[float, list[str]]] = {}

    def labels(self, repo: str) -> list[str]:
        cached = self._labels.get(repo)
        if cached and time.monotonic() - cached[0] < LABELS_TTL:
            return cached[1]
        resp = self._http.get(f"/repos/{self.org}/{repo}/labels", params={"per_page": 100})
        resp.raise_for_status()
        names = [lb["name"] for lb in resp.json()]
        self._labels[repo] = (time.monotonic(), names)
        return names

    def _scope(self) -> str:
        if self.only_repos:
            return " ".join(f"repo:{self.org}/{r}" for r in self.only_repos)
        return f"org:{self.org}"

    def search(self, terms: str, limit: int = 6) -> list[dict[str, Any]]:
        """Busca issues em todos os repositórios liberados da organização."""
        resp = self._http.get(
            "/search/issues",
            params={"q": f"{self._scope()} is:issue {terms}".strip(), "per_page": limit},
        )
        resp.raise_for_status()
        return [
            {
                "repo": it["repository_url"].rstrip("/").rsplit("/", 1)[-1],
                "number": it["number"],
                "title": it["title"],
                "state": it["state"],
                "url": it["html_url"],
                "labels": [lb["name"] for lb in it.get("labels", [])],
                "body": (it.get("body") or "")[:300],
            }
            for it in resp.json().get("items", [])
        ]
