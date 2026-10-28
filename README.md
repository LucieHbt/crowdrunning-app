# CrowdRunning — prototype

Aide à la décision pour préparer un événement de course à pied : on pose les équipements sur une carte, on simule l'écoulement des coureurs et on lit les zones de congestion.

## Installation
Python 3.11 ou plus récent recommandé.

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Utilisation
1. Importer un tracé GPX (sinon un petit parcours de démonstration est utilisé).
2. Dans **Éditeur de scénario**, choisir un type d'objet puis cliquer sur la carte pour le poser : point d'eau, poste d'épongeage, toilettes, poste de secours, médecin, ambulance, bénévoles, rétrécissement. Les paramètres (capacité, durée, largeur…) se modifient dans le tableau ; on peut aussi supprimer des objets.
3. Régler les paramètres de la barre latérale, puis **Simuler et animer**.
4. Lire le ruban de densité, la carte animée, la carte de chaleur espace-temps, les files d'attente (points d'eau, toilettes) et les délais d'intervention des secours.
   L'onglet **📜 Conformité** (4ᵉ onglet) confronte le scénario au règlement FFA 2026 : anneau de synthèse, avancement par thème, échéances administratives, points à traiter en priorité, puis détail règle par règle (article, page, citation) avec export CSV de la checklist. Des pastilles sur les onglets signalent le nombre d'objets posés et d'écarts. Le contexte de l'épreuve (route ou trail, voie publique, etc.) se règle dans la barre latérale, rubrique « Réglementation » ; l'onglet se met à jour aussitôt.
5. Enregistrer le scénario (GeoJSON, tracé + objets) pour le recharger plus tard ; télécharger la synthèse JSON et les trajectoires CSV.

## Architecture
- `app.py` : interface Streamlit (éditeur de carte, paramètres, résultats, exports).
- `meso.py` : moteur mésoscopique. Tous les partants sont suivis en 1D le long du parcours ; la densité de chaque tronçon (≈ 10 m) est calculée sur l'effectif complet, puis la vitesse de chaque coureur en découle (relation vitesse-densité inspirée de Weidmann). Gère vagues, points d'eau avec files d'attente, rétrécissements.
- `rules.yaml` + `rules.py` : moteur de règles. `rules.yaml` contient les règles du règlement FFA 2026, chacune avec son article, sa page imprimée, une courte citation, ses conditions d'application et son type de contrôle ; `rules.py` les évalue sans aucun seuil codé en dur. Pour changer une règle ou en ajouter une, on modifie seulement `rules.yaml`.
- `plan_operationnel.py` : génération du **Plan Opérationnel** (PDF) depuis l'onglet Résultats : fermetures de routes, personnel, points d'eau, scénarios de repli, médias, flux aux intersections, services d'urgence, conformité. Nécessite `reportlab` et `matplotlib`.
- `test_rules.py` : tests du moteur ; avec `REGLEMENT_PDF=<chemin du PDF>`, il vérifie aussi que chaque citation figure bien dans le règlement.
- `queues.py` : files d'attente des toilettes avant le départ (FIFO multi-cabines) et modèle de secours (incidents tirés dans l'occupation simulée, Monte Carlo, temps d'intervention).
- `scenario.py` : objets posés sur la carte, accrochage au tracé, bilan d'équipements, export/import GeoJSON.
- `simulator.py` : lecture GPX, projection locale, moteur microscopique 2D (non utilisé pour les densités ; réservé à de futures micro-simulations locales).

