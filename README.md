# CrowdRunning

CrowdRunning est un prototype d'aide à la décision destiné aux organisateurs de courses à pied. À partir d'un tracé GPX et de paramètres d'organisation, l'application simule les flux de coureurs, visualise les zones de congestion et aide à préparer le dispositif opérationnel de l'événement.

L'objectif est de mieux anticiper les difficultés de circulation, d'identifier les points sensibles du parcours et d'évaluer différents scénarios d'organisation avant le jour de la course.

## Fonctionnalités principales

- **Importation du parcours :** chargement d'un fichier GPX pour définir le tracé de la course.
- **Paramétrage des départs :** définition du nombre de participants, du nombre de vagues, de l'intervalle entre les départs et de la dispersion des allures.
- **Simulation des flux :** estimation des vitesses et des densités de coureurs le long du parcours.
- **Visualisation cartographique :** représentation du parcours, des équipements et des zones de congestion, avec animation des déplacements lorsque cette fonctionnalité est activée.
- **Analyse des points sensibles :** identification des secteurs où le débit devient contraint ou où des congestions peuvent apparaître.
- **Gestion des équipements :** positionnement et paramétrage des points d'eau, toilettes, postes de secours, médecins, ambulances, bénévoles et rétrécissements.
- **Analyse opérationnelle :** estimation des files d'attente, des besoins en équipements et des délais d'intervention selon les hypothèses retenues.
- **Vérification réglementaire :** contrôle de certaines exigences du règlement FFA 2026, selon le contexte de l'épreuve et les règles implémentées.
- **Export des résultats :** sauvegarde des scénarios et export des données utiles à leur analyse et à leur exploitation.

## Installation

### Prérequis

- Python 3.11 ou version ultérieure recommandé.
- Git, facultatif, si vous souhaitez cloner le dépôt.
- Un environnement Windows, macOS ou Linux compatible avec Python.

### 1. Récupérer le projet

Placez-vous dans le dossier contenant `app.py`.

### 2. Créer l'environnement virtuel

Sous Windows PowerShell :

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Sous macOS ou Linux :

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Installer les dépendances

Si le fichier `requirements.txt` est présent à la racine du projet :

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 4. Lancer l'application

```bash
python -m streamlit run app.py
```

L'application s'ouvre dans votre navigateur. Si la page ne s'ouvre pas automatiquement, utilisez l'adresse locale indiquée dans le terminal.

**Important :** si vous déplacez le dossier du projet, recréez l'environnement virtuel `.venv`. Celui-ci contient des chemins propres à son emplacement et ne doit pas être considéré comme portable.

## Guide d'utilisation

### 1. Importer le parcours

Importez un fichier GPX correspondant au tracé réel de l'épreuve. Vérifiez que le parcours est correctement représenté sur la carte avant de lancer une simulation.

### 2. Configurer le scénario

Dans la barre latérale, définissez les principaux paramètres de l'événement : effectif, organisation des départs, allures, conditions météorologiques et paramètres de simulation disponibles.

Dans l'éditeur de scénario, sélectionnez un type d'équipement puis cliquez sur la carte pour le positionner. Selon les objets disponibles, vous pouvez ajuster leurs paramètres, modifier leur emplacement ou les supprimer.

### 3. Lancer la simulation

Après avoir vérifié les paramètres, lancez la simulation. Les résultats dépendent notamment de l'effectif, de la répartition des allures, des vagues de départ, de la géométrie du parcours et des équipements configurés.

### 4. Analyser les résultats

Examinez les représentations cartographiques et les indicateurs proposés pour repérer les secteurs sensibles, comprendre l'évolution des flux et identifier les situations nécessitant une attention particulière.

Les estimations de files d'attente et de moyens opérationnels doivent être interprétées au regard des hypothèses utilisées.

### 5. Enregistrer et exporter

Lorsque les fonctionnalités correspondantes sont disponibles, sauvegardez votre scénario pour le reprendre ultérieurement et exportez les résultats afin de les analyser ou de les intégrer à votre préparation opérationnelle.

## Comprendre les niveaux de densité

Les seuils suivants constituent des repères de lecture utilisés par l'application. Ils ne correspondent pas à des seuils réglementaires universels.

| Densité estimée | Interprétation |
|---|---|
| Inférieure à 0,3 coureur/m² | **Flux fluide** : circulation relativement peu dense |
| De 0,3 à 0,5 coureur/m² | **Flux contraint** : interactions plus fréquentes entre les coureurs |
| Supérieure à 0,5 coureur/m² | **Congestion** : secteur à examiner attentivement |

Ces valeurs sont des indicateurs de simulation. Elles ne suffisent pas, à elles seules, à caractériser le niveau de sécurité d'une situation réelle.

## Architecture du projet

