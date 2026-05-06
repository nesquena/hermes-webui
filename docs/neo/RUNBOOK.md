# Neo WebUI — Runbook operacional

Playbook curto para incidentes mais comuns na VPS `srvjosemaria`.

Antes de entrar em qualquer um dos cenários, leia o estado público do serviço:

```bash
ssh root@38.52.128.62
sudo systemctl status hermes-webui --no-pager
curl -fsS http://127.0.0.1:8787/health | python3 -m json.tool
```

O bloco `stream_latency_ms` no `/health` lista `count`, `p50_ms` e `p95_ms` por
endpoint SSE recente. `approval_sse_subscribers` e `clarify_sse_subscribers`
mostram quantas conexões long-lived estão ativas naquele instante.

---

## Cenário 1 — Modal de aprovação não aparece

Sintoma: usuário envia comando, agente fica "thinking" indefinidamente, mas
nenhum card "Approval required" aparece.

1. **Confirmar pendência no servidor.** Identifique a sessão (`session_id`) e:
   ```bash
   curl -fsS "http://127.0.0.1:8787/api/approval/pending?session_id=<sid>" \
     | python3 -m json.tool
   ```
   - Se `pending: null` → não há aprovação registrada. O agente está apenas
     lento (ver Cenário 2).
   - Se `pending: {...}` → o backend tem aprovação em fila.

2. **Confirmar SSE viva.** No `/health`, `approval_sse_subscribers` deve ser
   ≥ 1 enquanto a aba estiver aberta na sessão. Se for 0:
   - O frontend não conectou. Pedir ao usuário para recarregar (F5).
   - Se ainda assim ficar em 0, verificar nginx (Cenário 3).

3. **Forçar aparição em teste.** Loopback only:
   ```bash
   curl -X POST "http://127.0.0.1:8787/api/approval/inject_test?session_id=<sid>&pattern_key=test"
   ```
   Se o card aparece com isso, o pipeline frontend↔backend está OK e o problema
   estava na aprovação real (algum tool call não chamou `submit_pending`).

4. **Resolver na unha.** Para destravar uma sessão sem reiniciar:
   ```bash
   curl -X POST http://127.0.0.1:8787/api/approval/respond \
     -H 'Content-Type: application/json' \
     -d '{"session_id":"<sid>","choice":"deny"}'
   ```

---

## Cenário 2 — Chat lento (pensamento e ações)

Sintoma: tokens demoram dezenas de segundos para começar a chegar, ou chegam
em rajadas grandes.

1. **Medir.** No `/health`, observe `stream_latency_ms["/api/chat/stream"]`.
   - `p50` < 30000 ms (30 s) é razoável para turnos com tool call.
   - `p95` > 120000 ms (2 min) sugere fallback no agente, não no WebUI.

2. **Separar agente vs. proxy.** Acesse `127.0.0.1:8787` direto pulando o
   nginx:
   ```bash
   ssh -N -L 8787:127.0.0.1:8787 root@38.52.128.62
   # outra aba
   curl -N "http://127.0.0.1:8787/api/chat/stream?stream_id=<id>"
   ```
   Se essa rota é fluida e `https://neo.investiorion.com` é lenta, o gargalo
   é o nginx — verifique `proxy_buffering off` para `/api/.*/stream` em
   `/etc/nginx/sites-available/hermes-webui` (ver
   [PRODUCAO.md](./PRODUCAO.md)).

3. **Agente em fallback.** Se a lentidão é sistêmica, o Neo principal pode
   ter caído na cadeia (`gpt-5.5` → Gemini → Groq → OpenRouter). Logs:
   ```bash
   sudo journalctl -u hermes-gateway -n 200 --no-pager | grep -iE 'fallback|429|timeout'
   ```

4. **Restart cirúrgico.** Só do WebUI (não derruba o gateway):
   ```bash
   sudo systemctl restart hermes-webui
   curl -fsS http://127.0.0.1:8787/health
   ```

---

## Cenário 3 — Deploy quebrou

Sintoma: pós `git pull` + restart, `/health` retorna 502 ou nginx erro.

1. **Logs imediatos.**
   ```bash
   sudo journalctl -u hermes-webui -n 200 --no-pager
   ```
   Procurar por `Traceback`, `ImportError` ou `address already in use`.

2. **Rollback rápido.**
   ```bash
   cd /opt/hermes-webui
   sudo -u hermes-admin git log --oneline -5
   sudo -u hermes-admin git checkout <commit-anterior>
   sudo systemctl restart hermes-webui
   curl -fsS http://127.0.0.1:8787/health
   ```

3. **Estado do nginx.**
   ```bash
   sudo nginx -t
   sudo systemctl reload nginx
   sudo journalctl -u nginx -n 100 --no-pager
   ```

4. **Registrar.** Após estabilizar, anotar causa em
   `Obsidian Vault/02-Projetos/Neo-Segundo-Cerebro-Documentacao.md`
   antes de tentar novo deploy.

---

## Cenário 4 — Mobile mostra layout legado

Sintoma: usuário relata ícones duplicados ou título "Hermes" no celular.

1. Garantir cache limpo do PWA:
   - Aba: forçar reload com `Ctrl+Shift+R` (desktop) ou desinstalar/instalar
     o app na tela inicial (mobile).
2. Confirmar versão servida:
   ```bash
   curl -s https://neo.investiorion.com/ | grep "neoDefaults\|WEBUI_VERSION"
   ```
3. Se persistir, abrir devtools mobile (Safari Desenvolver / Chrome remote
   debugging) e checar:
   - `body.classList.contains("dashboard-shell-mode")` deve ser `true`
   - `.sidebar > .sidebar-nav` precisa estar com `display: none`

---

## Comandos de referência rápida

| Ação | Comando |
|---|---|
| Status do service | `sudo systemctl status hermes-webui --no-pager` |
| Tail de logs | `sudo journalctl -u hermes-webui -f` |
| Health detalhado | `curl -fsS http://127.0.0.1:8787/health \| python3 -m json.tool` |
| Recarregar nginx | `sudo nginx -t && sudo systemctl reload nginx` |
| Restart limpo | `sudo systemctl restart hermes-webui` |
| Última release | `cd /opt/hermes-webui && sudo -u hermes-admin git log --oneline -3` |
