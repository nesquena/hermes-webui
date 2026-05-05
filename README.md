# Neo WebUI

Interface web de produção do **Neo**, o agente pessoal/executivo de Júnior Melo.
Este projeto é um fork operacional do `hermes-webui`, customizado para substituir
a WebUI original do Hermes Agent na VPS do Neo.

O objetivo deste repositório é entregar uma experiência de uso diária para o Neo:
chat central com o agente, shell visual próprio, dashboard operacional, painel de
projetos, tarefas, skills, perfis, configurações e integração com o runtime Hermes
existente.

## Estado

| Item | Valor |
|---|---|
| Produto | Neo WebUI |
| Base técnica | Fork de `nesquena/hermes-webui` |
| Runtime | Hermes Agent |
| Produção | `neo.investiorion.com` |
| VPS | `srvjosemaria` (`38.52.128.62`) |
| Serviço | `hermes-webui.service` |
| Diretório alvo | `/opt/hermes-webui` |
| Porta interna | `127.0.0.1:8787` |
| Proxy | nginx HTTPS |
| Branch de integração | `develop` |
| Branch de produção | `main` |

Fonte operacional externa:
`/home/jrmelo/Documentos/Obsidian Vault/02-Projetos/Neo-Segundo-Cerebro-Documentacao.md`

## O Que Este Fork Entrega

- **Dashboard Neo** com navegação lateral, topbar operacional, chat central e
  coluna direita com hero, KPIs e ações rápidas.
- **Projetos Command Center** com Kanban, lista, filtros, edição, status,
  prioridade, fonte, progresso, vínculos com tarefas e APIs dedicadas.
- **Shell de produto** com páginas internas para Projetos, Tarefas, Pessoal,
  Finanças, Agentes, Skills, Automação e Configurações.
- **Branding Neo** com favicon raster, PWA icons, avatar, hero e identidade visual
  cyan/azul-neon.
- **Compatibilidade Hermes** preservando o server Python, SSE, sessões,
  workspaces, profiles, skills, comandos, anexos e controles do composer.
- **Estratégia de manutenção upstream** documentada em
  [docs/neo/UPSTREAM-SYNC.md](docs/neo/UPSTREAM-SYNC.md).

## Arquitetura

```text
Browser
  |
  | HTTPS
  v
nginx
  |
  | http://127.0.0.1:8787
  v
hermes-webui.service
  |
  | Python stdlib HTTP server + static app
  v
Neo WebUI
  |
  | imports / uses Hermes runtime
  v
/opt/hermes + /home/hermes-admin/.hermes
```

Na VPS, o Neo não roda como aplicação isolada do Hermes. Ele é a superfície web
do runtime Hermes já configurado em `/home/hermes-admin/.hermes`, usando a mesma
memória, sessões, profiles, toolsets, MCPs, providers e workspaces.

## Estrutura Relevante

```text
api/                    Rotas HTTP, auth, config, streaming, projetos e helpers
static/                 HTML, CSS, JS e assets servidos diretamente
static/brand/           Assets Neo de marca e hero
docs/neo/               PRD, design spec, backlog, tasks e evidências por HU
docs/superpowers/       Specs e planos de implementação
tests/                  Regressão backend, frontend estático e contratos Neo
server.py               Entrada HTTP principal
start.sh                Launcher local via bootstrap
neo.sh                  Launcher auxiliar do ambiente Neo
```

## Desenvolvimento Local

Requisitos:

- Linux/macOS/WSL
- Python 3.12+
- Runtime Hermes disponível localmente ou configurado via variáveis de ambiente

Inicialização padrão:

```bash
./start.sh
```

Variáveis úteis:

```bash
export HERMES_WEBUI_AGENT_DIR=/path/to/hermes-agent
export HERMES_WEBUI_PYTHON=/path/to/python
export HERMES_WEBUI_HOST=127.0.0.1
export HERMES_WEBUI_PORT=8787
export HERMES_WEBUI_STATE_DIR=~/.hermes/webui-mvp
export HERMES_WEBUI_DEFAULT_WORKSPACE=~/workspace
```

Healthcheck:

```bash
curl http://127.0.0.1:8787/health
```

## Testes

Rodada completa:

```bash
python -m pytest -q
```

Rodadas focadas usadas com frequência no fork Neo:

```bash
python -m pytest tests/test_neo_branding_assets.py tests/test_pwa_manifest_sw.py -q
python -m pytest tests/test_neo_dashboard_shell_visual.py tests/test_neo_hero_greeting.py -q
python -m pytest tests/test_neo_projects_api.py tests/test_neo_projects_kanban.py -q
node --check static/sw.js
git diff --check
```

