# La Gazette de Janzé

Le journal de bord de Janzé (35150) : conseil municipal, travaux et circulation, grands projets, urbanisme, alertes et presse locale.

Site statique publié avec GitHub Pages : https://florentmoulin35.github.io/gazette-janze/

- `index.html` : l'application (une seule page, données incluses)
- `manifest.webmanifest` et `icones/` : pour l'ajouter à l'écran d'accueil du téléphone

## Mise à jour automatique

Chaque nuit à 4 h UTC, `.github/workflows/collecte.yml` lance `scripts/collecter.py`, qui lit :

- les actualités de janze.fr,
- les articles de presse qui citent Janzé (Google Actualités : Ouest-France, Journal de Vitré…),
- les restrictions d'eau VigiEau pour Janzé,
- les flux RSS de Roche aux Fées Communauté.

Le résultat est écrit dans `donnees/auto.js`, que `index.html` charge au démarrage. Les infos saisies à la main dans `index.html` restent affichées. Pour lancer une collecte tout de suite : onglet **Actions › Collecte de la nuit › Run workflow**.

## Données d'urbanisme (à lancer à la main)

`scripts/filtrer_urbanisme_janze.py` télécharge les gros fichiers nationaux de data.gouv.fr et n'en garde que les lignes de Janzé (code INSEE 35136), dans des CSV légers. Il lit les fichiers par morceaux avec pandas, sans jamais les charger en entier.

```sh
pip install pandas
python scripts/filtrer_urbanisme_janze.py --sitadel   # permis de construire, d'aménager, de démolir (Sitadel)
python scripts/filtrer_urbanisme_janze.py --dvf       # ventes immobilières (DVF géolocalisées)
python scripts/filtrer_urbanisme_janze.py --entree fichier.csv --colonne commune --valeur "Janzé"
```

En local, les résultats vont dans `donnees_janze/` (non versionné).

Sans rien installer : onglet **Actions › Données d'urbanisme › Run workflow**. GitHub lance le script et enregistre les CSV de Janzé dans `donnees/urbanisme/`. `scripts/resumer_urbanisme.py` en tire ensuite `donnees/urbanisme.js` (quelques Ko), affiché en bas de la rubrique **Urbanisme** : logements autorisés par an, derniers permis, prix des ventes.