## Ce qui influence la simulation aujourd'hui
- **Points d'eau** posés (postes de service simultanés, durée de service) : files d'attente et ralentissements. Sans point d'eau posé, un point automatique est placé tous les 5 km.
- **Rétrécissements** posés (largeur utile, longueur) : la largeur locale remplace celle du couloir.
- **Toilettes** posées à moins de 500 m du départ : file d'attente avant le départ (attente, part d'usagers en retard pour leur vague, cabines estimées nécessaires pour que 95 % attendent moins que la tolérance). Les toilettes plus loin sur le parcours sont comptées dans le bilan mais pas simulées.
- **Postes de secours** posés (équipes, délai de mobilisation) : incidents tirés selon le taux pour 1 000 partants et l'occupation réelle du parcours, délai d'intervention (mobilisation + trajet le long du tracé + attente si les équipes sont occupées), 200 tirages Monte Carlo, profil de délai le long du parcours.
- Les **bénévoles**, **médecins**, **ambulances** et **postes d'épongeage** sont comptés dans le bilan et contrôlés par le moteur de règles, sans effet sur la foule.
- Toilettes et secours n'agissent pas en retour sur la foule (un incident ne bloque pas le parcours).

## Limites importantes
- Prototype pédagogique, pas un outil certifié de sécurité. Les sorties ne remplacent ni une analyse de sécurité ni une validation terrain.
- La relation vitesse-densité est une adaptation illustrative inspirée de Weidmann ; elle n'est ni la reproduction exacte du modèle original ni calibrée sur des mesures.
- Modèle 1D : le couloir est décrit par sa largeur, sans obstacles 2D ; les dépassements ne sont pas limités par la largeur.
- Les coureurs en attente à un point d'eau sont comptés en amont du poste (≈ 2 pers./m²) ; le comportement réel peut différer fortement.
- La zone d'arrivée (file après la ligne) n'est pas modélisée.
- Toilettes et secours reposent sur des hypothèses à calibrer (fenêtre d'arrivée, durée d'usage, vitesse des équipes, durée de prise en charge, part d'incidents à l'arrivée). Personne ne renonce à la file ni ne change de site en cours d'attente ; les équipes ne sont pas gênées par les coureurs et se déplacent uniquement le long du tracé.
- Le tracé est converti dans un repère local (projection équirectangulaire), correcte sur quelques kilomètres ; une projection UTM est prévue.
- Le moteur de règles ne reprend que ce que contient le **règlement FFA 2026** (Reglementation_Running_2026.pdf). Il ne remplace ni le Code du sport, ni les arrêtés préfectoraux ou municipaux, ni l'avis de la FFA et de la préfecture.
- Ce règlement **ne fixe aucune règle** sur les toilettes, la densité de coureurs, la largeur de couloir (hors cross), ni de délai d'intervention des secours sur route (les 30 min concernent les trails) ; il ne chiffre pas non plus le nombre de secouristes ou d'ambulances sur route (« un nombre adapté »). Les seuils 0,3 / 0,5 coureur/m², le délai visé et l'attente tolérée aux toilettes sont ceux de l'application.
- Certaines règles du texte disent « environ » (ravitaillement vers le km 5 puis tous les 5 km, épongeage à mi-chemin) : l'application les interprète avec une tolérance (± 1 km, ± 25 %), indiquée à l'écran et modifiable dans `rules.yaml`.
- Les règles qui ne se vérifient pas sur la carte (licences, signaleurs, liaisons radio, zones de propreté…) apparaissent comme « à confirmer » avec une case à cocher ; la confirmation n'est pas conservée dans le fichier de scénario.
- Les dates limites de demande de label (art. II.B.1) et les règles des labels, relais, sas élite, mesurage détaillé et courses à obstacles ne sont pas évaluées.

## Prochaines étapes
1. Projection UTM et couloir 2D (Shapely) avec largeurs issues d'OSM.
2. Rôle des bénévoles (signalisation, ravitaillement, fermeture de voirie) et effet des incidents sur la foule.
3. Compléter le moteur de règles avec les autres textes (Code du sport, arrêtés préfectoraux, guides sur les sanitaires) : ils s'ajoutent dans `rules.yaml`.
4. Recommandations automatiques (par exemple placer les points d'eau manquants), comparaison de scénarios, rapport PDF.
5. Calibration sur observations réelles.