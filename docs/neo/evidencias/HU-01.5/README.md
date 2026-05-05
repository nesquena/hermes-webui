# HU-01.5 — Favicon e PWA icons Neo

Data: 2026-05-01

## Evidência técnica

Arquivos atualizados/criados:

- `static/favicon-16.png`
- `static/favicon-32.png`
- `static/favicon-192.png`
- `static/favicon-512.png`
- `static/favicon.ico`
- `static/apple-touch-icon.png`
- `static/manifest.json`

O manifest agora declara a identidade `Neo WebUI`, tema cyan `#00E5FF` e
inclui apenas ícones raster derivados de `static/brand/neo-ico.png`; o favicon
SVG foi removido para evitar que o navegador priorize o asset vetorial antigo.

## Validação

- `pytest tests/test_neo_branding_assets.py`

## Pendência de DoD

- Validar visualmente aba do navegador/PWA em runtime.
