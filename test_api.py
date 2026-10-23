"""Tests de fumée locaux ; crée une base temporaire, sans toucher à la base de l'application."""
import os
import tempfile
import unittest
from pathlib import Path

TEMP_DB = Path(tempfile.gettempdir()) / f"prixo-test-{os.getpid()}.sqlite3"
os.environ["PRIXO_DATABASE_PATH"] = str(TEMP_DB)
os.environ["PRIXO_ADMIN_API_KEY"] = "test-key-only"

from fastapi.testclient import TestClient  # noqa: E402
import server  # noqa: E402

client = TestClient(server.app)
CSV = """ean,product_name,brand,quantity,retailer,store_name,store_id,city,latitude,longitude,price,currency,price_date,source_name
5449000000996,Boisson test,MarqueTest,1 L,Carrefour,Carrefour Test,CAR-TEST,Montpellier,43.6108,3.8767,1.25,EUR,2026-10-22,TEST
5449000000996,Boisson test,MarqueTest,1 L,E.Leclerc,Leclerc Test,LEC-TEST,Montpellier,43.6110,3.8800,1.19,EUR,2026-10-22,TEST
3017620422003,Pâte test,MarqueDeux,400 g,Carrefour,Carrefour Test,CAR-TEST,Montpellier,43.6108,3.8767,3.50,EUR,2026-10-22,TEST
3017620422003,Pâte test,MarqueDeux,400 g,E.Leclerc,Leclerc Test,LEC-TEST,Montpellier,43.6110,3.8800,3.20,EUR,2026-10-22,TEST
"""

class PrioxSmokeTests(unittest.TestCase):
    def test_health_empty(self):
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_import_search_compare(self):
        response = client.post("/api/admin/import/csv", headers={"X-Admin-Key": "test-key-only", "Content-Type": "text/csv"}, content=CSV)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["prices_added"], 4)
        found = client.get("/api/products/search", params={"q": "MarqueTest"})
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["count"], 1)
        comparison = client.post("/api/compare", json={"items": [{"ean": "5449000000996", "quantity": 2}, {"ean": "3017620422003", "quantity": 1}], "latitude": 43.61, "longitude": 3.88, "radius_km": 25})
        self.assertEqual(comparison.status_code, 200, comparison.text)
        data = comparison.json()
        self.assertEqual(data["best_store"]["retailer"], "E.Leclerc")
        self.assertEqual(data["best_store"]["total"], 5.58)
        self.assertTrue(all(store["complete"] for store in data["stores"]))

    def test_admin_authentication(self):
        response = client.get("/api/admin/status")
        self.assertEqual(response.status_code, 401)

if __name__ == "__main__":
    unittest.main(verbosity=2)
