# PR #7075 — lokalny test port 8788 — instrukcja odwracania

**Cel:** przetestować fix maintainera (rename `delete_confirm` → `msg_delete_confirm` w `static/i18n.js` + `static/ui.js`) przed pushem do PR.

**Zakres:** tylko frontend (i18n.js, ui.js), bez backendu.

## Co istnieje (do usunięcia po teście)

- **Katalog:** `/Users/kamil/hermes-webui-pr7075/` — klon Twojego forka, branch `feat/per-message-delete` @ SHA `00c02801`
- **Proces w tle:** PID serwera na porcie 8788 (będzie uruchomiony z `bootstrap.py` lub `server.py` bezpośrednio)
- **Osobny `state.db`:** instancja na 8788 trzyma stan w `data/state.db` względem katalogu klonu — izolowany od produkcyjnej instancji na 8787

## Cofanie (jedna komenda)

```bash
# 1. Znajdź i zabij proces nasłuchujący na 8788
lsof -nP -iTCP:8788 -sTCP:LISTEN -t | xargs -I{} kill {}
# 2. Usuń cały katalog klonu (zawiera patch, venv jeśli się utworzy, state.db)
rm -rf /Users/kamil/hermes-webui-pr7075
```

**NIE dotyka:** `/Users/kamil/hermes-webui/` (produkcyjne WebUI na 8787), launchd plistów, kluczy.

## Co zmieniam

| Plik | Zmiana |
|---|---|
| `static/i18n.js` | `delete_confirm` (linia ~161) → `msg_delete_confirm`, we WSZYSTKICH locale (en + inne) |
| `static/ui.js` (linia ~19220) | `t('delete_confirm')` → `t('msg_delete_confirm')` |
| `static/i18n.js` | opcjonalnie `delete_failed` → `msg_delete_failed` (identyczne stringi, nieszkodliwe ale czyści kolizję) |

## Plan uruchomienia

1. `git status` — czysty branch z PR
2. Patch 2 plików
3. `node --check static/i18n.js static/ui.js` — walidacja składni
4. `grep -n "delete_confirm" static/*.js` — weryfikacja, że martwy klucz zniknął
5. Sprawdzić `bootstrap.py` i `server.py` — jak production (8787) odpala, czy jest inny sposób
6. Uruchomić na 8788 (env var `PORT=8788` lub flaga CLI)
7. Hard refresh przeglądarki na `http://localhost:8788` → kliknąć delete w jakiejś wiadomości → sprawdzić czy confirm mówi "Delete this message and its turn-pair?"

## Czego NIE ruszam

- Backend (`api/session_ops.py`, `api/routes.py`) — fix jest tylko frontendowy
- Testy Pythona (`tests/test_issue_per_message_delete.py`) — fix nie zmienia testów (chyba że Manny'ego collision fixture dodam w drugim przebiegu, ale user najpierw chciał tylko confirm fix przetestować)
- Branch `master` w upstream
- launchd, port 8787