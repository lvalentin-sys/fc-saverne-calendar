# FC Saverne S1 — calendrier iCal automatique

Source officielle suivie :
https://epreuves.fff.fr/competition/club/503964-f-c-saverne/equipe/2026_1975_SEM_1/resultat-calendrier

## Ce que fait le dépôt
- vérifie la page FFF chaque heure ;
- ajoute les nouvelles rencontres, y compris les tours de coupe qui apparaissent sur la page ;
- met à jour les dates et horaires ;
- ajoute le score dans le titre de l’événement quand le résultat est publié ;
- conserve un UID stable pour éviter les doublons ;
- ne remplace pas le calendrier si la FFF bloque temporairement une mise à jour.

## Mise en ligne
1. Déposer ces fichiers dans un dépôt GitHub public.
2. Onglet **Settings > Pages**.
3. Choisir **Deploy from a branch**, branche `main`, dossier `/docs`.
4. Après publication, ouvrir l’adresse GitHub Pages du dépôt puis toucher **S’abonner au calendrier**.

Le calendrier public est `docs/fc-saverne-s1.ics`.

## Test manuel
Dans GitHub : **Actions > Mise à jour calendrier FC Saverne > Run workflow**.

## Important
La nouvelle plateforme FFF peut appliquer une protection anti-bot. Le script utilise un vrai navigateur Chromium (Playwright) plutôt qu’un simple appel HTTP et conserve le dernier calendrier valide si une exécution échoue.
