"""PRIXO API: catalogue, prix par magasin, comparaison de panier et import de données autorisées.

Les prix sont fournis par des flux que l'exploitant est autorisé à utiliser, ou importés en CSV.
Ce serveur ne contourne pas les protections ni les accès privés des enseignes.
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("PRIXO_DATABASE_PATH", str(APP_DIR / "prixo.sqlite3")))
ADMIN_API_KEY = os.getenv("PRIXO_ADMIN_API_KEY", "change-me-local-only")
PRICE_MAX_AGE_DAYS = int(os.getenv("PRIXO_PRICE_MAX_AGE_DAYS", "90"))

app = FastAPI(
    title="PRIXO API",
    description=(
        "API de comparaison de prix par EAN et par magasin. Les prix proviennent des imports "
        "de flux partenaires autorisés ou de fichiers CSV sourcés. Aucun prix n'est inventé."
    ),
    version="1.0.0",
)
origins = [x.strip() for x in os.getenv("PRIXO_CORS_ORIGINS", "*").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Key", "Authorization"],
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_text(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", value.casefold()).strip()


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.create_function("norm", 1, normalize_text)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                ean TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                brand TEXT NOT NULL DEFAULT '',
                quantity TEXT NOT NULL DEFAULT '',
                image_url TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                product_url TEXT NOT NULL DEFAULT '',
                source_name TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stores (
                store_id TEXT PRIMARY KEY,
                retailer TEXT NOT NULL,
                store_name TEXT NOT NULL,
                city TEXT NOT NULL DEFAULT '',
                postal_code TEXT NOT NULL DEFAULT '',
                latitude REAL,
                longitude REAL,
                store_url TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ean TEXT NOT NULL REFERENCES products(ean) ON UPDATE CASCADE ON DELETE CASCADE,
                store_id TEXT NOT NULL REFERENCES stores(store_id) ON UPDATE CASCADE ON DELETE CASCADE,
                price REAL NOT NULL CHECK(price > 0),
                currency TEXT NOT NULL DEFAULT 'EUR',
                price_date TEXT NOT NULL,
                is_promo INTEGER NOT NULL DEFAULT 0,
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL DEFAULT '',
                imported_at TEXT NOT NULL,
                UNIQUE(ean, store_id, price_date, price, source_name)
            );
            CREATE INDEX IF NOT EXISTS idx_prices_ean_date ON prices(ean, price_date DESC);
            CREATE INDEX IF NOT EXISTS idx_prices_store_date ON prices(store_id, price_date DESC);
            CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);
            """
        )


init_db()


class CompareItem(BaseModel):
    ean: str = Field(min_length=1, max_length=40)
    quantity: int = Field(default=1, ge=1, le=99)


class CompareRequest(BaseModel):
    items: list[CompareItem] = Field(min_length=1, max_length=100)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_km: float = Field(default=25, ge=1, le=250)
    max_age_days: int = Field(default=PRICE_MAX_AGE_DAYS, ge=1, le=730)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, a)))


def as_date(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("date de prix manquante")
    # Accepter YYYY-MM-DD, ISO-8601 et JJ/MM/AAAA.
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"date de prix invalide : {raw}")


