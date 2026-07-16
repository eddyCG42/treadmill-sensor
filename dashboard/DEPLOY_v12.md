# Déploiement v12.1 — firmware + Pi (déploiement COUPLÉ)

⚠️ **Le nouveau code Pi et le firmware v12.1 vont ensemble.** Le Pi parle le
protocole v12 (`SET` / `BLE_INCLIN` / `BLE_CAD` / handshake). Ancien firmware +
nouveau Pi (ou l'inverse) = calibration non synchronisée. On flashe ET on copie
dans la même opération.

**Nouveau en v12.1 — détection de cadence sur le Pi.** La cadence n'est plus
comptée par seuil sur le Feather (le capteur de cadre voit des impacts faibles +
du « ringing » → sous-compte la marche, double-compte la course). Le Feather
streame l'accéléro brut (`$RAW`), le Pi détecte la cadence par passe-bande +
autocorrélation en fenêtre glissante (validé **±4 spm de 64 à 171 spm** sur ta
séance de calibration), et la pousse via `BLE_CAD` → Garmin. Tout est réglable à
chaud (config Pi), **plus jamais de reflash pour la cadence**. Le détecteur du
Feather reste comme repli si le push du Pi devient périmé.

**Ordre imposé** : il faut *arrêter le serveur* avant de flasher (il tient le
port série USB ouvert — sinon l'upload Arduino échoue « port busy »).

```
1. Arrêter le serveur Pi   →  2. Flasher le Feather  →  3. Copier les fichiers Pi  →  4. Redémarrer
```

---

## 0. Avant de commencer (sur le Pi)
Sauvegarde de secours (le capot est ouvert, autant être tranquille) :
```bash
cp ~/treadmill_config.json ~/treadmill_config.json.bak
cp -r ~/treadmill_dashboard ~/treadmill_dashboard.bak-v11
```
Garde aussi une copie de l'ancien `.ino` (v11.21) à portée pour rollback firmware.

## 1. Arrêter le serveur (libère le port série)
```bash
sudo systemctl stop treadmill-server
```

## 2. Flasher le Feather (Arduino IDE)
- Fichier : `treadmill_sensor_v11_21.ino` (à la RACINE du projet ; version interne **v12.1**).
- Carte : **Adafruit Feather nRF52840 Express** (core *Adafruit nRF52*).
- Bibliothèques requises (déjà installées si le v11 tournait) :
  *Pololu VL53L4CD*, *Adafruit LSM303 Accel*, *Adafruit Unified Sensor*, *Adafruit Bluefruit nRF52*.
- Sélectionner le bon port, **Upload**.

**Vérif au banc** (Moniteur série, 115200 baud) — 7 points :
1. Au boot : `$HELLO,v12.1` puis `[BOOT] READY`.
2. Taper `CFG_DUMP` → 16 lignes `[CONFIG] …=…` puis `[CONFIG] END`.
3. `SET cad_threshold 3.5` → renvoie `[CONFIG] cad_threshold=3.500`.
4. `SET bidon 1` → `[CONFIG] rejected bidon`.
5. `STATUS` → ligne `[STATUS] FW:v12.1,…,CADSRC:LOCAL,CAD:…` (CADSRC=LOCAL au banc,
   passera à PI dès que le Pi pousse `BLE_CAD`).
6. `BLE_CAD:150` puis `STATUS` → `CADSRC:PI,CAD:150` (le push Pi prend la main).
7. `CAD_RAW:1` → `[CONFIG] cad_raw=1` puis le flux `$RAW,…` défile à ~100 Hz
   (c'est ce flux que le détecteur du Pi consomme). `CAD_RAW:0` l'arrête.

Si un point échoue → ne pas remonter le capot, corriger d'abord.

## 3. Copier les fichiers Pi

### Option A — recommandée : re-lancer deploy.sh (gère tout)
Copie tout le dossier mis à jour puis :
```bash
# depuis ta machine, remplace le dossier projet sur le Pi (rsync conserve
# config/logs/exports qui sont HORS du dossier, dans ~) :
rsync -av --delete \
  "/chemin/local/treadmill_dashboard/" \
  eddycg@<IP_DU_PI>:/home/eddycg/treadmill_dashboard/

# sur le Pi :
cd ~/treadmill_dashboard/web_dashboard
bash deploy.sh        # ré-installe le service, vendored JS, etc. + le redémarre
```
`deploy.sh` supprime aussi tout ancien service pygame résiduel et régénère
l'unit systemd (avec `~` inscriptible → l'historique anti-répétition des
parcours persiste bien).

### Option B — copie minimale (si tu ne veux pas re-run deploy.sh)
Fichiers **modifiés/ajoutés** à copier (chemins relatifs au dossier projet) :

| Fichier | Statut |
|---|---|
| `treadmill_metrics.py` | **NOUVEAU** |
| `treadmill_cadence.py` | **NOUVEAU** (détecteur de cadence — importé par le serveur) |
| `treadmill_routes.py` | modifié |
| `treadmill_export.py` | modifié |
| `treadmill_hr.py` | modifié |
| `treadmill_strava_queue.py` | modifié |
| `web_dashboard/treadmill_server.py` | modifié |
| `web_dashboard/static/dashboard.html` | modifié |
| `web_dashboard/static/layouts.jsx` | modifié |
| `web_dashboard/static/screens.jsx` | modifié |
| `web_dashboard/static/tokens.jsx` | modifié |
| `web_dashboard/static/primitives.jsx` | modifié |

⚠️ `treadmill_metrics.py` et `treadmill_cadence.py` sont **importés par le
serveur** — s'ils manquent, il ne démarre pas. Ne pas les oublier.

Exemple :
```bash
rsync -av \
  treadmill_metrics.py treadmill_cadence.py treadmill_routes.py treadmill_export.py \
  treadmill_hr.py treadmill_strava_queue.py \
  eddycg@<IP_DU_PI>:/home/eddycg/treadmill_dashboard/
rsync -av web_dashboard/treadmill_server.py \
  eddycg@<IP_DU_PI>:/home/eddycg/treadmill_dashboard/web_dashboard/
rsync -av web_dashboard/static/ \
  eddycg@<IP_DU_PI>:/home/eddycg/treadmill_dashboard/web_dashboard/static/
```

## 4. Redémarrer + vérifier
```bash
sudo systemctl restart treadmill-server
journalctl -u treadmill-server -f
```
**Signaux de succès dans les logs** :
- `Feather firmware v12.1`
- `Feather config synced: 16/16 params acked`  ← le nombre grimpe avec le
  nombre de points de calibration inclinaison (16 params + 0/1/2 points ToF).
- Après la synchro, le serveur envoie `CAD_RAW:1` → le flux `$RAW` alimente le
  détecteur de cadence (ré-armé automatiquement toutes les 10 min).

Si tu vois `No ack for SET …` répété → firmware pas en v12, ou port série pas
prêt : vérifier le flash (étape 2) et rebrancher l'USB.

## 5. Contrôles fonctionnels (tapis en marche)
- Dashboard → écran de course : **pace / cadence / inclinaison** en lecture seule,
  cadence en pas/min complets.
- **Pente Garmin = pente dashboard** (le correctif clé v12).
- **Cadence détectée (v12.1)** : marche puis cours ~1 min chacun ; la cadence du
  dashboard doit suivre ta cadence réelle (≈ ce que Garmin affiche) et non plus
  une valeur dérivée de la vitesse. `STATUS` sur le Feather → `CADSRC:PI`.
  Si elle paraît fausse, elle est réglable à chaud (voir Notes).
- Barre d'état basse : USB/BLE/HR réels + `fw v12.1` + stockage.
- Fin de course → **pace moyen non nul** sur l'écran de résumé.
- Calibration inclinaison : **6 points** (-3/0/3/6/9/12).

## Notes
- **Config** : `~/treadmill_config.json` est conservé. Les nouvelles clés
  prennent leurs défauts ; `inclin_sign`/`inclin_zero` (obsolètes) sont ignorées.
  Optionnel : y renseigner `hr_mac`, `hr_max`, `hr_rest`, `body_mass_kg`.
- **Réglages à chaud désormais sans reflash** : tout param firmware se change via
  `SET <clé> <val>` sur le port série (ou en éditant `treadmill_config.json` +
  redémarrage du serveur, qui re-pousse tout au connect).
- **Régler le détecteur de cadence (Pi, sans reflash)** : éditer
  `~/treadmill_config.json` puis redémarrer le serveur. Clés utiles :
  `cad_conf_min` (0.18 — monter si des cadences fantômes apparaissent tapis à
  vide, descendre si la marche lente n'est pas détectée), `cad_win_s` (4.0 —
  fenêtre d'analyse ; plus grand = plus stable mais plus de latence),
  `cad_smooth` (0.30 — lissage sortie), `cad_hold_ms` (2500 — durée de maintien
  avant repli). `cad_det_enable: false` désactive le détecteur Pi (retour au
  compteur du Feather). Le champ `cad_det_conf` de `/api/state` montre la
  confiance en direct (~0.5+ = rythme verrouillé).
- **Rollback firmware** : reflasher l'ancien `.ino` v11.21 ET remettre
  `~/treadmill_dashboard.bak-v11` — les deux ensemble (mêmes contraintes de
  couplage).
