"""SQLite-backed purchase tracking for savings calculation."""
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional


class PurchaseStore:
    """CRUD access to the purchases table in the shared history database."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def record_purchase(
        self,
        product_url: str,
        product_name: str,
        purchase_price: float,
        currency: str = "CAD",
        purchase_price_base: Optional[float] = None,
        reference_price: Optional[float] = None,
        reference_price_base: Optional[float] = None,
        notes: str = "",
    ) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        savings_base = None
        if reference_price_base is not None and purchase_price_base is not None:
            savings_base = round(reference_price_base - purchase_price_base, 2)

        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO purchases
                   (product_url, product_name, purchase_price, currency,
                    purchase_price_base, reference_price, reference_price_base,
                    savings_base, purchased_at, created_at, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    product_url, product_name, purchase_price, currency,
                    purchase_price_base, reference_price, reference_price_base,
                    savings_base, now, now, notes,
                ),
            )
            conn.commit()
            return {
                "id": cur.lastrowid,
                "savings_base": savings_base,
            }

    def get_purchases(self, product_url: Optional[str] = None) -> List[dict]:
        with self._connect() as conn:
            if product_url:
                rows = conn.execute(
                    "SELECT * FROM purchases WHERE product_url = ? ORDER BY purchased_at DESC",
                    (product_url,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM purchases ORDER BY purchased_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_total_savings(self) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(savings_base), 0), COUNT(*) FROM purchases"
            ).fetchone()
        return {
            "total_savings": round(row[0], 2),
            "purchase_count": row[1],
        }

    def delete_purchase(self, purchase_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM purchases WHERE id = ?", (purchase_id,))
            conn.commit()
        return cur.rowcount > 0
