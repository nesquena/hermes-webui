# Hermes WebUI — Mise à jour et modifications custom

**Date :** 7 juin 2026
**VPS :** idswood.fr (45.147.98.34)
**Branche :** `custom-telegram-delivery`

---

## 🔄 Mise à jour upstream

**v0.51.283 → v0.51.310** (28 commits, 12 905 lignes ajoutées)

### Principales nouveautés

| Version | Apport |
|---------|--------|
| v0.51.310 | Long-press chips projet (tactile) |
| v0.51.309 | Replay cartes outils live après reconnexion |
| v0.51.306 | Compression "branchy" — meilleure gestion historique long |
| v0.51.304 | Reaper terminaux zombies + GPU Docker opt-in |
| v0.51.303 | Toggle cron + expansion variables config |
| v0.51.302 | Fix crash mobile/iOS + perfs grosses sessions |
| v0.51.299 | Flow de mise à jour attend le nouveau serveur |
| v0.51.296 | Fix sécurité workspace distant root |
| v0.51.293 | Carte thinking plus en double |
| v0.51.292 | Erreurs compression remontent en surface |
| v0.51.291 | Conservation contenu live au changement d'onglet |
| v0.51.288 | Carte approbation rétractable |
| v0.51.286 | **Réorganisation onglets par drag & drop** (#3067) |
| v0.51.284 | Labels statut sidebar + toggle sessions cron |

---

## ✨ Modifications custom (ideesimple)

### 1. Livraison Telegram par alias

**Fichiers modifiés :**
- `api/routes.py` — `_handle_cron_delivery_options()` : parse `DELIVERY_ALIASES` depuis `.env`, format `"alias:telegram:<chat_id>,..."`
- `static/panels.js` — `openCronDetail()` devient `async`, résout `job.deliver_label` depuis `/api/crons/delivery-options` ; `_renderCronDetail()` affiche `job.deliver_label`

**Fichier de config :** `/opt/hermes-webui/.env`
```
DELIVERY_ALIASES="Nom d'alias:telegram:<CHAT_ID>,..."
```
> ⚠️ Les vrais chat_id sont dans le `.env` du serveur, pas dans ce repo public.

### 2. Protection par mot de passe (nginx)

**Fichiers :**
- `/etc/nginx/sites-available/hermes.idsworld.fr` — ajout `auth_basic` + exemption `.well-known`
- `/etc/nginx/.htpasswd-hermes` — utilisateur `laurent`

### 3. Autres ajustements

- `static/index.html` — modifications mineures d'intégration
- `static/style.css` — toast `visibility` + merge résolu

---

## 🔧 Procédure de déploiement

```bash
# 1. Pull upstream
cd /opt/hermes-webui
git stash
git pull origin master
git stash pop

# 2. Résoudre les conflits éventuels

# 3. Redémarrer le service
bash ctl.sh restart

# 4. Vérifier
curl -sk -o /dev/null -w "%{http_code}" https://hermes.idsworld.fr/ -u "laurent:<mdp>"
# → 200
```

---

## 🔗 URLs

- **Repo upstream :** https://github.com/nesquena/hermes-webui
- **Fork :** https://github.com/ideesimple/hermes-webui (branche `custom-telegram-delivery`)
- **WebUI :** https://hermes.idsworld.fr
