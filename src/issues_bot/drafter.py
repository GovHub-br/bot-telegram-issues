"""Cliente do endpoint Hayhooks `issue_draft` (relato livre → rascunho de issue)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import base64

import httpx

from .people import Person


class DraftUnavailable(RuntimeError):
    """O assistente não respondeu ou não conseguiu montar o rascunho; o bot cai no modo manual."""


@dataclass
class Similar:
    repo: str
    number: int
    title: str
    state: str
    url: str

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass
class Draft:
    title: str
    body: str
    labels: list[str]
    duplicate_of: tuple[str, int] | None = None  # (repo, número)
    similar: list[Similar] = field(default_factory=list)
    model: str = ""
    template: str | None = None  # nome do template de issue usado
    assignee: Person | None = None  # responsável pedido no relato, se o Agent achou
    assignee_options: list[Person] = field(default_factory=list)  # candidatos para trocar

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Draft:
        return cls(
            title=data["title"],
            body=data.get("body", ""),
            labels=list(data.get("labels") or []),
            duplicate_of=(dup["repo"], dup["number"]) if (dup := data.get("duplicada_de")) else None,
            similar=[
                Similar(s["repo"], s["number"], s["title"], s["state"], s["url"]) for s in data.get("similares") or []
            ],
            model=data.get("modelo", ""),
            template=data.get("template"),
            assignee=Person(who["login"], who.get("name") or "") if (who := data.get("responsavel")) else None,
            assignee_options=[Person(c["login"], c.get("name") or "") for c in data.get("candidatos_responsavel") or []],
        )


class DrafterClient:
    def __init__(self, url: str, *, timeout: float = 150, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def draft(self, text: str, repo: str, images: list[bytes] | None = None) -> Draft:
        payload = {"texto": text, "repo": repo, "imagens": [base64.b64encode(i).decode() for i in images or []]}
        try:
            resp = await self._http.post("/issue_draft/run", json=payload)
            resp.raise_for_status()
            result = resp.json()["result"]
        except httpx.TimeoutException as exc:
            raise DraftUnavailable("o assistente demorou demais para responder") from exc
        except httpx.HTTPStatusError as exc:
            raise DraftUnavailable(f"o assistente respondeu com erro {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise DraftUnavailable("não consegui falar com o assistente") from exc
        except (ValueError, KeyError) as exc:
            raise DraftUnavailable("o assistente devolveu uma resposta inesperada") from exc

        if "erro" in result:
            raise DraftUnavailable(result["erro"])
        try:
            return Draft.from_api(result)
        except (KeyError, TypeError) as exc:
            raise DraftUnavailable("o assistente devolveu um rascunho incompleto") from exc
