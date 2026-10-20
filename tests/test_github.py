import json

import httpx
import pytest

from issues_bot.github import GitHubClient, GitHubError

REPOS = [
    {"name": "rag", "has_issues": True, "archived": False, "description": "GraphRAG"},
    {"name": "web", "has_issues": True, "archived": False},
    {"name": "velho", "has_issues": True, "archived": True},
    {"name": "sem-issues", "has_issues": False, "archived": False},
]


def issue_json(number, *, repo="rag", pr=False, **kw):
    data = {
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "html_url": f"https://github.com/o/{repo}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/o/{repo}",
        "user": {"login": "ana"},
        "labels": [{"name": "bug"}],
        "assignees": [],
        "comments": 0,
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-02T10:00:00Z",
    } | kw
    if pr:
        data["pull_request"] = {}
    return data


def transport_with(handler) -> httpx.MockTransport:
    def with_repos(req: httpx.Request):
        if req.url.path == "/orgs/o/repos":
            return httpx.Response(200, json=REPOS)
        return handler(req)

    return httpx.MockTransport(with_repos)


def client_with(handler, only_repos=()) -> GitHubClient:
    return GitHubClient("tkn", "o", only_repos, transport=transport_with(handler))


async def test_list_repos_skips_archived_and_without_issues():
    assert [r.name for r in await client_with(lambda req: httpx.Response(404)).list_repos()] == ["rag", "web"]
    only_web = GitHubClient("t", "o", ("WEB",), transport=transport_with(lambda req: httpx.Response(404)))
    assert [r.name for r in await only_web.list_repos()] == ["web"]


async def test_list_repos_follows_pagination():
    def handler(req: httpx.Request):
        if req.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"name": "b", "has_issues": True}])
        return httpx.Response(
            200,
            json=[{"name": "a", "has_issues": True}],
            headers={"Link": '<https://api.github.com/orgs/o/repos?page=2>; rel="next"'},
        )

    client = GitHubClient("t", "o", transport=httpx.MockTransport(handler))
    assert [r.name for r in await client.list_repos()] == ["a", "b"]


async def test_list_issues_searches_the_whole_org():
    def handler(req: httpx.Request):
        assert req.url.path == "/search/issues"
        assert req.url.params["q"] == "org:o is:issue is:closed"
        assert req.url.params["sort"] == "updated"
        assert req.headers["Authorization"] == "Bearer tkn"
        return httpx.Response(200, json={"total_count": 20, "items": [issue_json(1), issue_json(3, repo="web")]})

    page = await client_with(handler).list_issues("closed", page=2, per_page=8)
    assert [i.ref for i in page.issues] == ["rag#1", "web#3"]
    assert page.has_next and page.total == 20


async def test_search_restricted_to_some_repos():
    def handler(req: httpx.Request):
        assert req.url.params["q"] == "repo:o/rag repo:o/web is:issue login quebrado"
        return httpx.Response(200, json={"total_count": 9, "items": [issue_json(5)]})

    page = await client_with(handler, ("rag", "web")).search("login quebrado", page=1, per_page=8)
    assert page.total == 9 and page.has_next


async def test_create_issue_in_the_chosen_repo():
    def handler(req: httpx.Request):
        assert req.method == "POST" and req.url.path == "/repos/o/web/issues"
        assert json.loads(req.content) == {"title": "T", "body": "B", "labels": ["bug"]}
        return httpx.Response(201, json=issue_json(10, repo="web", title="T"))

    issue = await client_with(handler).create_issue("WEB", "T", "B", ["bug"])
    assert issue.ref == "web#10" and issue.labels == ["bug"]


async def test_unknown_or_archived_repo_is_refused():
    client = client_with(lambda req: pytest.fail("não deveria chamar a API"))
    for name in ("outro", "velho"):
        with pytest.raises(GitHubError, match="não liberado"):
            await client.create_issue(name, "T")


async def test_get_issue_rejects_pull_request():
    client = client_with(lambda req: httpx.Response(200, json=issue_json(4, pr=True)))
    with pytest.raises(GitHubError, match="pull request"):
        await client.get_issue("rag", 4)


async def test_find_by_number_queries_every_repo_in_one_graphql_call():
    calls = []

    def handler(req: httpx.Request):
        query = json.loads(req.content)["query"]
        calls.append(query)
        assert 'name: "rag"' in query and 'name: "web"' in query and "issue(number: 7)" in query
        return httpx.Response(200, json={
            "data": {
                "r0": {"name": "rag", "issue": {"number": 7, "title": "A", "state": "OPEN", "stateReason": None,
                                                "url": "u"}},
                "r1": None,  # o 7 de web é um PR: o GraphQL devolve erro só para esse
            },
            "errors": [{"type": "NOT_FOUND", "path": ["r1", "issue"]}],
        })

    found = await client_with(handler).find_by_number(7)
    assert len(calls) == 1
    assert [(i.ref, i.state) for i in found] == [("rag#7", "open")]


async def test_recent_comments_fetches_last_page():
    def handler(req: httpx.Request):
        comment = lambda n: {  # noqa: E731
            "user": {"login": "bob"},
            "body": f"c{n}",
            "created_at": "2026-10-01T10:00:00Z",
            "html_url": "u",
        }
        if req.url.params.get("page") == "4":
            return httpx.Response(200, json=[comment(10), comment(11)])
        return httpx.Response(
            200,
            json=[comment(1), comment(2), comment(3)],
            headers={"Link": '<https://api.github.com/repos/o/rag/issues/1/comments?per_page=3&page=4>; rel="last"'},
        )

    comments = await client_with(handler).recent_comments("rag", 1)
    assert [c.body for c in comments] == ["c10", "c11"]


@pytest.mark.parametrize(
    "status, expected",
    [(401, "Token do GitHub inválido"), (403, "Sem permissão"), (404, "Não encontrado")],
)
async def test_errors_are_friendly(status, expected):
    client = client_with(lambda req: httpx.Response(status, json={"message": "nope"}))
    with pytest.raises(GitHubError, match=expected) as info:
        await client.get_issue("rag", 1)
    assert info.value.status == status
