"""Teclados inline (botões) usados nas mensagens.

Formato do callback_data (limite de 64 bytes do Telegram):
    l:<state>:<page>     lista de issues          s:<page>      página da busca
    v:<repo>:<n>         abre a issue numa mensagem nova
    v:<repo>:<n>:e       atualiza a issue na mesma mensagem
    c:<repo>:<n>         comentar
    n:...                passos da criação de issue (/nova)
        n:r:<repo> escolhe o repositório · n:rp:<page> página da lista de repositórios
"""

from __future__ import annotations

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup

from .formatting import state_icon, truncate
from .github import Issue, Repo

STATE_BUTTONS = (("open", "🟢 Abertas"), ("closed", "🟣 Fechadas"), ("all", "📚 Todas"))
REPOS_PER_PAGE = 10
MAX_CALLBACK = 64


def fits(data: str) -> bool:
    return len(data.encode()) <= MAX_CALLBACK


def _issue_button(issue: Issue) -> list[Btn]:
    data = f"v:{issue.repo}:{issue.number}"
    label = f"{state_icon(issue)} {issue.ref} · {truncate(issue.title, 36)}"
    return [Btn(label, callback_data=data) if fits(data) else Btn(label, url=issue.url)]


def issue_list(issues: list[Issue], nav_prefix: str, page: int, has_next: bool, state: str | None = None) -> Markup:
    rows = [_issue_button(i) for i in issues]
    nav = []
    if page > 1:
        nav.append(Btn("◀️ Anterior", callback_data=f"{nav_prefix}:{page - 1}"))
    if has_next:
        nav.append(Btn("Próxima ▶️", callback_data=f"{nav_prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    if state:
        rows.append(
            [Btn(("· " if s == state else "") + label, callback_data=f"l:{s}:1") for s, label in STATE_BUTTONS]
        )
    return Markup(rows)


def issue_choices(issues: list[Issue]) -> Markup:
    """Quando '#42' existe em mais de um repositório."""
    return Markup([_issue_button(i) for i in issues])


def issue_actions(issue: Issue) -> Markup:
    key = f"{issue.repo}:{issue.number}"
    buttons = [Btn("💬 Comentar", callback_data=f"c:{key}"), Btn("🔃 Atualizar", callback_data=f"v:{key}:e")]
    return Markup([[b for b in buttons if fits(b.callback_data)], [Btn("🔗 Abrir no GitHub", url=issue.url)]])


def open_issue(issue: Issue, project_url: str | None = None) -> Markup:
    data = f"v:{issue.repo}:{issue.number}"
    rows = [[Btn("🔗 Abrir no GitHub", url=issue.url)]]
    if fits(data):
        rows[0].append(Btn("👀 Ver detalhes", callback_data=data))
    if project_url:
        rows.append([Btn("📋 Ver no Project", url=project_url)])
    return Markup(rows)


def view_issue(repo: str, number: int) -> Markup | None:
    data = f"v:{repo}:{number}"
    return Markup([[Btn(f"👀 Ver {repo}#{number}", callback_data=data)]]) if fits(data) else None


# ---- criação de issue (/nova) -------------------------------------------------


def repo_picker(repos: list[Repo], page: int = 1, favorite: str | None = None) -> Markup:
    """Lista paginada de repositórios; o último usado pela pessoa vem primeiro, com ⭐."""
    ordered = sorted(repos, key=lambda r: r.name != favorite)  # sort estável: mantém a ordem por atividade
    start = (page - 1) * REPOS_PER_PAGE
    shown = [r for r in ordered[start : start + REPOS_PER_PAGE] if fits(f"n:r:{r.name}")]
    buttons = [
        Btn(("⭐ " if r.name == favorite else "📦 ") + truncate(r.name, 28), callback_data=f"n:r:{r.name}")
        for r in shown
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    nav = []
    if page > 1:
        nav.append(Btn("◀️ Anterior", callback_data=f"n:rp:{page - 1}"))
    if start + REPOS_PER_PAGE < len(ordered):
        nav.append(Btn("Mais repositórios ▶️", callback_data=f"n:rp:{page + 1}"))
    if nav:
        rows.append(nav)
    return Markup(rows)


def draft_labels(options: list[str], selected: list[str]) -> Markup:
    buttons = [
        Btn(("✅ " if name in selected else "▫️ ") + truncate(name, 24), callback_data=f"n:l:{idx}")
        for idx, name in enumerate(options)
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([Btn("Continuar ➡️", callback_data="n:ok")])
    return Markup(rows)


def draft_confirm() -> Markup:
    return Markup(
        [
            [Btn("✅ Criar issue", callback_data="n:create")],
            [
                Btn("✏️ Título", callback_data="n:e:title"),
                Btn("✏️ Descrição", callback_data="n:e:body"),
            ],
            [
                Btn("🏷️ Labels", callback_data="n:e:labels"),
                Btn("📦 Repositório", callback_data="n:e:repo"),
            ],
            [Btn("❌ Cancelar", callback_data="n:cancel")],
        ]
    )
