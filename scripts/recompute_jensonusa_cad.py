#!/usr/bin/env python3
"""
Recompute price_cad for JensonUSA derailleur records.

After fixing currency from CAD to USD, we need to recompute the price_cad
field to have the correct USD->CAD conversion (~1.4x exchange rate).
"""
import sqlite3
import sys
from pathlib import Path

def recompute_cad(db_path: str, dry_run: bool = False):
    """Recompute price_cad for JensonUSA derailleur records."""
    
    # The product URL
    url = "https://www.jensonusa.com/sram-x0-t-type-eagle-axs-12-spd-rear-derailleur"
    
    # Typical USD to CAD exchange rate (can be adjusted)
    USD_TO_CAD = 1.40
    
    with sqlite3.connect(db_path) as conn:
        # First, show what we'll change
        cursor = conn.execute(
            """
            SELECT COUNT(*), price, price_cad
            FROM price_history
            WHERE product_url = ? AND currency = 'USD'
            LIMIT 1
            """,
            (url,)
        )
        count_row = cursor.fetchone()
        
        cursor = conn.execute(
            """
            SELECT COUNT(*)
            FROM price_history
            WHERE product_url = ? AND currency = 'USD'
            """,
            (url,)
        )
        count = cursor.fetchone()[0]
        
        print(f"Found {count} records with currency='USD' for JensonUSA derailleur")
        if count_row and count_row[1]:
            price_usd = count_row[1]
            old_cad = count_row[2] or 0
            new_cad = round(price_usd * USD_TO_CAD, 2)
            print(f"Example: USD ${price_usd} -> CAD ${new_cad} (currently {old_cad})")
        
        if count == 0:
            print("No records to fix.")
            return 0
        
        if dry_run:
            print("\nDRY RUN - no changes made")
            return count
        
        # Update the records - use a typical USD->CAD conversion rate
        cursor = conn.execute(
            f"""
            UPDATE price_history
            SET price_cad = ROUND(price * {USD_TO_CAD}, 2)
            WHERE product_url = ? AND currency = 'USD'
            """,
            (url,)
        )
        
        updated = cursor.rowcount
        conn.commit()
        
        print(f"\n✅ Updated {updated} records with recomputed price_cad")
        return updated


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python recompute_jensonusa_cad.py <path_to_history.db> [--dry-run]")
        sys.exit(1)
    
    db_path = sys.argv[1]
    dry_run = '--dry-run' in sys.argv
    
    if not Path(db_path).exists():
        print(f"Error: Database file not found: {db_path}")
        sys.exit(1)
    
    recompute_cad(db_path, dry_run)