def first(row: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def parse_bool(value: str) -> int:
    return 1 if normalize_text(value) in {"1", "true", "oui", "yes", "promo", "promotional"} else 0


def row_to_normalized(row: dict[str, Any], source_name: str) -> dict[str, Any]:
    ean = re.sub(r"\s+", "", first(row, "ean", "ean_13", "ean13", "gtin_13", "code_barres", "code_barre", "barcode", "code", "product_code", "gtin"))
    # EAN-8/UPC-A/EAN-13/GTIN-14 acceptés sans réécriture du code.
    if not ean or not ean.isdigit() or len(ean) not in (8, 12, 13, 14):
        raise ValueError("EAN invalide (8, 12, 13 ou 14 chiffres requis)")
    name = first(row, "product_name", "nom_produit", "nom_du_produit", "produit", "nom", "article", "name", "title", "product_title")
    retailer_input = first(row, "retailer", "enseigne", "chain", "merchant")
    retailer_norm = normalize_text(retailer_input)
    if "carrefour" in retailer_norm:
        retailer = "Carrefour"
    elif "leclerc" in retailer_norm:
        retailer = "E.Leclerc"
    elif "intermarche" in retailer_norm:
        retailer = "Intermarché"
    elif "auchan" in retailer_norm:
        retailer = "Auchan"
    else:
        retailer = retailer_input
    store_name = first(row, "store_name", "nom_magasin", "nom_du_magasin", "magasin", "store", "location_name", default=retailer)
    price_text = first(row, "price", "prix", "current_price")
    price_text = price_text.replace("€", "").replace("\u00a0", "").replace(" ", "").replace(",", ".")
    price = float(price_text)
    if not math.isfinite(price) or price <= 0 or price > 100000:
        raise ValueError("prix invalide")
    if not name:
        raise ValueError("nom de produit manquant")
    if not retailer or not store_name:
        raise ValueError("enseigne ou nom du magasin manquant")
    currency = first(row, "currency", "devise", default="EUR").upper()
    if currency != "EUR":
        raise ValueError("seuls les prix en EUR sont acceptés")
    price_date = as_date(first(row, "price_date", "date_prix", "date_de_prix", "date_releve", "date_du_prix", "date", "observed_at"))
    lat_raw = first(row, "latitude", "lat")
    lon_raw = first(row, "longitude", "lon", "lng")
    lat = float(lat_raw) if lat_raw else None
    lon = float(lon_raw) if lon_raw else None
    if (lat is None) != (lon is None):
        raise ValueError("latitude et longitude doivent être fournies ensemble")
    if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordonnées invalides")
    store_code = first(row, "store_id", "store_code", "code_magasin", "location_id")
    if not store_code:
        store_code = "-".join(filter(None, [normalize_text(retailer).replace(" ", "-"), normalize_text(store_name).replace(" ", "-"), normalize_text(first(row, "postal_code", "code_postal")).replace(" ", "-")]))
    store_id = f"{retailer.strip()}::{store_code}"
    return {
        "ean": ean,
        "name": name,
        "brand": first(row, "brand", "marque"),
        "quantity": first(row, "quantity", "format", "size", "conditionnement"),
        "image_url": first(row, "image_url", "image", "product_image"),
        "category": first(row, "category", "categorie", "rayon"),
        "product_url": first(row, "product_url", "url_produit", "url_du_produit", "product_page"),
        "retailer": retailer,
        "store_name": store_name,
        "store_id": store_id,
        "city": first(row, "city", "ville"),
        "postal_code": first(row, "postal_code", "code_postal", "postcode"),
        "latitude": lat,
        "longitude": lon,
        "store_url": first(row, "store_url", "url_magasin", "store_page"),
        "price": price,
        "currency": currency,
        "price_date": price_date,
        "is_promo": parse_bool(first(row, "is_promo", "promo", "promotion")),
        "source_name": first(row, "source_name", "source", default=source_name),
        "source_url": first(row, "source_url", "url_source"),
    }


def save_normalized_rows(rows: list[dict[str, Any]], source_name: str) -> dict[str, int]:
    saved_prices = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    with connect_db() as db:
        for i, raw in enumerate(rows, start=1):
            try:
                x = row_to_normalized(raw, source_name)
                stamp = utc_now()
                db.execute(
                    """INSERT INTO products(ean,name,brand,quantity,image_url,category,product_url,source_name,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(ean) DO UPDATE SET
                       name=excluded.name,
                       brand=CASE WHEN excluded.brand<>'' THEN excluded.brand ELSE products.brand END,
                       quantity=CASE WHEN excluded.quantity<>'' THEN excluded.quantity ELSE products.quantity END,
                       image_url=CASE WHEN excluded.image_url<>'' THEN excluded.image_url ELSE products.image_url END,
                       category=CASE WHEN excluded.category<>'' THEN excluded.category ELSE products.category END,
                       product_url=CASE WHEN excluded.product_url<>'' THEN excluded.product_url ELSE products.product_url END,
                       source_name=excluded.source_name, updated_at=excluded.updated_at""",
                    (x["ean"], x["name"], x["brand"], x["quantity"], x["image_url"], x["category"], x["product_url"], x["source_name"], stamp),
                )
                db.execute(
                    """INSERT INTO stores(store_id,retailer,store_name,city,postal_code,latitude,longitude,store_url,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(store_id) DO UPDATE SET
                       retailer=excluded.retailer,store_name=excluded.store_name,city=excluded.city,
                       postal_code=excluded.postal_code,
                       latitude=COALESCE(excluded.latitude,stores.latitude),
                       longitude=COALESCE(excluded.longitude,stores.longitude),
                       store_url=CASE WHEN excluded.store_url<>'' THEN excluded.store_url ELSE stores.store_url END,
                       updated_at=excluded.updated_at""",
                    (x["store_id"], x["retailer"], x["store_name"], x["city"], x["postal_code"], x["latitude"], x["longitude"], x["store_url"], stamp),
                )
                db.execute(
                    """INSERT OR IGNORE INTO prices(ean,store_id,price,currency,price_date,is_promo,source_name,source_url,imported_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (x["ean"], x["store_id"], x["price"], x["currency"], x["price_date"], x["is_promo"], x["source_name"], x["source_url"], stamp),
                )
                if db.execute("SELECT changes()").fetchone()[0] > 0:
                    saved_prices += 1
                else:
                    skipped += 1
            except Exception as exc:  # return line-level validation issues to the admin
                errors.append({"line": i + 1, "error": str(exc)})
    return {"rows_received": len(rows), "prices_added": saved_prices, "duplicates_skipped": skipped, "rows_rejected": len(errors), "errors": errors[:100]}


def admin_guard(x_admin_key: str | None) -> None:
    if not x_admin_key or x_admin_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Clé administrateur incorrecte ou absente.")


def store_brand_status(db: sqlite3.Connection) -> list[dict[str, Any]]:
    chains = ["Carrefour", "E.Leclerc", "Intermarché", "Auchan"]
    result = []
    for chain in chains:
        row = db.execute(
            """SELECT COUNT(DISTINCT p.ean) AS products, COUNT(p.id) AS prices, MAX(p.price_date) AS last_price
               FROM prices p JOIN stores s ON s.store_id=p.store_id WHERE norm(s.retailer)=norm(?)""",
            (chain,),
        ).fetchone()
        result.append({
            "retailer": chain,
            "products": int(row["products"] or 0),
            "price_records": int(row["prices"] or 0),
            "last_price_date": row["last_price"],
            "connected": int(row["prices"] or 0) > 0,
            "message": "Données de prix importées" if int(row["prices"] or 0) else "Aucun flux ou fichier de prix connecté",
        })
    return result


@app.get("/")
def root() -> dict[str, Any]:
    return {"app": "PRIXO API", "status": "ok", "docs": "/docs", "health": "/api/health"}


@app.get("/api/health")
def health() -> dict[str, Any]:
    with connect_db() as db:
        products = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        stores = db.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
        prices = db.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    return {"ok": True, "products": products, "stores": stores, "price_records": prices, "max_price_age_days": PRICE_MAX_AGE_DAYS}


@app.get("/api/retailers")
def retailers_status() -> dict[str, Any]:
    with connect_db() as db:
        return {"retailers": store_brand_status(db)}


@app.get("/api/products/search")
def search_products(q: str = Query(min_length=1, max_length=120), brand: str = "", limit: int = Query(default=48, ge=1, le=100)) -> dict[str, Any]:
    query = f"%{normalize_text(q)}%"
    params: list[Any] = [query] * 5
    sql = """SELECT p.ean,p.name,p.brand,p.quantity,p.image_url,p.category,p.product_url,p.source_name,
                     COUNT(DISTINCT s.retailer) AS retailer_count,
                     GROUP_CONCAT(DISTINCT s.retailer) AS retailers
              FROM products p LEFT JOIN prices pr ON pr.ean=p.ean LEFT JOIN stores s ON s.store_id=pr.store_id
              WHERE (norm(p.name) LIKE ? OR norm(p.brand) LIKE ? OR norm(p.ean) LIKE ? OR norm(p.quantity) LIKE ? OR norm(p.category) LIKE ?)"""
    if brand.strip():
        sql += " AND norm(p.brand) LIKE ?"
        params.append(f"%{normalize_text(brand)}%")
    sql += " GROUP BY p.ean ORDER BY retailer_count DESC, p.name COLLATE NOCASE ASC LIMIT ?"
    params.append(limit)
    with connect_db() as db:
        rows = db.execute(sql, (*params,)).fetchall()
    return {
        "source": "PRIXO — catalogues de distributeurs importés",
        "items": [{
            "ean": row["ean"], "code": row["ean"], "name": row["name"], "brand": row["brand"],
            "quantity": row["quantity"], "image_url": row["image_url"], "image": row["image_url"],
            "category": row["category"], "product_url": row["product_url"],
            "source_name": row["source_name"], "retailer_count": row["retailer_count"],
            "retailers": row["retailers"].split(",") if row["retailers"] else [],
        } for row in rows],
        "count": len(rows),
    }


@app.post("/api/compare")
def compare_basket(payload: CompareRequest) -> dict[str, Any]:
    now = date.today()
    cutoff = (now - timedelta(days=payload.max_age_days)).isoformat()
    requested = {x.ean: x.quantity for x in payload.items}
    eans = list(requested)
    placeholders = ",".join("?" for _ in eans)
    with connect_db() as db:
        product_info_rows = db.execute(
            f"SELECT ean,name,brand,quantity FROM products WHERE ean IN ({placeholders})", eans
        ).fetchall()
        product_info = {r["ean"]: dict(r) for r in product_info_rows}
        missing_products = [ean for ean in eans if ean not in product_info]
        if missing_products:
            raise HTTPException(status_code=422, detail={"message": "Un ou plusieurs EAN ne figurent pas dans le catalogue importé.", "missing_eans": missing_products})
        rows = db.execute(
            f"""SELECT p.ean, p.name AS product_name, p.brand, p.quantity AS product_quantity, p.image_url,
                       p.product_url, pr.price, pr.currency, pr.price_date, pr.is_promo, pr.source_name,
                       pr.source_url, s.store_id, s.retailer, s.store_name, s.city, s.postal_code,
                       s.latitude, s.longitude, s.store_url
                FROM prices pr JOIN products p ON p.ean=pr.ean JOIN stores s ON s.store_id=pr.store_id
                WHERE pr.ean IN ({placeholders}) AND pr.currency='EUR' AND pr.price_date>=?
                ORDER BY pr.price_date DESC, pr.imported_at DESC""",
            (*eans, cutoff),
        ).fetchall()
    # If the user asks for nearby stores, never label unknown-location branches as nearby.
    eligible_rows = []
    distances: dict[str, float | None] = {}
    for row in rows:
        sid = row["store_id"]
        if payload.latitude is not None and payload.longitude is not None:
            if row["latitude"] is None or row["longitude"] is None:
                continue
            dist = distance_km(payload.latitude, payload.longitude, row["latitude"], row["longitude"])
            if dist > payload.radius_km:
                continue
            distances[sid] = dist
        else:
            distances[sid] = None
        eligible_rows.append(dict(row))
    # The input is sorted newest-first; keep the most recent record for each exact EAN/store.
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in eligible_rows:
        key = (row["store_id"], row["ean"])
        if key not in latest:
            latest[key] = row
    offers_by_ean: dict[str, list[dict[str, Any]]] = {ean: [] for ean in eans}
    stores_map: dict[str, dict[str, Any]] = {}
    for (sid, ean), row in latest.items():
        offer = {
            "ean": ean, "product_name": row["product_name"], "brand": row["brand"],
            "quantity": row["product_quantity"], "price": row["price"], "currency": row["currency"],
            "price_date": row["price_date"], "is_promo": bool(row["is_promo"]),
            "retailer": row["retailer"], "store_id": sid, "store_name": row["store_name"],
            "city": row["city"], "postal_code": row["postal_code"],
            "distance_km": round(distances[sid], 1) if distances.get(sid) is not None else None,
            "product_url": row["product_url"], "store_url": row["store_url"],
            "source_name": row["source_name"], "source_url": row["source_url"],
        }
        offers_by_ean[ean].append(offer)
        if sid not in stores_map:
            stores_map[sid] = {
                "store_id": sid, "retailer": row["retailer"], "store_name": row["store_name"],
                "city": row["city"], "postal_code": row["postal_code"], "store_url": row["store_url"],
                "distance_km": round(distances[sid], 1) if distances.get(sid) is not None else None,
                "offers": {}, "latest_date": row["price_date"],
            }
        stores_map[sid]["offers"][ean] = offer
        if row["price_date"] > stores_map[sid]["latest_date"]:
            stores_map[sid]["latest_date"] = row["price_date"]
    item_results = []
    for ean in eans:
        offers = sorted(offers_by_ean[ean], key=lambda x: (x["price"], x["price_date"]))
        item_results.append({
            "ean": ean, "quantity": requested[ean],
            "product_name": offers[0]["product_name"] if offers else product_info[ean]["name"],
            "brand": offers[0]["brand"] if offers else product_info[ean]["brand"],
            "quantity": product_info[ean]["quantity"],
            "offers": offers[:10], "best_offer": offers[0] if offers else None,
        })
    stores_results = []
    for store in stores_map.values():
        coverage = len(store["offers"])
        complete = coverage == len(eans)
        total = round(sum(store["offers"][ean]["price"] * requested[ean] for ean in eans if ean in store["offers"]), 2)
        stores_results.append({
            **{k: v for k, v in store.items() if k != "offers"},
            "covered_count": coverage, "basket_count": len(eans), "complete": complete,
            "total": total if complete else None,
            "partial_total": total,
            "offers": [store["offers"][ean] for ean in eans if ean in store["offers"]],
        })
    stores_results.sort(key=lambda x: (not x["complete"], x["total"] if x["complete"] else 1e15, -x["covered_count"], x["distance_km"] if x["distance_km"] is not None else 1e15))
    complete_stores = [s for s in stores_results if s["complete"]]
    return {
        "currency": "EUR", "as_of": utc_now(), "max_age_days": payload.max_age_days,
        "location_filter": {"enabled": payload.latitude is not None and payload.longitude is not None, "radius_km": payload.radius_km if payload.latitude is not None else None},
        "retailers": store_brand_status_from_current_db(),
        "items": item_results,
        "stores": stores_results,
        "best_store": complete_stores[0] if complete_stores else None,
        "message": "Prix datés issus des fichiers ou flux importés. Un magasin incomplet n'a pas de total de panier comparable." if stores_results else "Aucun prix récent importé ne correspond à ce panier dans le périmètre sélectionné.",
    }


def store_brand_status_from_current_db() -> list[dict[str, Any]]:
    with connect_db() as db:
        return store_brand_status(db)


@app.post("/api/admin/import/csv")
async def import_csv(request: Request, x_admin_key: str | None = Header(default=None)) -> dict[str, Any]:
    admin_guard(x_admin_key)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Fichier CSV vide.")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = body.decode("latin-1")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="Encodage CSV non reconnu ; exporter en UTF-8.") from exc
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="Le CSV doit contenir une ligne d'en-têtes.")
    original_fields = reader.fieldnames
    def normalize_header(value: str) -> str:
        value = normalize_text(value).replace("€", "eur")
        return re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    rows = []
    for row in reader:
        rows.append({normalize_header(k): v for k, v in row.items() if k is not None})
    result = save_normalized_rows(rows, "import-csv")
    return {"ok": result["prices_added"] > 0 or result["duplicates_skipped"] > 0, **result, "fields_seen": original_fields}


@app.post("/api/admin/import/json")
async def import_json(request: Request, x_admin_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Importe les données normalisées provenant d'un flux/API distributeur autorisé."""
    admin_guard(x_admin_key)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Corps JSON invalide.") from exc
    if isinstance(payload, dict):
        rows = payload.get("items", payload.get("products", payload.get("results")))
    else:
        rows = payload
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail="Le JSON doit être une liste non vide ou un objet contenant items[].")
    if not all(isinstance(row, dict) for row in rows):
        raise HTTPException(status_code=400, detail="Chaque élément JSON doit être un objet produit/prix.")
    result = save_normalized_rows(rows, "import-json-feed")
    return {"ok": result["prices_added"] > 0 or result["duplicates_skipped"] > 0, **result}


@app.get("/api/admin/status")
def admin_status(x_admin_key: str | None = Header(default=None)) -> dict[str, Any]:
    admin_guard(x_admin_key)
    with connect_db() as db:
        counts = {
            "products": db.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "stores": db.execute("SELECT COUNT(*) FROM stores").fetchone()[0],
            "price_records": db.execute("SELECT COUNT(*) FROM prices").fetchone()[0],
            "last_price_date": db.execute("SELECT MAX(price_date) FROM prices").fetchone()[0],
        }
    return {"ok": True, "database": counts, "retailers": store_brand_status_from_current_db()}
