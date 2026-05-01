# HU-01.1 — Topbar, título e notificações exibem "Neo"

Data: 2026-05-01

## Evidência técnica

- `static/index.html` inicia com `<title>Neo</title>`.
- `static/index.html` usa `apple-mobile-web-app-title` como `Neo`.
- `static/index.html` troca o ícone inicial da titlebar para
  `static/brand/neo-mark.svg`.
- O placeholder estático inicial do composer usa `Message Neo...`.
- `static/manifest.json` usa `name: Neo WebUI` e `short_name: Neo`.

## Validação

- `pytest tests/test_neo_branding_assets.py`

## Pendência de DoD

- Anexar screenshot/homologação manual em runtime.
