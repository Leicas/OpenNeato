# Analyse locale de navigation et de couverture

Prototype serveur, séparé du firmware et du frontend. Il collecte les cartes et
cycles conservés par Vacuula, recale les grilles sur une référence et produit un
rapport local. Il ne commande pas le robot et ne modifie pas Home Assistant.

## Installation et lancement

Depuis la racine du dépôt, avec Python 3 et PowerShell :

```powershell
python -m venv .venv-navigation
.\.venv-navigation\Scripts\python.exe -m pip install -r scripts/navigation/requirements.txt

# Conserver ces données domestiques dans un dossier privé hors du dépôt.
.\.venv-navigation\Scripts\python.exe scripts/navigation/collect.py --base-url http://localhost:8080 --output D:/private/neato/dataset.json
.\.venv-navigation\Scripts\python.exe -m scripts.navigation.analyze D:/private/neato/dataset.json --output D:/private/neato/report --serve --port 8765
```

Remplacer l'URL par celle du serveur Vacuula. Le serveur de visualisation écoute
uniquement sur localhost. Sans `--serve`, les fichiers du rapport sont simplement
écrits dans le dossier indiqué. Aucun identifiant de robot n'est à inscrire dans
le code. Ne pas publier le jeu de données : il contient le plan du logement et les
horaires des cycles.

Le rapport propose une carte de l'ancienneté des passages sur plusieurs jours,
les comparaisons par cycle et `analysis.json`. Sur les comparaisons, les murs du
cycle recalé sont orange et les passages cyan. Les passages d'un recalage dont
l'indice est inférieur à 0,8 sont exclus, même si le moteur a trouvé un candidat.
L'ancienneté de la synthèse est relative au dernier cycle disponible, et non à
l'heure d'ouverture du rapport. Elle utilise la fin du cycle, pas l'heure exacte
du passage. Les fichiers privés restent dans le dossier de
sortie ; aucun plan domestique n'est nécessaire dans Git.

## Fusion des plans

`consensus.png` compare le plan original, un consensus candidat et le nombre de
jours d'observation par cellule. `consensus-map.json` exporte ses classes en RLE.
Les cartes sont toutes recalées sur la référence originale, jamais sur le plan
fusionné. Seuls les recalages d'indice au moins 0,8 sont retenus.

L'inconnu s'abstient. Chaque jour UTC apporte au plus un vote pondéré par cellule.
Une classe libre ou obstacle devient stable après au moins deux jours décisifs
et 80 % d'accord. Les désaccords restent dans les diagnostics. L'auto-comparaison
de référence et les grilles similaires à au moins 99,5 % des cellules connues
sont exclues du renforcement du consensus. Cela réduit le poids des cartes
persistantes réutilisées, sans prouver l'indépendance de toutes les observations.

Le cadre est limité à celui de la référence. Une chaise jamais déplacée peut
rester un obstacle persistant : la fréquence seule ne prouve pas un mur. Ce plan
peut aider à distinguer les obstacles mobiles et à annoter les pièces ; il ne
remplace ni la carte du robot ni une mesure physique validée.

```powershell
.\.venv-navigation\Scripts\python.exe -m unittest discover -s scripts/navigation -p "test_*.py" -v
```

## Pièces et coordonnées

Le prototype n'invente pas les pièces. Sans `--zones`, la couverture concerne
l'étendue de la carte, sans nom de pièce. Pour des zones nommées, créer un fichier
JSON contenant une liste, puis ajouter `--zones D:/private/neato/zones.json` :

```json
[
  {
    "id": "kitchen",
    "name": "Cuisine",
    "polygon": [[10, 20], [40, 20], [40, 45], [10, 45]],
    "target_days": 1
  }
]
```

Cet exemple décrit uniquement le format, pas les coordonnées d'une vraie pièce.
Les sommets sont exprimés en cellules de la grille de référence : x vers la
droite, y vers le bas. La cellule `(x, y)` est évaluée en son centre
`(x + 0.5, y + 0.5)`. Aucune conversion en mètres carrés n'est faite tant que la
résolution physique de la carte n'est pas établie. Des zones qui se chevauchent
sont calculées séparément ; leurs fractions ne doivent pas être additionnées.

## Ce que les résultats prouvent

Le champ `RunMap` Gen3 indique où le robot est passé. Il ne prouve pas que
l'aspiration était active sur chaque cellule. Le collecteur le marque donc
`coverage_semantics: "traversed"` :

