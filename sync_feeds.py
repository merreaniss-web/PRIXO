"""Synchronisation de flux JSON partenaires autorisés.

Chaque flux doit déjà fournir des lignes normalisées selon imports/template.csv.
Les structures propres à Carrefour, E.Leclerc, Intermarché ou Auchan doivent être
mappées selon le contrat du flux/API réellement obtenu auprès de l'enseigne.
"""
import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_BASE = os.getenv("PRIXO_API_BASE", "http://127.0.0.1:8000").rstrip("/")
ADMIN_KEY = os.getenv("PRIXO_ADMIN_API_KEY", "")
RETAILERS = {
    "Carrefour": ("PRIXO_CARREFOUR_FEED_URL", "PRIXO_CARREFOUR_FEED_TOKEN"),
    "E.Leclerc": ("PRIXO_LECLERC_FEED_URL", "PRIXO_LECLERC_FEED_TOKEN"),
    "Intermarché": ("PRIXO_INTERMARCHE_FEED_URL", "PRIXO_INTERMARCHE_FEED_TOKEN"),
    "Auchan": ("PRIXO_AUCHAN_FEED_URL", "PRIXO_AUCHAN_FEED_TOKEN"),
}

if not ADMIN_KEY:
    sys.exit("Erreur : définis PRIXO_ADMIN_API_KEY avant la synchronisation.")

results = []
for retailer, (url_key, token_key) in RETAILERS.items():
    url = os.getenv(url_key, "").strip()
    if not url:
        print(f"{retailer}: flux non configuré (ignoré).")
        continue
    token = os.getenv(token_key, "").strip()
    headers = {"Accept": "application/json", "User-Agent": "PRIXO-DataSync/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=40) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("items", payload.get("products", payload.get("results", [])))
        if not isinstance(rows, list):
            raise ValueError("Le flux JSON ne renvoie pas une liste items/products/results.")
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item.setdefault("retailer", retailer)
            item.setdefault("source_name", f"flux-partenaire-{retailer}")
            normalized.append(item)
        body = json.dumps({"items": normalized}, ensure_ascii=False).encode("utf-8")
        request = Request(API_BASE + "/api/admin/import/json", data=body, headers={"Content-Type": "application/json", "X-Admin-Key": ADMIN_KEY}, method="POST")
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
        result["retailer"] = retailer
        results.append(result)
        print(f"{retailer}: {result.get('prices_added', 0)} prix ajoutés, {result.get('rows_rejected', 0)} lignes rejetées.")
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f"{retailer}: échec de synchronisation : {exc}")
        results.append({"retailer": retailer, "error": str(exc)})

if not results:
    print("Aucun flux de distributeur n'est configuré. Utilise l'import CSV ou configure les URL de flux partenaires.")
    sys.exit(2)
if any(x.get("error") or x.get("rows_rejected", 0) for x in results):
    sys.exit(1)
