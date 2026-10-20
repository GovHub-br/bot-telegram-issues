"""Formatação das mensagens (HTML do Telegram) e utilitários de texto."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape

from .github import Comment, Issue

# Limite de uma mensagem do Telegram é 4096 caracteres; deixamos folga para o HTML.
BODY_PREVIEW = 1500
COMMENT_PREVIEW = 300

STATE_FILTERS = {
    "abertas": "open",
    "fechadas": "closed",
    "todas": "all",
}
STATE_TITLES = {"open": "abertas", "closed": "fechadas", "all": "todas"}


def state_icon(issue: Issue) -> str:
    if issue.is_open:
        return "🟢"
    return "⚪" if issue.state_reason == "not_planned" else "🟣"


def state_text(issue: Issue) -> str:
    if issue.is_open:
        return "Aberta"
    return "Fechada · não planejada" if issue.state_reason == "not_planned" else "Fechada · concluída"


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def strip_markdown_noise(text: str) -> str:
    """Remove o que não faz sentido no Telegram: comentários HTML e excesso de linhas vazias."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def ago(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "—"
    now = now or datetime.now(timezone.utc)
    secs = int((now - dt).total_seconds())
    if secs < 60:
        return "agora há pouco"
    for size, one, many in (
        (60 * 60 * 24 * 365, "ano", "anos"),
        (60 * 60 * 24 * 30, "mês", "meses"),
        (60 * 60 * 24 * 7, "semana", "semanas"),
        (60 * 60 * 24, "dia", "dias"),
        (60 * 60, "hora", "horas"),
        (60, "minuto", "minutos"),
    ):
        if secs >= size:
            n = secs // size
            return f"há {n} {one if n == 1 else many}"
    return "agora há pouco"


def issue_line(issue: Issue) -> str:
    labels = "".join(f" <code>{escape(lb)}</code>" for lb in issue.labels[:3])
    comments = f" · 💬 {issue.comments}" if issue.comments else ""
    return (
        f"{state_icon(issue)} <a href=\"{issue.url}\"><b>{escape(issue.ref)}</b></a> "
        f"{escape(truncate(issue.title, 80))}{labels}\n"
        f"      <i>@{escape(issue.author)} · {ago(issue.updated_at)}{comments}</i>"
    )


def issue_list(title: str, issues: list[Issue], page: int, subtitle: str = "") -> str:
    head = f"📋 <b>{escape(title)}</b>"
    if subtitle:
        head += f"\n<i>{escape(subtitle)}</i>"
    if not issues:
        return f"{head}\n\nNenhuma issue encontrada. 🍃"
    page_info = f"\n\n<i>Página {page}</i>" if page > 1 else ""
    return head + "\n\n" + "\n\n".join(issue_line(i) for i in issues) + page_info


def issue_detail(issue: Issue, comments: list[Comment] | None = None) -> str:
    # O escape de HTML pode inflar o texto; se estourar o limite, encurtamos os trechos.
    for body_limit, comment_limit in ((BODY_PREVIEW, COMMENT_PREVIEW), (700, 150), (200, 60)):
        text = _issue_detail(issue, comments, body_limit, comment_limit)
        if len(text) <= 4096:
            return text
    return text[:4000] + "…"


def _issue_detail(issue: Issue, comments: list[Comment] | None, body_limit: int, comment_limit: int) -> str:
    lines = [
        f"{state_icon(issue)} <b>{escape(issue.title)}</b>",
        f'📦 <a href="{issue.url}">{escape(issue.ref)}</a>',
        "",
        f"<b>Status:</b> {state_text(issue)}",
        f"<b>Autor:</b> @{escape(issue.author)} · aberta {ago(issue.created_at)}",
    ]
    if issue.labels:
        lines.append("<b>Labels:</b> " + " ".join(f"<code>{escape(lb)}</code>" for lb in issue.labels))
    if issue.assignees:
        lines.append("<b>Responsáveis:</b> " + ", ".join(f"@{escape(a)}" for a in issue.assignees))
    if not issue.is_open and issue.closed_at:
        lines.append(f"<b>Fechada:</b> {ago(issue.closed_at)}")
    lines.append(f"<b>Atualizada:</b> {ago(issue.updated_at)}")

    body = strip_markdown_noise(issue.body)
    lines += ["", f"<blockquote expandable>{escape(truncate(body, body_limit))}</blockquote>" if body else "<i>Sem descrição.</i>"]

    if comments:
        extra = issue.comments - len(comments)
        header = f"💬 <b>Comentários</b> ({issue.comments})"
        if extra > 0:
            header += f" · <i>mostrando os {len(comments)} mais recentes</i>"
        lines += ["", header]
        for c in comments:
            text = escape(truncate(strip_markdown_noise(c.body), comment_limit))
            lines.append(f"\n<b>@{escape(c.author)}</b> <i>{ago(c.created_at)}</i>\n<blockquote>{text}</blockquote>")

    return "\n".join(lines)


def telegram_signature(full_name: str, username: str | None, note: str | None = None) -> str:
    who = f"{full_name} (@{username})" if username else full_name
    extra = f" · {note}" if note else ""
    return f"\n\n---\n<sub>Enviado via Telegram por {who}{extra}</sub>"


def split_title_body(text: str) -> tuple[str, str]:
    """'Título | descrição' -> ('Título', 'descrição'). Sem '|', tudo vira título."""
    title, _, body = text.partition("|")
    return title.strip(), body.strip()


def parse_number(text: str) -> int | None:
    """Aceita '42' ou '#42'."""
    m = re.fullmatch(r"#?(\d+)", text.strip())
    return int(m.group(1)) if m else None


_REF_RE = re.compile(
    r"(?:https://github\.com/[\w.-]+/(?P<url_repo>[\w.-]+)/issues/(?P<url_n>\d+)\S*"
    r"|(?:[\w.-]+/)?(?P<repo>[\w.-]+)?(?:#|\s+#?)(?P<n>\d+)"
    r"|#?(?P<bare>\d+))"
)


def parse_ref(text: str) -> tuple[str | None, int] | None:
    """Referência a uma issue: 'repo#42', 'org/repo#42', 'repo 42', '#42', '42' ou a URL.
    Devolve (repo ou None, número)."""
    m = _REF_RE.fullmatch(text.strip())
    if not m:
        return None
    if m["url_n"]:
        return m["url_repo"], int(m["url_n"])
    if m["n"]:
        return m["repo"], int(m["n"])
    return None, int(m["bare"])
