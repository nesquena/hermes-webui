# HU-03.9 — Admin dropdown

**Data:** 2026-05-02  
**Branch:** `develop`

## Implementação

- Adicionado dropdown no botão Admin da topbar do Dashboard.
- Menu inclui ações Perfil, Configurações e Sair.
- Ações reutilizam handlers existentes:
  - Perfil: `switchPanel('profiles')`
  - Configurações: `switchPanel('settings')` + `switchSettingsSection('preferences')`
  - Sair: `signOut()`
- Dropdown fecha por clique externo e tecla Escape.

## Validação técnica

- Teste dedicado: `tests/test_neo_dashboard_admin_personal.py`

## Homologação visual

- Validada manualmente pelo usuário em 2026-05-02 dentro do escopo da HU.
- Escopo validado: dropdown Admin abre corretamente e expõe Perfil,
  Configurações e Sair reutilizando os fluxos existentes.
