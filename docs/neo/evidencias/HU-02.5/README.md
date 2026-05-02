# HU-02.5 — Erros e toasts em pt-BR

Data: 2026-05-02

## Implementação

- Inventariados toasts/erros visíveis em `static/messages.js`, `static/login.js` e `static/terminal.js`.
- Adicionadas chaves i18n `chat_*`, `clarify_*` e `terminal_library_failed` em `en` e `pt-BR`.
- Removidos hardcodes em inglês de estados visíveis do chat: fila, upload, stream, reconexão, erro genérico, sem resposta e clarify indisponível.
- Login passa a exibir o texto localizado para erro 401, mesmo quando a API retorna `Invalid password`.
- `_LOGIN_LOCALE` em `api/routes.py` passa a cobrir `pt` e `pt-BR` para a página `/login`.

## Validação

- `node --check static/messages.js`
- `node --check static/i18n.js`
- `node --check static/terminal.js`
- `node --check static/login.js`
- `.venv/bin/pytest tests/test_neo_pt_br_toasts.py -q` -> `4 passed in 1.13s`

## Pendências de DoD

- Homologação manual em runtime com `pt-BR`.
- Validação específica de settings, projetos e finanças conforme os painéis MVP forem fechados.