Les principaux modules et leurs responsabilités sont les suivants, sous réserve de leur présence dans la version utilisée.

| Fichier | Rôle |
|---|---|
| `app.py` | Interface Streamlit, paramétrage, affichage cartographique, résultats et exports. |
| `meso.py` | Moteur de simulation mésoscopique des flux de coureurs. |
| `rules.yaml` | Définition des règles réglementaires prises en charge par l'application. |
| `rules.py` | Évaluation des règles et contrôle du scénario. |
| `plan_operationnel.py` | Génération du plan opérationnel et de ses documents de synthèse. |
| `test_rules.py` | Tests du moteur de règles et vérification des citations lorsque le PDF de référence est fourni. |
| `queues.py` | Modélisation des files d'attente et estimation des délais d'intervention. |
| `scenario.py` | Gestion des objets du scénario, de leur positionnement et des échanges GeoJSON. |
| `simulator.py` | Outils de lecture et de projection du parcours et fonctionnalités de simulation microscopique expérimentales. |

Les dépendances exactes et les responsabilités de chaque module peuvent évoluer avec les versions du projet. Consultez le code et le fichier `requirements.txt` pour connaître l'architecture effectivement déployée.

## Principes de modélisation

La simulation repose sur des hypothèses destinées à représenter les déplacements collectifs des coureurs à une échelle adaptée à l'analyse d'un événement.

Dans le modèle mésoscopique décrit par le projet, les participants progressent le long du parcours et leur vitesse dépend notamment de la densité locale. La relation vitesse-densité s'inspire de travaux de modélisation des déplacements piétonniers, notamment ceux associés à Weidmann.

Les vagues de départ, les points de service et les rétrécissements peuvent modifier les flux selon les mécanismes implémentés.

Les résultats sont des estimations produites par le modèle, et non des prédictions certaines du comportement de chaque participant.

## Limites et précautions d'utilisation

CrowdRunning est un **prototype d'aide à la décision**. Il ne constitue pas un outil certifié de sécurité et ne remplace ni une étude spécialisée, ni une reconnaissance de terrain, ni la validation des dispositifs par les autorités et les professionnels compétents.

Les principales limites à prendre en compte sont les suivantes :

- **Modélisation simplifiée :** les relations entre densité, vitesse et comportement des coureurs reposent sur des hypothèses qui doivent être confrontées à des observations réelles.
- **Représentation du parcours :** une modélisation principalement longitudinale ne reproduit pas toutes les interactions latérales, les dépassements, les obstacles et les comportements individuels.
- **Équipements :** leur effet sur la circulation dépend des mécanismes effectivement intégrés. Un équipement affiché ou comptabilisé n'influence pas nécessairement la dynamique de la foule.
- **Files d'attente :** les estimations reposent sur des hypothèses de durée de service, de capacité et de répartition des arrivées. Les comportements réels peuvent être différents.
- **Secours :** les délais calculés dépendent des hypothèses d'incident, de mobilisation, de déplacement et de disponibilité des équipes.
- **Réglementation :** les contrôles couvrent uniquement les exigences implémentées et ne constituent pas une vérification exhaustive de la conformité de l'événement.

Le règlement FFA 2026 ne représente qu'une partie du cadre applicable. Selon l'épreuve et son contexte, d'autres dispositions peuvent devoir être prises en compte, notamment le Code du sport, les arrêtés préfectoraux ou municipaux et les prescriptions des autorités compétentes.

Les seuils de densité, les délais cibles et les tolérances opérationnelles définis dans l'application ne doivent pas être présentés comme des obligations réglementaires lorsqu'ils ne le sont pas.

## Développements envisagés

Les pistes d'évolution du projet comprennent notamment :

1. **Amélioration de la représentation spatiale :** prise en compte plus fine de la largeur des voies, des obstacles et de la géométrie locale.
2. **Calibrage sur des données réelles :** comparaison des résultats simulés aux chronométrages, aux effectifs et aux observations de terrain.
3. **Enrichissement des comportements :** modélisation plus détaillée des interactions entre coureurs, équipements, incidents et bénévoles.
4. **Aide à la décision :** recommandations de positionnement des équipements et identification automatique des secteurs à examiner.
5. **Comparaison de scénarios :** évaluation de plusieurs organisations de départ, répartitions des moyens et configurations de parcours.
6. **Amélioration des restitutions :** rapports opérationnels et exports facilitant la préparation et le suivi de l'événement.

## Avertissement

Les résultats fournis par CrowdRunning doivent être considérés comme des éléments d'aide à la réflexion. Toute décision concernant la sécurité, les moyens de secours, les fermetures de voies ou l'organisation d'une manifestation doit être validée par les personnes et autorités compétentes.

**CrowdRunning : simuler les flux pour mieux anticiper les contraintes d'organisation.**
