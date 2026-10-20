"""Configuração do serviço de rascunho, lida do ambiente (o mesmo .env do bot)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _url(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    return raw if raw.startswith(("http://", "https://")) else f"http://{raw}"


@dataclass(frozen=True)
class DrafterSettings:
    github_token: str
    github_org: str
    ollama_url: str
    ollama_model: str
    ollama_timeout: int = 120
    default_labels: tuple[str, ...] = ()
    max_agent_steps: int = 5
    github_repos: tuple[str, ...] = ()  # vazio = todos os repositórios da organização

    @classmethod
    def from_env(cls) -> DrafterSettings:
        missing = [k for k in ("GITHUB_TOKEN", "GITHUB_ORG", "OLLAMA_URL", "OLLAMA_MODEL") if not os.getenv(k)]
        if missing:
            raise RuntimeError(f"Variáveis faltando para o issue_draft: {', '.join(missing)}")
        return cls(
            github_token=os.environ["GITHUB_TOKEN"].strip(),
            github_org=os.environ["GITHUB_ORG"].strip(),
            github_repos=tuple(r.strip() for r in os.getenv("GITHUB_REPOS", "").split(",") if r.strip()),
            ollama_url=_url(os.environ["OLLAMA_URL"]),
            ollama_model=os.environ["OLLAMA_MODEL"].strip(),
            ollama_timeout=int(os.getenv("OLLAMA_TIMEOUT", "120")),
            default_labels=tuple(lb.strip() for lb in os.getenv("DEFAULT_LABELS", "").split(",") if lb.strip()),
            max_agent_steps=int(os.getenv("DRAFT_MAX_STEPS", "5")),
        )
