"""Cliente assíncrono para issues dos repositórios de uma organização do GitHub."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx

API_URL = "https://api.github.com"
REPOS_TTL = 600  # segundos de cache da lista de repositórios

State = Literal["open", "closed", "all"]


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Repo:
    name: str
    description: str = ""


@dataclass
class Label:
    name: str
    color: str = ""
    description: str = ""


@dataclass
class Comment:
    author: str
    body: str
    created_at: datetime
    url: str


@dataclass
class Issue:
    repo: str
    number: int
    title: str
    state: str
    state_reason: str | None
    author: str
    url: str
    body: str
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    comments: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    node_id: str = ""

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Issue:
        return cls(
            # repository_url = https://api.github.com/repos/<org>/<repo>
            repo=data.get("repository_url", "").rstrip("/").rsplit("/", 1)[-1],
            number=data["number"],
            title=data["title"],
            state=data["state"],
            state_reason=data.get("state_reason"),
            author=(data.get("user") or {}).get("login", "?"),
            url=data["html_url"],
            body=data.get("body") or "",
            labels=[lb["name"] for lb in data.get("labels", [])],
            assignees=[a["login"] for a in data.get("assignees", [])],
            comments=data.get("comments", 0),
            created_at=_parse_dt(data.get("created_at")),
            updated_at=_parse_dt(data.get("updated_at")),
            closed_at=_parse_dt(data.get("closed_at")),
            node_id=data.get("node_id", ""),
        )


@dataclass
class Project:
    id: str
    title: str
    url: str
    status_field: str | None = None
    status_options: dict[str, str] = field(default_factory=dict)


@dataclass
class Page:
    issues: list[Issue]
    page: int
    has_next: bool
    total: int | None = None


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class GitHubClient:
    """Acesso às issues da organização `org`.

    `only_repos` restringe o bot a alguns repositórios; vazio = todos os repositórios da
    organização que o token enxerga, com issues ativadas e não arquivados.
    """

    def __init__(
        self,
        token: str,
        org: str,
        only_repos: tuple[str, ...] = (),
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.org = org
        self.only_repos = only_repos
        self._repos: list[Repo] = []
        self._repos_at = 0.0
        self._http = httpx.AsyncClient(
            base_url=API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "bot-telegram-issues",
            },
            timeout=20,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(0, f"Falha de conexão com o GitHub: {exc}") from exc
        if resp.is_error:
            raise GitHubError(resp.status_code, _error_message(resp))
        return resp

    # ---- repositórios ------------------------------------------------------------

    async def list_repos(self, *, refresh: bool = False) -> list[Repo]:
        """Repositórios com issues ativadas, do mais movimentado para o menos."""
        if refresh or not self._repos or time.monotonic() - self._repos_at > REPOS_TTL:
            items: list[dict[str, Any]] = []
            url: str | None = f"/orgs/{self.org}/repos"
            params: dict[str, Any] | None = {"type": "all", "sort": "pushed", "direction": "desc", "per_page": 100}
            while url:
                resp = await self._request("GET", url, params=params)
                items += resp.json()
                url, params = resp.links.get("next", {}).get("url"), None
            allowed = {r.lower() for r in self.only_repos}
            self._repos = [
                Repo(it["name"], it.get("description") or "")
                for it in items
                if it.get("has_issues") and not it.get("archived") and (not allowed or it["name"].lower() in allowed)
            ]
            self._repos_at = time.monotonic()
        return self._repos

    async def resolve_repo(self, name: str) -> str:
        """Confere se o repositório é um dos liberados e devolve o nome com a grafia certa."""
        name = name.strip().removeprefix(f"{self.org}/")
        for repo in await self.list_repos():
            if repo.name.lower() == name.lower():
                return repo.name
        raise GitHubError(404, f"Repositório {name!r} não encontrado ou não liberado para o bot")

    async def list_labels(self, repo: str) -> list[Label]:
        resp = await self._request("GET", f"/repos/{self.org}/{repo}/labels", params={"per_page": 100})
        return [Label(lb["name"], lb.get("color", ""), lb.get("description") or "") for lb in resp.json()]

    # ---- leitura de issues -------------------------------------------------------

    def _scope(self) -> str:
        if self.only_repos:
            return " ".join(f"repo:{self.org}/{r}" for r in self.only_repos)
        return f"org:{self.org}"

    async def list_issues(self, state: State = "open", page: int = 1, per_page: int = 8) -> Page:
        # A busca filtra PRs no servidor e cobre todos os repositórios de uma vez
        # (uma issue recém-criada pode levar alguns segundos para aparecer).
        return await self.search("" if state == "all" else f"is:{state}", page, per_page, sort="updated")

    async def search(self, terms: str, page: int = 1, per_page: int = 8, sort: str | None = None) -> Page:
        params: dict[str, Any] = {"q": f"{self._scope()} is:issue {terms}".strip(), "per_page": per_page, "page": page}
        if sort:
            params |= {"sort": sort, "order": "desc"}
        data = (await self._request("GET", "/search/issues", params=params)).json()
        total = data.get("total_count", 0)
        reachable = min(total, 1000)  # a busca do GitHub para em 1000 resultados
        return Page(
            issues=[Issue.from_api(item) for item in data.get("items", [])],
            page=page,
            has_next=page * per_page < reachable,
            total=total,
        )

    async def get_issue(self, repo: str, number: int) -> Issue:
        repo = await self.resolve_repo(repo)
        data = (await self._request("GET", f"/repos/{self.org}/{repo}/issues/{number}")).json()
        if "pull_request" in data:
            raise GitHubError(404, f"{repo}#{number} é um pull request, não uma issue")
        return Issue.from_api(data)

    async def find_by_number(self, number: int) -> list[Issue]:
        """Todas as issues com esse número nos repositórios liberados (uma consulta GraphQL)."""
        repos = await self.list_repos()
        if not repos:
            return []
        parts = [
            f"r{i}: repository(owner: {json.dumps(self.org)}, name: {json.dumps(r.name)}) "
            f"{{ name issue(number: {int(number)}) {{ number title state stateReason url }} }}"
            for i, r in enumerate(repos)
        ]
        data = await self._graphql_raw("query {" + "\n".join(parts) + "}")
        found = []
        for node in (data.get("data") or {}).values():
            issue = (node or {}).get("issue")
            if issue:
                found.append(
                    Issue(
                        repo=node["name"], number=issue["number"], title=issue["title"],
                        state=issue["state"].lower(), state_reason=(issue.get("stateReason") or "").lower() or None,
                        author="", url=issue["url"], body="",
                    )
                )
        return found

    async def recent_comments(self, repo: str, number: int, limit: int = 3) -> list[Comment]:
        repo = await self.resolve_repo(repo)
        # Os comentários vêm em ordem cronológica; buscamos a última página.
        first = await self._request(
            "GET", f"/repos/{self.org}/{repo}/issues/{number}/comments", params={"per_page": limit}
        )
        last_url = first.links.get("last", {}).get("url")
        resp = await self._request("GET", last_url) if last_url else first
        return [
            Comment(
                author=(c.get("user") or {}).get("login", "?"),
                body=c.get("body") or "",
                created_at=_parse_dt(c["created_at"]),
                url=c["html_url"],
            )
            for c in resp.json()[-limit:]
        ]

    # ---- escrita (só criar issue e comentar) -------------------------------------

    async def create_issue(self, repo: str, title: str, body: str = "", labels: list[str] | None = None) -> Issue:
        repo = await self.resolve_repo(repo)
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        data = (await self._request("POST", f"/repos/{self.org}/{repo}/issues", json=payload)).json()
        return Issue.from_api(data)

    async def add_comment(self, repo: str, number: int, body: str) -> str:
        repo = await self.resolve_repo(repo)
        data = (
            await self._request(
                "POST", f"/repos/{self.org}/{repo}/issues/{number}/comments", json={"body": body}
            )
        ).json()
        return data["html_url"]

    # ---- GitHub Projects (v2, da organização) -------------------------------------

    async def _graphql_raw(self, query: str, **variables: Any) -> dict[str, Any]:
        return (await self._request("POST", "/graphql", json={"query": query, "variables": variables})).json()

    async def _graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        data = await self._graphql_raw(query, **variables)
        if data.get("errors"):
            err = data["errors"][0]
            msg = err.get("message", "erro desconhecido")
            if err.get("type") == "FORBIDDEN" or "not accessible" in msg:
                msg = "o token não tem permissão de Projects (Read and write) na organização"
            raise GitHubError(403 if err.get("type") == "FORBIDDEN" else 400, msg)
        return data["data"]

    async def get_project(self, owner: str, number: int) -> Project:
        node = (await self._graphql(_PROJECT_QUERY, owner=owner, number=number))["organization"]["projectV2"]
        if not node:
            raise GitHubError(404, f"Project {number} não encontrado em {owner}")
        status = node.get("field") or {}
        return Project(
            id=node["id"],
            title=node["title"],
            url=node["url"],
            status_field=status.get("id"),
            status_options={opt["name"]: opt["id"] for opt in status.get("options", [])},
        )

    async def add_to_project(self, project: Project, issue: Issue, status: str | None = None) -> str | None:
        """Coloca a issue no Project e, se der, define o Status. Devolve o Status aplicado."""
        data = await self._graphql(_ADD_ITEM, project=project.id, content=issue.node_id)
        item_id = data["addProjectV2ItemById"]["item"]["id"]
        option = project.status_options.get(status or "")
        if not (project.status_field and option):
            return None
        await self._graphql(_SET_STATUS, project=project.id, item=item_id, field=project.status_field, option=option)
        return status


_PROJECT_QUERY = """
query($owner: String!, $number: Int!) {
  organization(login: $owner) {
    projectV2(number: $number) {
      id title url
      field(name: "Status") { ... on ProjectV2SingleSelectField { id options { id name } } }
    }
  }
}"""

_ADD_ITEM = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
}"""

_SET_STATUS = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $project, itemId: $item, fieldId: $field, value: {singleSelectOptionId: $option}}
  ) { projectV2Item { id } }
}"""


def _error_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    msg = data.get("message") or resp.reason_phrase
    errors = data.get("errors") or []
    details = "; ".join(e.get("message", "") if isinstance(e, dict) else str(e) for e in errors)
    friendly = {
        401: "Token do GitHub inválido ou expirado",
        403: "Sem permissão (confira se o token tem Issues: Read and write neste repositório)",
        404: "Não encontrado",
        410: "As issues estão desativadas neste repositório",
    }.get(resp.status_code)
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        friendly = "Limite de requisições do GitHub atingido; tente de novo em alguns minutos"
    parts = [p for p in (friendly, msg if not friendly else None, details) if p]
    return " — ".join(parts)
