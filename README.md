# PRIXO — application de comparaison par EAN et magasin

Ce projet transforme le prototype HTML en une application **frontend + API + base de données**. Le frontend garde le design PRIXO et gère la recherche, les photos de produit, la marque, le panier et la localisation. L'API stocke les produits, magasins et prix datés, puis compare les articles par EAN exact.

## Ce que le code fait déjà

- Recherche les produits réellement chargés dans le catalogue PRIXO.
- Affiche la marque dans le nom si elle n'y est pas déjà, et propose un bouton pour afficher les produits de cette marque.
- Priorise les images transmises dans le flux catalogue ; la photo est affichée seule dans un cadre propre (`object-fit: contain`).
- Importe les catalogues/prix à partir de CSV avec EAN, prix daté, enseigne et magasin.
- Compare un prix par article et un total par magasin. Un total n'est affiché que si le magasin possède un prix pour **tous** les articles du panier.
- Filtre les résultats à 25 km si la géolocalisation est activée et si le magasin a des coordonnées.
- Rejette les montants invalides, les devises différentes de l'euro et les dates manquantes/incorrectes. N'invente aucun prix.
- Affiche les enseignes connectées et le nombre de lignes de prix importées.

## Limite importante : les catalogues des enseignes

Le code ne peut pas deviner ni télécharger automatiquement des bases privées. Carrefour, E.Leclerc, Intermarché et Auchan n'exposent pas forcément leurs bases complètes prix + magasins au public par une API universelle. Les prix peuvent dépendre du magasin/Drive, du mode de retrait et de la date. Une intégration en direct nécessite un flux/API autorisé ou un accord d'accès propre à chaque enseigne.

**Aucun vrai prix n'est livré dans ce projet.** Le fichier `imports/template.csv` ne contient que les en-têtes. Pour que le comparateur retourne des prix réels, il faut importer les données issues d'un flux fournisseur/partenaire autorisé ou d'un fichier que l'enseigne t'a fourni. Une connexion directe aux sites n'est pas activée par ce code ; les pages HTML ne constituent pas une API stable et peuvent nécessiter un magasin, une session ou un compte. Le code ne tente pas de contourner ces protections.

## Lancer en local

Pré-requis : Python 3.11 ou plus récent.

1. Ouvre un terminal dans le dossier `PRIXO_PROJECT`.
2. Installe les dépendances :

   ```bash
   python -m venv .venv
   # Windows : .venv\Scripts\activate
   # macOS/Linux : source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Définis une clé administrateur forte dans le terminal, par exemple :

   ```bash
   # macOS/Linux
   export PRIXO_ADMIN_API_KEY='remplace-par-une-cle-longue-et-aleatoire'
   # Windows PowerShell
   $env:PRIXO_ADMIN_API_KEY='remplace-par-une-cle-longue-et-aleatoire'
   ```

4. Lance l'API :

   ```bash
   uvicorn server:app --reload --port 8000
   ```

5. Ouvre `frontend/index.html` via un petit serveur web local, pas en double-cliquant le fichier :

   ```bash
   python -m http.server 8080 --directory frontend
   ```

6. Ouvre `http://127.0.0.1:8080`. L'API est documentée sur `http://127.0.0.1:8000/docs`.
7. Pour importer les données, ouvre `http://127.0.0.1:8080/import.html`, saisis la même clé dans la page admin et importe le CSV.

Le backend et le frontend sont deux services distincts. Une page GitHub Pages ne peut pas exécuter FastAPI ni stocker cette base SQLite.

## Publier sur GitHub Pages + un hébergeur API

1. Déploie `server.py` et `requirements.txt` sur un hébergeur qui exécute Python/ASGI (ou Docker). Configure `PRIXO_ADMIN_API_KEY`, `PRIXO_CORS_ORIGINS` et un stockage persistant pour `PRIXO_DATABASE_PATH`.
2. Copie l'URL HTTPS de l'API.
3. Modifie `frontend/config.js` pour qu'il contienne cette URL :

   ```js
   window.PRIXO_API_BASE = "https://URL-HTTPS-DE-TON-API";
   ```

4. Mets les trois fichiers `frontend/index.html`, `frontend/config.js` et `frontend/import.html` à la racine de ton dépôt GitHub Pages (ou adapte les chemins si tu les mets dans un sous-dossier).
5. Sur le backend, configure `PRIXO_CORS_ORIGINS` avec l'URL exacte de ton site GitHub Pages, plutôt que `*` en production.
6. N'inscris **jamais** `PRIXO_ADMIN_API_KEY` dans le HTML, `config.js` ou dans GitHub Pages. Seule la page d'import demande la clé à l'administrateur lors de l'import.
7. Sur un hébergeur avec système de fichiers éphémère, configure un volume persistant ou une base de données persistante pour éviter de perdre les prix au redémarrage.

## Format CSV

Le modèle `imports/template.csv` contient les en-têtes seulement. Champs requis par ligne :

- `ean` : code EAN réel de 8, 12, 13 ou 14 chiffres
- `product_name` : nom de produit
- `retailer` : `Carrefour`, `E.Leclerc`, `Intermarché` ou `Auchan` (d'autres enseignes sont également acceptées)
- `store_name` : nom exact du magasin ou du Drive
- `price` : prix en euros, positif
- `price_date` : date du prix (`YYYY-MM-DD` ou `JJ/MM/AAAA`)

Champs recommandés : `brand`, `quantity`, `image_url`, `product_url`, `store_id`, `city`, `postal_code`, `latitude`, `longitude`, `store_url`, `promo`, `source_name`, `source_url`. Latitude et longitude doivent être fournies ensemble. Pour une comparaison de proximité, il faut des coordonnées correctes.

Les lignes rejetées et leur motif sont renvoyés après import. Les doublons stricts (même EAN, magasin, prix, date et source) sont ignorés.

## Vérifications

```bash
python -m compileall server.py
```

Le code a été écrit pour être testé avec des données réelles importées. L'exécution d'un test réseau final nécessite que l'API soit lancée sur l'environnement où l'application sera hébergée, et que les flux/données d'enseignes soient disponibles.

## Synchronisation automatique de flux partenaires JSON

Pour un flux/API partenaire autorisé, configure les variables `PRIXO_CARREFOUR_FEED_URL`, `PRIXO_LECLERC_FEED_URL`, `PRIXO_INTERMARCHE_FEED_URL` et/ou `PRIXO_AUCHAN_FEED_URL`, ainsi que la variable `*_FEED_TOKEN` si le partenaire exige un jeton. Puis lance :

```bash
python sync_feeds.py
```

Le JSON doit renvoyer une liste ou un objet `{ "items": [...] }` de lignes au format du CSV modèle. Chaque ligne doit contenir un EAN réel, un prix en EUR, un nom de magasin et une date de prix. Les formats propriétaires doivent être transformés par un adaptateur propre à l'API/le flux avant l'import. Les URL ne sont **pas** des URL génériques de page web : il faut l'URL API/flux réellement autorisée par le fournisseur.