Os testes usam estado isolado quando sobem servidor de teste. Dados reais de
produção, sessões reais e crons reais não devem ser tocados pela suíte.

## Fluxo Git

- `develop`: integração de sprints e ajustes visuais.
- `main`: base pronta para produção.
- Commits devem ser pequenos, descritivos e ligados a Sprint/HU quando aplicável.
- Antes de mergear `develop` em `main`, rodar verificação local e garantir que o
  working tree está limpo.

Fluxo esperado:

```bash
git checkout develop
git pull --ff-only origin develop
python -m pytest -q

git checkout main
git pull --ff-only origin main
git merge --no-ff develop
python -m pytest -q
git push origin main
```

## Produção

Produção usa systemd + nginx na VPS:

| Camada | Valor |
|---|---|
| Host | `root@38.52.128.62` |
| Usuário runtime | `hermes-admin` |
| Repo WebUI | `/opt/hermes-webui` |
| Runtime Hermes | `/opt/hermes` |
| Estado Hermes | `/home/hermes-admin/.hermes` |
| Service | `hermes-webui.service` |
| Porta interna | `127.0.0.1:8787` |
| Domínio | `https://neo.investiorion.com` |

Deploy manual recomendado:

```bash
ssh root@38.52.128.62

cd /opt/hermes-webui
sudo -u hermes-admin git fetch origin
sudo -u hermes-admin git checkout main
sudo -u hermes-admin git pull --ff-only origin main

sudo systemctl restart hermes-webui
sudo systemctl status hermes-webui --no-pager
curl -fsS http://127.0.0.1:8787/health
```

Validação pós-deploy:

```bash
journalctl -u hermes-webui -n 120 --no-pager
curl -I https://neo.investiorion.com
```

Checklist manual:

- Abrir `https://neo.investiorion.com`.
- Confirmar favicon Neo raster no navegador.
- Confirmar Dashboard como primeira tela.
- Enviar mensagem curta no chat.
- Abrir Projetos e alternar Kanban/Lista.
- Abrir Skills e Configurações dentro do shell Neo.
- Conferir console do navegador sem erro crítico.

## Rollback

Se o deploy falhar:

```bash
ssh root@38.52.128.62
cd /opt/hermes-webui

sudo -u hermes-admin git log --oneline -5
sudo -u hermes-admin git checkout <commit-estavel>
sudo systemctl restart hermes-webui
curl -fsS http://127.0.0.1:8787/health
```

Depois do rollback, abrir uma issue/tarefa interna ou registrar no documento
operacional do Obsidian antes de tentar novo deploy.

## Segurança

- Não commitar `.env`, tokens, cookies, chaves OAuth ou dumps de sessão.
- Produção deve ficar atrás de HTTPS no nginx.
- O serviço deve continuar bindado em `127.0.0.1`; exposição pública é feita
  apenas pelo reverse proxy.
- `HERMES_WEBUI_PASSWORD` pode ser usado para autenticação da WebUI quando
  necessário.
- Domínios institucionais/sensíveis do Neo seguem as regras do Hermes em
  `SOUL.md`, `config.yaml`, `contexts/` e skills do runtime.

## Documentação

- [docs/neo/README.md](docs/neo/README.md): índice da iniciativa Neo WebUI.
- [docs/neo/PRD.md](docs/neo/PRD.md): produto, requisitos e escopo.
- [docs/neo/DESIGN-SPEC.md](docs/neo/DESIGN-SPEC.md): sistema visual e telas.
- [docs/neo/TASKS.md](docs/neo/TASKS.md): execução por HUs.
- [docs/neo/BACKLOG.md](docs/neo/BACKLOG.md): backlog priorizado.
- [docs/neo/UPSTREAM-SYNC.md](docs/neo/UPSTREAM-SYNC.md): manutenção do fork.
- [TESTING.md](TESTING.md): detalhes da suíte herdada do Hermes WebUI.
- [ARCHITECTURE.md](ARCHITECTURE.md): arquitetura original do WebUI base.

## Licença e Upstream

Este fork preserva a licença original do Hermes WebUI. Consulte [LICENSE](LICENSE).

Upstream original:

- Hermes Agent: <https://github.com/NousResearch/hermes-agent>
- Hermes WebUI base: `nesquena/hermes-webui`

Mudanças genéricas que beneficiem o WebUI base devem ser avaliadas para PR
upstream. Mudanças específicas do Neo devem permanecer isoladas em assets,
documentação, CSS/JS/rotas do fork ou arquivos claramente marcados em
`docs/neo/UPSTREAM-SYNC.md`.
