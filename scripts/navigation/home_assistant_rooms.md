# Commandes manuelles des pièces

Le template `home_assistant_rooms.yaml` ajoute un lecteur REST des cartes et
quatre scripts manuels. Il utilise le service existant
`rest_command.vacuula_robot_command`. Il n'ajoute aucun horaire et ne lance aucun
nettoyage au chargement ou au rechargement.

Les identifiants du logement restent dans un JSON privé hors du dépôt :

```json
{
  "map_id": "2020-01-02T03:04:05Z",
  "maps_url": "http://localhost/api/robot/test-robot/maps",
  "boundaries": {
    "cuisine": "00000000-0000-4000-8000-000000000001",
    "chambre": "00000000-0000-4000-8000-000000000002",
    "entree": "00000000-0000-4000-8000-000000000003"
  }
}
```

Ces valeurs illustrent le format uniquement. Lire les vrais identifiants via
Vacuula et vérifier les trois polygones activés avant installation.

```powershell
python -m scripts.navigation.rooms D:/private/neato/rooms.json --output D:/private/neato/neato_rooms.yaml
python -m unittest discover -s scripts/navigation -p test_rooms.py -v
```

Après sauvegarde de la configuration et du dashboard, le YAML rendu peut être
installé comme package HA. Valider la configuration, puis recharger
`rest_command` et `script` ; aucun redémarrage HA n'est requis pour ces deux
domaines. Ne pas créer aussi les mêmes scripts dans `scripts.yaml`.

Avant un départ demandé par l'utilisateur, le script commun vérifie : robot
connecté, au repos, sur sa base, sans erreur ni alerte ; une seule carte activée et
présente dans la liste, avec l'identifiant attendu ; le polygone demandé activé
sur cette carte. Un refus ou une réponse HTTP incorrecte arrête la demande.

La commande Gen3 cible une seule pièce avec `category: 4`, `mapId` et
`boundaryId`, en turbo et navigation profonde. Il n'y a aucun secours sans carte,
aucune relance automatique et aucune date de pièce nettoyée mise à jour à la
simple acceptation du départ. La fin du cycle et la couverture restent à suivre
séparément. Le mode `single` du script commun évite deux demandes simultanées.

Pour revenir en arrière : retirer les trois boutons ajoutés et le package privé,
puis recharger `script` et `rest_command`. Les scripts, horaires et capteurs qui
existaient auparavant sont indépendants et ne sont pas remplacés.
