"""Guarda as imagens enviadas pelo Telegram num repositório do GitHub (ASSETS_REPO).

A API do GitHub não permite anexar arquivos em issues. Por isso a imagem vai para um
repositório (privado) só de anexos, e a issue recebe o link. Quem tem acesso a esse
repositório vê a imagem dentro da issue; quem não tem, não vê.

Usa um token próprio (GITHUB_TOKEN_ASSETS) com Contents: write só nesse repositório,
para o bot não ganhar permissão de escrever código nos demais.
"""

from __future__ import annotations

import base64
import secrets
from datetime import datetime, timezone

import httpx

from .github import API_URL, GitHubError, _error_message

_EXTENSIONS = {b"\xff\xd8\xff": "jpg", b"\x89PNG": "png", b"RIFF": "webp", b"GIF8": "gif"}


def image_extension(data: bytes) -> str | None:
    return next((ext for magic, ext in _EXTENSIONS.items() if data.startswith(magic)), None)


class AssetStore:
    def __init__(self, token: str, org: str, repo: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.org = org
        self.repo = repo
        self._branch: str | None = None
        self._http = httpx.AsyncClient(
            base_url=API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "bot-telegram-issues",
            },
            timeout=60,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubError(0, f"Falha de conexão com o GitHub: {exc}") from exc
        if resp.is_error:
            raise GitHubError(resp.status_code, _error_message(resp))
        return resp

    async def upload_image(self, data: bytes, folder: str) -> str:
        """Salva a imagem em <folder>/<ano-mês>/<aleatório>.<ext> e devolve o link para a issue."""
        ext = image_extension(data)
        if not ext:
            raise GitHubError(415, "Formato de imagem não suportado")
        if self._branch is None:
            self._branch = (await self._request("GET", f"/repos/{self.org}/{self.repo}")).json()["default_branch"]
        now = datetime.now(timezone.utc)
        path = f"{folder}/{now:%Y-%m}/{now:%d-%H%M%S}-{secrets.token_hex(4)}.{ext}"
        await self._request(
            "PUT",
            f"/repos/{self.org}/{self.repo}/contents/{path}",
            json={"message": f"Imagem de issue ({folder})", "content": base64.b64encode(data).decode()},
        )
        # Link no github.com (e não no raw.githubusercontent): o navegador de quem está logado
        # e tem acesso ao repositório carrega a imagem direto dentro da issue.
        return f"https://github.com/{self.org}/{self.repo}/raw/{self._branch}/{path}"
