# HU-01.2 — Logo "NEO" e avatar/mark humanoide

Data: 2026-05-01

## Evidência técnica

Assets criados:

- `static/brand/neo-avatar.svg`
- `static/brand/neo-avatar-mono.svg`
- `static/brand/neo-mark.svg`

Os SVGs incluem `<title>`, descrição acessível, `role="img"` e
`aria-labelledby`. O avatar segue a especificação do Design Spec §9:
silhueta humanoide frontal, wireframe holográfico cyan, pontos de brilho,
linhas de scan e texto `NEO`.

## Validação

- `pytest tests/test_neo_branding_assets.py`

## Pendência de DoD

- Anexar screenshot comparando dark/light em runtime.
