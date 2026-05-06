# Neo WebUI — Customização do Hermes WebUI

> Pasta de documentação da iniciativa **Neo WebUI**: customização visual e funcional
> do Hermes WebUI para virar a interface web do agente pessoal **Neo** (segundo
> cérebro do Júnior Melo). Mantém a stack original (Python + vanilla JS, sem build),
> adiciona painéis novos (Dashboard, Kanban) e prepara terreno para um painel
> futuro de **Agentes** (mapeamento de subagentes em delegação).
>
> Este fork é cliente do upstream `nesquena/hermes-webui`. As mudanças foram
> desenhadas para serem **aditivas e isoláveis**, permitindo absorver releases
> upstream com baixo atrito de merge — ver [UPSTREAM-SYNC.md](./UPSTREAM-SYNC.md).

---

## Mapa dos documentos

| Documento | Função |
|---|---|
| [PRD.md](./PRD.md) | Visão de produto: persona, problema, objetivos, escopo, requisitos funcionais e não-funcionais, restrições e riscos. Quem está chegando agora começa por aqui. |
| [BACKLOG.md](./BACKLOG.md) | Backlog completo organizado por **épicos** (EP-01..EP-N), com objetivo, HUs filhas, dependências e prioridade. |
| [TASKS.md](./TASKS.md) | **Documento vivo** de execução. Sprints, HUs (User Stories), tasks técnicas, critérios de aceite, e checklist de evidências (testes + homologação + screenshots) para fechar cada HU. |
| [UPSTREAM-SYNC.md](./UPSTREAM-SYNC.md) | Estratégia de manutenção: como manter o fork sincronizável com `nesquena/hermes-webui`, quais arquivos são "core" (evitar tocar), quais são "Neo-only" (livres), e como resolver conflitos de merge sem perder a personalização. |
| [PRODUCAO.md](./PRODUCAO.md) | Configuração nginx (SSE sem buffering, cache de assets), procedimento de deploy/rollback na VPS `srvjosemaria`. |
| [RUNBOOK.md](./RUNBOOK.md) | Playbook de incidentes: modal travado, chat lento, deploy quebrou, mobile legado. |

---

## Contexto rápido

- **Repositório upstream:** [`nesquena/hermes-webui`](https://github.com/nesquena/hermes-webui) — Apache-2.0
- **Backend Neo (runtime):** [`NousResearch/hermes-agent`](https://github.com/NousResearch/hermes-agent), com `bot_name=Neo` e provider default `glm-5.1` via Z.AI
- **Documentação canônica do Neo:** `~/Documentos/Obsidian Vault/02-Projetos/Neo-Segundo-Cerebro-Documentacao.md`
- **Implantação:** `https://neo.investiorion.com` (nginx → `127.0.0.1:8787`, service `hermes-webui.service` em `/opt/hermes-webui`)
- **Mockup-alvo:** `static/neo_agent_web_ui.png` (dashboard cyan/azul-neon com hero NEO, Kanban e chat lateral)

## Subir localmente

Para homologar a UI com os defaults Neo:

```bash
./neo.sh
```

Opções úteis:

```bash
./neo.sh --port 8788
./neo.sh --isolated
./neo.sh --foreground
./neo.sh --no-browser
```

`--isolated` usa `/tmp/neo-webui-ui` para validar layout sem tocar no estado real
do Hermes. Sem `--isolated`, o launcher usa o estado/configuração normal do
ambiente local.

---

## Princípios de execução

1. **Manter a stack.** Vanilla JS, sem build, sem framework. Toda nova UI deve seguir o padrão dos módulos existentes em `static/*.js`.
2. **Aditivo > intrusivo.** Novos painéis ficam em arquivos dedicados (`dashboard.js`, `kanban.js`); CSS do skin "neo" em bloco isolado dentro de `style.css`.
3. **Configurável quando possível.** O `bot_name` já é runtime config (`HERMES_WEBUI_BOT_NAME=Neo`). Nada de hardcode "Neo" onde já existe variável.
4. **Documento vivo.** Cada HU em `TASKS.md` só fecha quando: (a) testes passam, (b) homologação manual no ambiente de staging, (c) evidências (screenshots/logs) anexadas ou linkadas.
5. **Didático.** Comentários técnicos só onde o "porquê" não é óbvio. Decisões importantes ficam neste diretório, não nos arquivos de código.

---

## Como contribuir

1. Leia [PRD.md](./PRD.md) para entender escopo.
2. Pegue uma HU em [TASKS.md](./TASKS.md) marcada como `🔵 disponível` na sprint corrente.
3. Confira em [UPSTREAM-SYNC.md](./UPSTREAM-SYNC.md) se os arquivos que vai tocar são "core" ou "Neo-only".
4. Implemente. Rode `pytest tests/` (suíte upstream) — não pode regredir.
5. Anexe evidências de homologação na HU e marque o checkbox correspondente.
