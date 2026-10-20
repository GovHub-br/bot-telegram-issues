"""Acesso somente leitura ao GitHub usado pelas tools do Agent."""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

LABELS_TTL = 300  # segundos
TEMPLATES_TTL = 600


@dataclass
class IssueTemplate:
    """Template de issue em Markdown (.github/ISSUE_TEMPLATE/*.md)."""

    name: str
    about: str
    body: str
    labels: list[str] = field(default_factory=list)


def parse_template(text: str, filename: str) -> IssueTemplate:
    """Lê o front matter (name, about, labels) e devolve o corpo do template."""
    meta: dict[str, str] = {}
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip()] = value.strip().strip("'\"")
    labels = [lb.strip().strip("'\"") for lb in meta.get("labels", "").strip("[]").split(",") if lb.strip(" '\"")]
    return IssueTemplate(
        name=meta.get("name") or filename.rsplit(".", 1)[0],
        about=meta.get("about", ""),
        body=body.strip(),
        labels=labels,
    )


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
        self._templates: dict[str, tuple[float, list[IssueTemplate]]] = {}

    def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET com uma nova tentativa: o GitHub às vezes derruba a conexão sem responder."""
        for attempt in (1, 2, 3):
            try:
                return self._http.get(url, **kwargs)
            except httpx.TransportError:
                if attempt == 3:
                    raise
                time.sleep(0.5 * attempt)
        raise AssertionError("inalcançável")

    def labels(self, repo: str) -> list[str]:
        cached = self._labels.get(repo)
        if cached and time.monotonic() - cached[0] < LABELS_TTL:
            return cached[1]
        resp = self._get(f"/repos/{self.org}/{repo}/labels", params={"per_page": 100})
        resp.raise_for_status()
        names = [lb["name"] for lb in resp.json()]
        self._labels[repo] = (time.monotonic(), names)
        return names

    def templates(self, repo: str) -> list[IssueTemplate]:
        """Templates de issue em Markdown do repositório (os formulários .yml ficam de fora)."""
        cached = self._templates.get(repo)
        if cached and time.monotonic() - cached[0] < TEMPLATES_TTL:
            return cached[1]
        found: list[IssueTemplate] = []
        resp = self._get(f"/repos/{self.org}/{repo}/contents/.github/ISSUE_TEMPLATE")
        if resp.status_code != 404:
            resp.raise_for_status()
            for item in resp.json():
                if item.get("type") != "file" or not item["name"].lower().endswith(".md"):
                    continue
                file = self._get(item["url"])
                file.raise_for_status()
                text = base64.b64decode(file.json()["content"]).decode("utf-8")
                found.append(parse_template(text, item["name"]))
        self._templates[repo] = (time.monotonic(), found)
        return found

    def _scope(self) -> str:
        if self.only_repos:
            return " ".join(f"repo:{self.org}/{r}" for r in self.only_repos)
        return f"org:{self.org}"

    def search(self, terms: str, limit: int = 6) -> list[dict[str, Any]]:
        """Busca issues em todos os repositórios liberados da organização."""
        resp = self._get(
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
