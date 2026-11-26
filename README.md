# S.M.A.R.E.

**Station Météorologique Automatisée de Recherche Environnementale**

S.M.A.R.E. est un prototype pédagogique de station environnementale embarquée sur drone. Il associe des relevés de CO₂ et de météo à une position GPS, puis les présente sur une carte consultable en ligne.

[**Ouvrir la carte interactive**](https://sasha38travail-stack.github.io/S.M.A.R.E/) · [Voir le workflow Windows](https://github.com/sasha38travail-stack/S.M.A.R.E/actions/workflows/exe.yml)

## Ce que montre la carte

- **CO₂** en ppm
- **Température** en °C
- **Humidité relative** en %
- **Pression atmosphérique** en hPa
- **Position GPS** et altitude lorsqu’elles sont disponibles
- Affichage **2D** ou **3D**, légende par grandeur et détails au clic sur un point

La carte relit data.csv toutes les 30 secondes. Elle affiche les relevés présents dans le fichier; aucune mesure n’est ajoutée automatiquement par la page.

## Chaîne de mesure

1. Les capteurs embarqués mesurent l’air et les conditions météo. Le GPS situe le relevé et la carte microSD conserve les données.
2. Une liaison radio transmet les mesures au récepteur au sol.
3. Le programme Python SMARE.py sur l’ordinateur rassemble les données dans data.csv et peut les publier dans ce dépôt.
4. La page GitHub Pages lit le CSV et affiche les points sur la carte.

## Fichiers du dépôt

| Fichier | Rôle |
|---|---|
| index.html | Site du projet et carte interactive |
| data.csv | Mesures publiées, avec position, altitude et valeurs météo |
| SMARE.py | Application PC pour réception, enregistrement et synchronisation |
| .github/workflows/exe.yml | Compilation de SMARE.exe sur GitHub Actions |

Les programmes Arduino et les fichiers de conception sont conservés dans leurs dossiers de projet respectifs.

## Télécharger l’application Windows

Ouvrir [les exécutions du workflow Construire SMARE.exe](https://github.com/sasha38travail-stack/S.M.A.R.E/actions/workflows/exe.yml), choisir une exécution réussie, puis télécharger l’artefact SMARE-windows.

## Format des données

Le CSV utilise les colonnes suivantes :

**id,CO2_ppm,latitude,longitude,altitude,temperature,humidite,pression**

Les valeurs de température, d’humidité ou de pression peuvent être absentes si le capteur ne les fournit pas. La page conserve alors le point et affiche les données disponibles.

## Technologies

- Python et Tkinter pour le programme PC
- Arduino pour l’acquisition et la transmission embarquées
- GitHub Actions pour générer l’exécutable Windows
- GitHub Pages, MapLibre GL JS et les tuiles OpenStreetMap pour la carte

Les mesures dépendent des capteurs, de leur calibration et des conditions de collecte. Elles sont destinées à l’observation du projet et doivent être interprétées avec ces limites.