- `traversed_fraction` mesure la part de la zone traversée dans les cycles recalés
  et acceptés, sans compter deux fois leurs cellules communes.
- `cleaned_fraction` et `last_confirmed_complete_at` restent `null` avec ces seules
  données. Une surface libre dans un plan n'est jamais une preuve de nettoyage.
- Un cycle annulé ou incomplet reste `partial`, même si sa trajectoire couvre toute
  une zone. Un cycle terminé avec une trajectoire reste `traversed`.
- Un recalage refusé, une confiance inférieure au seuil, une sémantique inconnue
  ou une géométrie absente ne donnent pas de crédit : état `unknown` si aucune
  autre observation utilisable ne couvre la zone.

Les cellules hors des zones ou du cadre de référence sont ignorées. Les données
de cycles futurs, non terminés ou sans horodatage de fin sont exclues. Les raisons
figurent dans `rejected_runs`. L'analyse dépend de l'historique disponible ; une
absence d'observation n'est pas une preuve que le robot n'est jamais passé.

## Contrat de couverture et évolution Home Assistant

`coverage.analyze_coverage(runs, zones=None, reference_shape=(height, width))`
attend des masques déjà recalés et booléens, avec pour chaque cycle :

```python
{
    "id": "run-id",
    "status": "complete",  # ou canceled / cancelled / incomplete
    "ended_at": "2026-11-14T17:00:00Z",
    "registration": {"accepted": True, "confidence": 0.95},
    "coverage_mask": boolean_mask_in_reference_grid,
    "coverage_semantics": "traversed",
}
```

L'API accepte aussi des masques explicitement prouvés `cleaned`, ou une
`trajectory` continue en cellules avec `trajectory_semantics: "cleaning"`,
`trajectory_continuous: True` et `brush_radius_cells` connu. Le fournisseur de ces
données doit établir ces propriétés : ce ne sont pas des options pour convertir
une simple trajectoire Vacuula en nettoyage confirmé. Les trajets doivent être
séparés aux téléportations ou aux lacunes de mesure pour ne pas créer de faux
passages entre deux points.

Avec une preuve de nettoyage, `recent_cleaned_fraction` applique l'intervalle
`target_days` par cellule ; `suggested_priority` vaut la fraction sans observation
récente. Un cycle `complete` couvrant au moins 90 % d'une zone peut confirmer son
nettoyage, seuil configurable dans l'API. Un cycle partiel peut contribuer à la
couverture observée sans valider la zone entière. Avec une simple traversée,
`suggested_priority` reste `null` et `priority_basis` indique
`cleaning_state_unknown`.

Le JSON est une base de travail pour un futur capteur ou planificateur HA. Le
programme d'analyse n'installe aucun capteur, ne change aucune date de nettoyage
et ne lance aucune action. Il faudra conserver la distinction entre observation, achèvement
du cycle et nettoyage confirmé avant d'automatiser des reports de pièces.

## Intégration HA et notifications optionnelles

`home_assistant.yaml` est un package à installer séparément et volontairement dans
`/config/packages/neato_navigation.yaml`, avec l'inclusion des packages HA. Il
ajoute un capteur REST du service local, un helper de dernier événement notifié et
une automatisation de synchronisation chaque minute. Installer également
`ha_publisher.py` comme `/config/neato_navigation_publisher.py` et créer
`/config/neato_navigation_options.json` :

```json
{"service_base": "http://127.0.0.1:8765"}
```

Cette URL suppose que HA et le service utilisent le réseau de l'hôte. Valider la
configuration et sauvegarder les fichiers avant de recharger/redémarrer HA.

Le publisher réutilise la configuration YAML `notify` nommée `ntfy` déjà installée
dans HA, y compris ses références `!secret`. Aucun jeton n'est inscrit dans ce
dépôt. Il copie les images validées sous `/config/www/neato-navigation/`, soit les
URLs HA `/local/neato-navigation/latest.png` et `/local/neato-navigation/eink.png`.
Une carte Markdown peut ajouter `?v=<image_run_id>` pour éviter une image en cache.
Ces fichiers statiques sont accessibles à toute personne qui connaît leur URL HA :
ils contiennent une carte du logement et doivent rester dans le périmètre d'accès
souhaité.

