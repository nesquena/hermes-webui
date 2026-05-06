# Neo WebUI — Operação em produção

VPS `srvjosemaria` (`38.52.128.62`), service `hermes-webui.service`, porta interna
`127.0.0.1:8787`, proxy nginx em `neo.investiorion.com`.

Este documento concentra:

1. Configuração nginx exigida para SSE não bufferizar.
2. Cabeçalhos de cache para assets estáticos versionados.
3. Procedimento de deploy + rollback (resumo do README).
4. Diagnóstico rápido de incidentes comuns (ver também [RUNBOOK.md](./RUNBOOK.md)).

---

## 1. nginx — SSE sem buffering

O Neo WebUI usa Server-Sent Events em quatro endpoints longos:

| Endpoint | Uso |
|---|---|
| `/api/chat/stream` | Tokens, reasoning e tool calls do turno em curso |
| `/api/approval/stream` | Solicitações de aprovação de ferramentas |
| `/api/clarify/stream` | Perguntas de clarificação do agente |
| `/api/cmd/stream` | Saída do shell visual |

O servidor já envia `Content-Type: text/event-stream`, `Cache-Control: no-cache` e
`X-Accel-Buffering: no`. nginx ainda assim pode segurar bytes se `gzip on`,
`proxy_buffering on` (default) ou `proxy_read_timeout` curto estiverem ativos.

Bloco recomendado em `/etc/nginx/sites-available/hermes-webui`:

```nginx
upstream hermes_webui {
    server 127.0.0.1:8787;
    keepalive 16;
}

server {
    listen 443 ssl http2;
    server_name neo.investiorion.com;

    # ── Certificados Let's Encrypt (mantidos pelo certbot) ──
    ssl_certificate     /etc/letsencrypt/live/neo.investiorion.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/neo.investiorion.com/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    # ── Limites de upload (anexos do composer) ──
    client_max_body_size 64m;

    # ── Defaults para tudo ──
    proxy_http_version 1.1;
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection        "";

    # ── Bloco SSE (streams longos, sem buffering, sem gzip) ──
    location ~ ^/api/(chat|approval|clarify|cmd)/stream$ {
        proxy_pass http://hermes_webui;
        proxy_buffering off;
        proxy_cache off;
        proxy_request_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        gzip off;
        chunked_transfer_encoding on;
    }

    # ── Assets estáticos versionados (`?v=__WEBUI_VERSION__`) ──
    location /static/ {
        proxy_pass http://hermes_webui;
        proxy_cache_valid 200 30d;
        # O servidor reescreve __WEBUI_VERSION__ a cada release, então o nome
        # do recurso muda automaticamente — pode marcar imutável com folga.
        add_header Cache-Control "public, max-age=31536000, immutable" always;
    }

    # ── Aplicação ──
    location / {
        proxy_pass http://hermes_webui;
        proxy_read_timeout 120s;
        gzip on;
        gzip_types text/css application/javascript application/json text/html;
    }
}

server {
    listen 80;
    server_name neo.investiorion.com;
    return 301 https://$host$request_uri;
}
```

Após editar:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Validação rápida — abrir uma sessão e rodar:

```bash
curl -N -H 'Accept: text/event-stream' \
  'https://neo.investiorion.com/api/approval/stream?session_id=<sid>'
```

Saída esperada: `event: initial` em ≤300 ms. Se demora >2 s ou fica em silêncio
até receber tudo de uma vez, o buffering ainda está ligado.

---

## 2. Cabeçalhos de cache

Os assets servidos pela app já vêm com `?v=__WEBUI_VERSION__` (`api/updates.py`).
Cada release muda o querystring, então cache imutável é seguro. O bloco
`location /static/` acima cobre isso.

Para vendor self-hosted (Onda 3b), o caminho `/static/vendor/<lib>@<versao>/...`
herda o mesmo cabeçalho — não precisa regra adicional.

---

## 3. Deploy padrão

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

---

## 4. Rollback

```bash
ssh root@38.52.128.62
cd /opt/hermes-webui

sudo -u hermes-admin git log --oneline -5
sudo -u hermes-admin git checkout <commit-estavel>
sudo systemctl restart hermes-webui
curl -fsS http://127.0.0.1:8787/health
```

Após rollback bem-sucedido, registrar o motivo em
`Obsidian Vault/02-Projetos/Neo-Segundo-Cerebro-Documentacao.md` antes de tentar
novo deploy.
