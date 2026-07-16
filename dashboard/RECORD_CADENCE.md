# Séance d'enregistrement cadence (~6 min)

But : capturer le signal de l'accéléromètre de cadre + la vitesse tapis à
plusieurs allures, pour que je calibre hors-ligne la détection de pas
(algorithme autocorrélation + a priori vitesse). Résultat : de bons réglages
`cad_threshold` / `cad_rearm` / `cad_refract_ms` poussés par `SET` — **sans reflash**.

## Avant de commencer
- Dashboard → **Calibration → onglet CADENCE**. (L'enregistrement refuse si une
  *course* est en cours — reste sur cet écran, pas en mode course.)
- Le Feather doit être branché (USB) et le tapis fonctionnel.
- Prépare un chrono (téléphone) pour les comptages « vérité terrain ».

## La séance (tiens chaque palier STABLE, ne touche à rien pendant le palier)

| # | Durée | Allure | À faire |
|---|-------|--------|---------|
| 1 | **20 s** | **tapis ARRÊTÉ**, toi debout à côté ou immobile | rien — je mesure le bruit de fond |
| 2 | 45 s | marche lente (~4-5 km/h) | — |
| 3 | 45 s | marche rapide (~5,5-6,5 km/h) | **compte tes pas 20 s** (ancre A) |
| 4 | 45 s | petit jogging (~7-8 km/h) | — |
| 5 | 45 s | course facile (~8,5-9,5 km/h) | **compte tes pas 20 s** (ancre B) |
| 6 | 45 s | course confortable (ton allure tenue, pas besoin de forcer) | compte 20 s si possible (ancre C) |
| 7 | 30 s | marche de récup | — |

**Adapte les km/h à TES allures** — la vitesse réelle est enregistrée dans le
fichier, je m'aligne dessus. Entre deux paliers, change la vitesse et laisse
stabiliser ~5 s (je coupe les transitions automatiquement).

## Le comptage « vérité terrain » (le point clé)
À un palier stable, **compte CHAQUE pied qui touche** (gauche + droite) pendant
**20 s**, puis note. Cadence = `nombre × 3` pas/min.
- Le minimum utile : **1 ancre en marche + 1 ancre en course** (ça verrouille
  l'harmonique, l'algo fait le reste).
- Compter en courant est plus dur : si tu n'y arrives pas, **compte au moins la
  marche rapide** (ancre A) et je m'appuie sur l'autocorrélation pour les courses.

Note-moi juste, par ex. :
```
marche rapide  ~6.0 km/h  -> 38 pas en 20 s   (=114 spm)
course facile  ~9.0 km/h  -> 55 pas en 20 s   (=165 spm)
```

## Démarrer / arrêter
1. Onglet CADENCE → bouton **● ENREGISTRER** (il passe à **■ ARRÊTER**, un compteur de lignes défile).
2. Déroule la séance ci-dessus.
3. **■ ARRÊTER**. Le dashboard note le fichier : `cadraw_AAAAMMJJ_HHMMSS.csv`.

## M'envoyer le fichier
Le CSV est sur le Pi dans `~/treadmill_logs/`. Copie-le vers le dossier projet
(depuis ta machine Windows) pour que je le lise :
```
scp eddycg@<IP_DU_PI>:~/treadmill_logs/cadraw_*.csv "D:/Running/Nouveau dossier/treadmill_dashboard/"
```
Puis dis-moi **le nom du fichier** + tes **comptages**. Je lance
`cadence_calibrate.py` dessus et je te renvoie les valeurs `SET`
(à mettre dans `treadmill_config.json` + redémarrage serveur, ou via les
boutons ± de l'onglet CADENCE). Aucun reflash.

## Astuce fiabilité
- Reste bien **stable** à chaque palier (le calibrage aime les plateaux propres).
- Si un palier a été bancal, refais simplement une séance — c'est rapide.
