"""Leitura das configurações a partir do ambiente (ou do arquivo .env)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

_NAME_RE = re.compile(r"^[\w.-]+$")


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    github_token: str
    github_org: str
    allowed_users: frozenset[int]
    github_repos: tuple[str, ...] = ()  # vazio = todos os repositórios da organização
    allowed_chats: frozenset[int] = frozenset()
    allowed_usernames: frozenset[str] = frozenset()  # usernames do Telegram, em minúsculas
    allow_everyone: bool = False
    default_labels: tuple[str, ...] = ()
    hayhooks_url: str | None = None
    project: tuple[str, int] | None = None  # (organização, número) do GitHub Project
    project_status: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()

        missing = [k for k in ("TELEGRAM_TOKEN", "GITHUB_TOKEN", "GITHUB_ORG") if not os.getenv(k)]
        if missing:
            raise ConfigError(f"Variáveis faltando no .env: {', '.join(missing)}")

        org = os.environ["GITHUB_ORG"].strip()
        if not _NAME_RE.match(org):
            raise ConfigError(f"GITHUB_ORG deve ser só o nome da organização (recebido: {org!r})")
        repos = tuple(r.strip().removeprefix(f"{org}/") for r in os.getenv("GITHUB_REPOS", "").split(",") if r.strip())
        if bad := [r for r in repos if not _NAME_RE.match(r)]:
            raise ConfigError(f"GITHUB_REPOS tem nomes inválidos: {', '.join(bad)}")

        users = os.getenv("ALLOWED_USERS", "").strip()
        user_ids, usernames = (frozenset(), frozenset()) if users == "*" else parse_users(users)
        return cls(
            telegram_token=os.environ["TELEGRAM_TOKEN"].strip(),
            github_token=os.environ["GITHUB_TOKEN"].strip(),
            github_org=org,
            github_repos=repos,
            allow_everyone=users == "*",
            allowed_users=user_ids,
            allowed_usernames=usernames,
            allowed_chats=parse_ids(os.getenv("ALLOWED_CHATS", ""), "ALLOWED_CHATS"),
            default_labels=tuple(lb.strip() for lb in os.getenv("DEFAULT_LABELS", "").split(",") if lb.strip()),
            hayhooks_url=_url(os.getenv("HAYHOOKS_URL", "")),
            project=parse_project(os.getenv("GITHUB_PROJECT", "")),
            project_status=os.getenv("PROJECT_STATUS", "").strip() or None,
        )

    def is_allowed(self, user_id: int | None, chat_id: int | None, username: str | None = None) -> bool:
        return (
            self.allow_everyone
            or (user_id is not None and user_id in self.allowed_users)
            or (username is not None and username.lower() in self.allowed_usernames)
            or (chat_id is not None and chat_id in self.allowed_chats)
        )


_USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")


def parse_users(raw: str) -> tuple[frozenset[int], frozenset[str]]:
    """'123, @Fulano beltrano' -> ({123}, {'fulano', 'beltrano'}): IDs ou usernames do Telegram."""
    ids, names = set(), set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        if part.lstrip("-").isdigit():
            ids.add(int(part))
        elif m := _USERNAME_RE.match(part):
            names.add(m.group(1).lower())
        else:
            raise ConfigError(f"ALLOWED_USERS contém um valor inválido: {part!r}")
    return frozenset(ids), frozenset(names)


def parse_project(raw: str) -> tuple[str, int] | None:
    """'GovHub-br/4' ou 'https://github.com/orgs/GovHub-br/projects/4' -> ('GovHub-br', 4)."""
    raw = raw.strip().rstrip("/")
    if not raw:
        return None
    m = re.fullmatch(r"(?:https://github\.com/orgs/)?([\w.-]+)/(?:projects/)?(\d+)", raw)
    if not m:
        raise ConfigError(f"GITHUB_PROJECT deve ser org/número ou a URL do Project (recebido: {raw!r})")
    return m.group(1), int(m.group(2))


def _url(raw: str) -> str | None:
    raw = raw.strip().rstrip("/")
    if not raw:
        return None
    return raw if raw.startswith(("http://", "https://")) else f"http://{raw}"


def parse_ids(raw: str, name: str = "ALLOWED_USERS") -> frozenset[int]:
    """'123, 456 789' -> {123, 456, 789}. Aceita vírgula e/ou espaço como separador."""
    ids = set()
    for part in re.split(r"[,\s]+", raw.strip()):
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"{name} contém um valor inválido: {part!r}") from exc
    return frozenset(ids)
