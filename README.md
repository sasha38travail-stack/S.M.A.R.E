# S.M.A.R.E

Carte de suivi du CO₂ en ligne, alimentée par un capteur branché sur un PC.

## Fonctionnement

1. Un capteur de CO₂ envoie ses mesures sur un port série.
2. Un script Python sur le PC lit ces mesures et les ajoute à `data.csv`.
3. Le script envoie automatiquement `data.csv` sur GitHub (`git push`).
4. La page `index.html`, publiée avec GitHub Pages, lit `data.csv` et affiche la carte.

## Fichiers

| Fichier | Rôle |
|---|---|
| `index.html` | Page web qui affiche la carte |
| `data.csv` | Mesures de CO₂ (mises à jour par le script) |
| `README.md` | Cette présentation |

## Site en ligne

https://VOTRENOM.github.io/NOM-DU-DEPOT/

## Installation sur le PC

1. Installer [Git](https://git-scm.com) et [Python](https://www.python.org) (cocher « Add Python to PATH »).
2. Cloner le dépôt :
   ```
   git clone https://github.com/VOTRENOM/NOM-DU-DEPOT.git
   ```
3. Installer la bibliothèque série :
   ```
   pip install pyserial
   ```
4. Lancer le script de lecture du capteur (à venir).

## Format de `data.csv`

```
date,co2
```

Une ligne par mesure : la date et l'heure, puis la valeur de CO₂ en ppm.
