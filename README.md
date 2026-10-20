# 🤖 bot-telegram-issues

Bot do Telegram para **criar, consultar e comentar issues** nos repositórios de uma organização do GitHub (hoje a `GovHub-br`), sem sair do chat.

Para criar issues, a pessoa descreve o problema do jeito dela. Um **Agent do Haystack** servido pelo **Hayhooks** monta o rascunho (título, descrição estruturada e labels), procura **issues parecidas ou duplicadas** e devolve tudo para revisão. Quem cria a issue é sempre a pessoa, no botão.

```
/issues                 🟢 gov-hub#42 Erro no login             ← issues de todos os repositórios,
                        🟢 graphrag-tais#41 Ajustar cron        com botões para abrir cada uma
                        [◀️ Anterior] [Próxima ▶️]
                        [· 🟢 Abertas] [🟣 Fechadas] [📚 Todas]

gov-hub#42              🟢 Erro no login
                        📦 gov-hub#42
                        Status: Aberta · Autor: @ana · aberta há 2 dias
                        ┃ Passos para reproduzir…
                        💬 Comentários (5) · mostrando os 3 mais recentes
                        [💬 Comentar] [🔃 Atualizar]
                        [🔗 Abrir no GitHub]
```

## Comandos

| Comando | O que faz |
|---|---|
| `/issues` · `/issues fechadas` · `/issues todas` | Lista as issues de **todos os repositórios**, 8 por página, com botões |
| `/issue gov-hub#42`, ou só `gov-hub#42`, `#42` ou o link | Detalhes, labels, responsáveis e últimos comentários. Um `#42` sem repositório é procurado em todos; se existir em mais de um, o bot pergunta qual |
| `/buscar termo` | Busca no título, descrição e comentários (aceita a [sintaxe de busca do GitHub](https://docs.github.com/pt/search-github/searching-on-github/searching-issues-and-pull-requests), ex.: `/buscar label:bug author:ana`) |
| `/nova` | Escolha o **repositório** (o último que você usou vem primeiro, com ⭐), descreva o problema ou a ideia e o assistente monta o rascunho e aponta duplicadas na organização toda. Depois: labels → revisão (dá para editar título, descrição, labels e repositório) → criar |
| `/nova texto livre` | O mesmo, sem a pergunta inicial |
| `/nova Título \| descrição` | Já preenche título e descrição; só falta escolher a label |
| `/comentar gov-hub#42 texto` | Comenta na issue (sem o texto, o bot pergunta) |
| `/cancelar` | Interrompe a criação ou o comentário em andamento |

Toda issue nova entra no **GitHub Project** da organização (`GITHUB_PROJECT`, hoje o [Gov Hub - Project Management](https://github.com/orgs/GovHub-br/projects/4)) já na coluna `PROJECT_STATUS` (`Project Backlog`). Se isso falhar, a issue é criada do mesmo jeito e o bot avisa o motivo.

Toda issue nova precisa de **pelo menos uma label**. As de `DEFAULT_LABELS` (hoje `arquitetura`) já vêm marcadas.

Pelo botão da issue também dá para comentar.

**O bot só cria e comenta.** Por segurança, ele não fecha, reabre, edita nem apaga nada. Para corrigir um erro, use a interface do GitHub.

Tudo o que o bot escreve no GitHub (issues e comentários) leva uma assinatura dizendo **quem mandou pelo Telegram**, já que todas as ações usam o mesmo token.

## Rascunho assistido (Haystack + Hayhooks)

```
Telegram ──/nova → 📦 repo → "o retriever erra a coluna…"──► bot ──POST /issue_draft/run {texto, repo}──► Hayhooks
                                                                                   │
                                     Agent do Haystack (OllamaChatGenerator, qwen3.8:27b-mlx)
                                       ├─ buscar_issues(termos)   → GitHub Search na organização toda (só leitura)
                                       ├─ ler_template(nome)      → modelo de issue do repositório (.github/ISSUE_TEMPLATE)
                                       ├─ buscar_pessoa(nome)     → quem pode ser responsável, por semelhança de nome
                                       │  (+ imagens do relato, lidas pelo modelo com visão)
                                       └─ propor_issue(title, body, labels, duplicada_de)  ← saída
                                                                                   │
bot ◄── {title, body, labels, duplicada_de, similares} ◄──────────────────────────┘
 └─ labels (sugeridas + padrão marcadas) → revisão → [✅ Criar issue]
```

- **Por que um Agent:** ele decide os termos da busca de duplicadas, cita as relacionadas no corpo e só termina ao chamar `propor_issue`. O Agent **não cria nem altera** nada no GitHub; as tools dele só leem.
- **Por que tool calling para a saída:** os modelos MLX do Ollama recusam saída estruturada (`format`/JSON schema). Por isso o rascunho chega como argumentos da tool `propor_issue`, com as labels restritas por `enum` às que existem **no repositório escolhido**. A busca de duplicadas cobre todos os repositórios, porque o Project junta todos.
- **Templates de issue:** o Agent escolhe o modelo que combina com o relato (DAG, dbt, extração, dashboard…), lê o modelo e preenche todas as seções. Campos que o relato não informa ficam como `a definir`. Ele usa os templates do próprio repositório escolhido; se o repositório não tiver, usa os de `ISSUE_TEMPLATES_REPO` (hoje os 14 do `data-application-cidades`). Os templates são lidos do GitHub e ficam em cache por 10 minutos, então editar um template no GitHub já muda o bot.
- **Responsável por nome:** escreva no relato algo como "deixe como responsável o lucas botino". O Agent procura, entre quem pode ser responsável no repositório, o nome mais parecido. A comparação ignora acentos e maiúsculas e olha o nome do perfil e o login, então o exemplo acha *Lucas Bottino (@bottinolucas)*. A revisão mostra **👤 Responsável**, e o botão permite trocar por outro candidato, procurar outro nome ou deixar sem ninguém. Se o nome for ambíguo (por exemplo, só "lucas"), o Agent não escolhe: mostra os candidatos para você decidir. Um responsável só é aceito se tiver saído da busca, então o modelo não consegue inventar alguém.
- **Prints e fotos:** no `/nova`, mande uma foto (com ou sem legenda) em vez de texto. O modelo (`qwen3.8:27b-mlx`, que tem visão) lê a imagem e usa nomes de DAG, mensagens de erro, tabelas e números para preencher o template. A imagem é anexada à issue, numa seção "Imagens" no fim. A API do GitHub não aceita anexos em issues, então a imagem vai para o repositório privado `ASSETS_REPO` (`GovHub-br/issue-assets`) e a issue recebe o link. Quem tem acesso a esse repositório vê a imagem dentro da issue; quem não tem, não vê. O envio usa um token próprio (`GITHUB_TOKEN_ASSETS`) com escrita só nesse repositório, e a imagem só é salva quando você confirma a criação. Em álbum, só a primeira foto é usada; o limite é de 8 MB por imagem.
- **Fallback:** se o Hayhooks estiver fora do ar, demorar ou o modelo não propuser a issue, o bot avisa e segue no **modo manual**, e o texto da pessoa vira a descrição. Com `HAYHOOKS_URL` vazio, o `/nova` é sempre manual.
- **Tempo:** cerca de 15 a 20 s por rascunho com o `qwen3.8:27b-mlx` (`think` desligado). O bot atende outras mensagens enquanto isso.
- **Open WebUI:** o mesmo endpoint responde no contrato OpenAI-compatível (modelo `issue_draft`) com o rascunho em Markdown.

O serviço fica em [`hayhooks/`](hayhooks): o pacote `issue_drafter` tem o Agent, as tools e o prompt em `prompts/system.md`, e `pipelines/issue_draft/pipeline_wrapper.py` é a ponte com o Hayhooks. O formato segue o do `graphrag-tais`.

```bash
curl -s localhost:1416/issue_draft/run -H 'Content-Type: application/json' \
  -d '{"texto": "quando a pergunta tem acento o embedding esparso não acha a coluna", "repo": "graphrag-tais"}'
```

## Configuração

### 1. Bot no Telegram

1. No [@BotFather](https://t.me/BotFather): `/newbot` e guarde o token.
2. Para usar em grupo, `/setprivacy` → **Disable** faz o bot enxergar o atalho `#42` em qualquer mensagem. Com o privacy ligado, os comandos e as respostas ao bot continuam funcionando.

O menu de comandos (o botão `/` do chat) é registrado automaticamente quando o bot sobe.

### 2. Token do GitHub

Em **Settings → Developer settings → Fine-grained tokens → Generate new token**:

- **Resource owner:** `GovHub-br`
- **Repository access:** *All repositories* (ou *Only select repositories* com os repositórios que o bot vai usar)
- **Permissions → Repository → Issues:** *Read and write* (Metadata: Read-only vem junto)
- **Permissions → Organizations → Projects:** *Read and write* (para colocar a issue no Project). Na tela de permissões, troque para a aba **Organizations** e use **+ Add permissions**.

### 3. `.env`

```bash
cp .env.example .env
```

| Variável | Exemplo |
|---|---|
| `TELEGRAM_TOKEN` | `123456789:AAH…` |
| `GITHUB_TOKEN` | `github_pat_…` |
| `GITHUB_ORG` | `GovHub-br` |
| `GITHUB_REPOS` | vazio = todos os repositórios com issues ativadas (não arquivados); ou `gov-hub,graphrag-tais` para limitar |
| `ALLOWED_CHATS` | `-1001234567890`: grupo(s) do time; todos os membros usam o bot lá dentro |
| `ALLOWED_USERS` | IDs ou usernames do Telegram (`123456789`, `@fulano`), separados por vírgula; `*` libera qualquer pessoa. Hoje: `arthrok` |
| `GITHUB_PROJECT` | `GovHub-br/4` ou a URL do Project |
| `PROJECT_STATUS` | `Project Backlog` (coluna inicial no Project) |
| `DEFAULT_LABELS` | `arquitetura` (labels já marcadas na criação) |
| `ISSUE_TEMPLATES_REPO` | `data-application-cidades` (templates usados quando o repositório escolhido não tem os seus) |
| `ASSETS_REPO` / `GITHUB_TOKEN_ASSETS` | `issue-assets` e um token fine-grained com *Contents: Read and write* só nesse repositório (vazio = imagens só ajudam no rascunho) |
| `HAYHOOKS_URL` | `http://localhost:1416` (vazio = `/nova` só manual; no compose vira `http://hayhooks:1416`) |
| `OLLAMA_URL` | `http://10.0.0.10:11434` (usado pelo Hayhooks) |
| `OLLAMA_MODEL` | `qwen3.8:27b-mlx` (precisa suportar tools) |
| `LOG_LEVEL` | `INFO` (opcional) |

> **Liberando o grupo do time:** adicione o bot ao grupo e mande `/start@joazindamassa_bot` lá. Ele responde com o ID do grupo; coloque esse ID em `ALLOWED_CHATS` e reinicie. Depois, no @BotFather, `/setjoingroups` → **Disable** impede que outras pessoas adicionem o bot em outros grupos. Se alguém adicionar mesmo assim, o bot sai sozinho.

## Rodando

Com [uv](https://docs.astral.sh/uv/), em dois terminais:

```bash
cd hayhooks && uv sync && set -a && . ../.env && set +a && uv run hayhooks run --port 1416 --pipelines-dir pipelines
```

```bash
uv sync && uv run issues-bot
```

No Kubernetes (produção): veja [`kubernetes/README.md`](kubernetes/README.md). As imagens são publicadas pelo GitHub Actions em `ghcr.io/govhub-br`.

Com Docker (sobe o Hayhooks e o bot juntos):

```bash
docker compose up -d --build
docker compose logs -f
```

Na subida o bot lista os repositórios da organização e, se o token ou o `GITHUB_ORG` estiverem errados, para com uma mensagem clara no log. A lista fica em cache por 10 minutos, então um repositório novo aparece sozinho.

## Desenvolvimento

```bash
uv run pytest                      # bot (inclui a conversa do /nova de ponta a ponta)
cd hayhooks && uv run pytest       # Agent, com um gerador roteirizado no lugar do LLM
```

```
src/issues_bot/
├── __main__.py     ponto de entrada (logging + polling)
├── app.py          registro dos handlers, menu de comandos, checagem do repositório
├── handlers.py     comandos, botões, comentários, tratamento de erros
├── new_issue.py    conversa do /nova (assistida e manual)
├── drafter.py      cliente do endpoint issue_draft
├── keyboards.py    botões inline (e o formato do callback_data)
├── formatting.py   mensagens em HTML do Telegram
├── github.py       cliente assíncrono da API REST do GitHub
└── config.py       leitura do .env

hayhooks/
├── pipelines/issue_draft/pipeline_wrapper.py   endpoint (run_api + run_chat_completion)
└── src/issue_drafter/
    ├── agent.py        Agent do Haystack, tools e limpeza do rascunho
    ├── github.py       busca de issues e labels (só leitura)
    ├── config.py
    └── prompts/system.md
```

### Segurança

- Hoje o bot está liberado só para `ALLOWED_USERS=arthrok`, no privado e em qualquer grupo em que ele esteja; os demais são recusados. Para liberar um grupo inteiro, use `ALLOWED_CHATS=<id do grupo>`: assim o bot também sai sozinho de grupos não liberados. Prefira IDs a usernames quando puder, porque o username do Telegram pode ser trocado e depois reaproveitado por outra pessoa. Com `ALLOWED_USERS=*`, **qualquer pessoa que achar o bot** consegue ler e criar issues.
- Quem não está liberado recebe o próprio ID e mais nada; em grupos, mensagens comuns de quem não está liberado são ignoradas.
- O Hayhooks também expõe rotas de deploy de pipelines, por isso o compose **não publica a porta dele**: só o bot o acessa, pela rede interna.
- O log do `httpx` fica em `WARNING`, porque a URL da API do Telegram contém o token do bot.
- O `.env` está no `.gitignore` e no `.dockerignore`.
