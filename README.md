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
