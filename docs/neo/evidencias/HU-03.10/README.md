# HU-03.10 — Painel mínimo "Pessoal"

**Data:** 2026-05-02  
**Branch:** `develop`

## Implementação

- Adicionado bloco Neo no painel `profiles` com resumo de:
  - perfil ativo
  - idioma
  - painel inicial
  - tema / skin
- Adicionado link direto para Configurações > Preferências.
- Mantido o painel upstream de perfis abaixo do resumo, sem backend novo.
- Escopo futuro de notas pessoais fica fora da Sprint 2 e deve ser definido em HU posterior.

## Validação técnica

- Teste dedicado: `tests/test_neo_dashboard_admin_personal.py`

## Homologação visual

- Validada manualmente pelo usuário em 2026-05-02 dentro do escopo da HU.
- Escopo validado: painel Pessoal mínimo apresenta resumo útil de perfil e
  preferências, com link para Settings.