Un statut de backfill sans `event_id`, ou avec `notifications_allowed: false`,
copie les images sans notification. Un nouveau cycle final autorisé déclenche un
upload PNG binaire vers le topic ntfy existant ; le serveur ntfy héberge alors la
pièce jointe, au lieu de demander au téléphone d'atteindre une URL LAN. Le statut
est relu après le téléchargement pour refuser des images provenant de générations
différentes. Le [protocole des pièces jointes ntfy](https://docs.ntfy.sh/publish/#attachments)
utilise `X-Filename`, `X-Title` et `X-Message`.
Le publisher s'identifie avec un `User-Agent` applicatif explicite : le nom par
défaut de `urllib` peut être refusé par le Browser Integrity Check Cloudflare
([erreur 1010](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/)).

La déduplication persiste dans
`/config/.storage/neato_navigation_publisher.json`. Une tentative est enregistrée
avant l'envoi : si la réponse HTTP est perdue, le statut reste `pending` et la même
notification n'est pas renvoyée automatiquement. Cela évite les doubles envois,
mais demande une vérification manuelle en cas de résultat ambigu. La commande fixe
`shell_command.neato_navigation_test_notification` envoie volontairement un test
marqué **TEST**, sans commander le robot ni consommer l'événement du vrai cycle.

## Inspection des cartes natives D6 hors ligne

`permap.py` lit les conteneurs natifs `TSAM`/`TCES` obtenus par export de
diagnostic et vérifie une reconstruction strictement identique :

```powershell
python -m scripts.navigation.permap C:\private\permap_0000000001.bin
```

La sortie JSON contient les tailles, IDs de sections et SHA-256. Les champs
inconnus sont conservés ; les sections non observées, doublons, longueurs
incohérentes et fichiers tronqués sont refusés. Ce lecteur n'a aucun accès
réseau et ne modifie pas ses entrées. Les dumps réels restent hors du dépôt.
Il n'est pas branché sur le service de couverture : les unités, le rôle de
toutes les couches et la procédure de restauration dans le robot ne sont pas
établis.

La fonction `floor_rows(container, width, height)` reproduit les classes
FloorMap après découpage à l'étendue fournie et inversion des lignes. Elle a
été validée pixel par pixel sur le snapshot natif correspondant au FloorMap
actif (143 664 pixels identiques). La largeur et la hauteur doivent provenir
d'une source indépendante ; elles ne sont pas déduites du fichier natif.
Cette conversion ne mesure ni la surface en mètres ni le nettoyage confirmé.

## Fusion native expérimentale et transport préparé

`native_fusion.py` recale les autres cartes sur une référence explicitement
choisie ; les cartes rejetées ne votent pas. Le robot ayant servi dans plusieurs
logements, une ressemblance ou un numéro de fichier ne suffit pas à les associer.
Les rendus de consensus restent des résultats d'analyse, pas une vérité terrain.

```powershell
python -m scripts.navigation.native_fusion PRIVATE/reference.bin PRIVATE/other.bin --output PRIVATE/fusion --candidate candidate.bin
```

Le candidat n'est jamais envoyé. La génération native est plus stricte que le
rendu : seule la valeur brute 128 peut changer ; deux votes exacts concordants
et aucun vote opposé sont requis. Une proposition de libre est aussi refusée
si une source place un mur dans son voisinage 3×3. La confirmation par voisinage
utilisée pour l'affichage ne compte pas comme une seconde observation exacte.
`--clear-changing` est désactivé. Les entrées et les candidats existants sont
protégés contre l'écrasement.

Ces conditions ne prouvent ni l'indépendance des acquisitions ni la cohérence
avec les sous-cartes/liaisons SLAM. Les artefacts en rayons restent visibles ;
aucune carte expérimentale n'est validée pour installation.

```powershell
python -m scripts.navigation.stage_payload PRIVATE/candidate.bin --output-dir PRIVATE/staging
```

Ce second outil produit un script marqué pour le hook de maintenance, avec une
archive compressée à membre unique et des contrôles SHA-256/taille. Le payload
final doit rester sous 65 536 octets. Il écrit uniquement dans un nouveau dossier
`/tmp/openneato-stage-<hash>` sur le robot, si un opérateur l'exécute plus tard.
Il n'a aucun client réseau, ne remplace aucune carte persistante et ne redémarre
pas le robot. Le hook et les commandes QNX doivent être validés indépendamment.
