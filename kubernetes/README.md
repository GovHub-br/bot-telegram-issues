# Deploy no Kubernetes

```
namespace bot-telegram-issues
├── Deployment bot        1 réplica, Recreate. Busca as mensagens no Telegram (polling): sem Service, sem Ingress
├── Deployment hayhooks   Agent do rascunho de issues, porta 1416
├── Service hayhooks      ClusterIP, só dentro do cluster
├── NetworkPolicy         só o bot acessa o hayhooks
├── ConfigMap             configuração sem segredos (org, Project, quem pode usar, Ollama…)
└── Secret                TELEGRAM_TOKEN, GITHUB_TOKEN e GITHUB_TOKEN_ASSETS (criado à mão, fora do git)
```

**Rede:** nada entra no cluster. O bot só **sai** para o Telegram e o GitHub (internet). O Hayhooks sai para o GitHub e para o Ollama pelo Service `ollama-external.macstudio`, que já aponta para o Mac Studio (`10.0.0.10:11434`). As pessoas usam o bot de qualquer lugar, porque falam com os servidores do Telegram, e não com o cluster.

## Imagens

O GitHub Actions ([`.github/workflows/main.yml`](../.github/workflows/main.yml)) roda os testes e, a cada push na `main`, publica:

- `ghcr.io/govhub-br/bot-telegram-issues:<sha>` e `:latest`
- `ghcr.io/govhub-br/bot-telegram-issues-hayhooks:<sha>` e `:latest`

Como o repositório é privado, os pacotes nascem **privados**. Escolha uma das opções:

- **Público** (como `gov-bricks` e `tais-webui`): no GitHub, em *Packages → bot-telegram-issues → Package settings → Change visibility → Public*. Faça o mesmo para o `-hayhooks`. O cluster puxa sem credencial.
- **Privado:** crie um Secret de pull com um token *classic* com `read:packages` e adicione `imagePullSecrets` nos dois Deployments.

## Subindo

1. **Pare o bot que roda fora do cluster** (no seu computador ou no docker compose). Duas instâncias com o mesmo token dão `Conflict` no Telegram.

2. Crie o namespace e o Secret a partir do `.env`, sem passar os tokens pela linha de comando:

   ```bash
   kubectl apply -f kubernetes/namespace.yaml
   ```

   ```bash
   grep -E '^(TELEGRAM_TOKEN|GITHUB_TOKEN|GITHUB_TOKEN_ASSETS)=' .env | kubectl -n bot-telegram-issues create secret generic bot-telegram-issues-tokens --from-env-file=/dev/stdin --dry-run=client -o yaml | kubectl apply -f -
   ```

3. Aplique o resto:

   ```bash
   kubectl apply -k kubernetes/
   ```

4. Acompanhe:

   ```bash
   kubectl -n bot-telegram-issues get pods
   ```

   ```bash
   kubectl -n bot-telegram-issues logs deploy/bot -f
   ```

   No log do bot devem aparecer `conectado a GovHub-br`, `Rascunho assistido ligado` e `Application started`.

## Atualizando

- **Nova versão:** depois do push na `main`, rode `kubectl -n bot-telegram-issues rollout restart deploy/bot deploy/hayhooks`. Os pods usam `imagePullPolicy: Always` e puxam o `latest`. Para fixar uma versão, troque `newTag: latest` pelo SHA no [`kustomization.yaml`](kustomization.yaml).
- **Configuração:** edite o [`configmap.yaml`](configmap.yaml), rode `kubectl apply -k kubernetes/` e reinicie os dois Deployments.
- **Tokens:** repita o comando do passo 2 e reinicie os Deployments.

## ArgoCD

O cluster já tem ArgoCD. Para gerenciar por GitOps, aponte uma `Application` para este repositório no caminho `kubernetes/`. O Secret continua fora do git (crie à mão como no passo 2, ou pelo OpenBao).
