#!/usr/bin/env python3
"""
Fix JensonUSA derailleur currency in database.

The historical records incorrectly have currency='CAD' when the product
is actually priced in USD. This script updates those records to have
the correct currency='USD' so the conversion to base currency works properly.
"""
import sqlite3
import sys
from pathlib import Path

def fix_currency(db_path: str, dry_run: bool = False):
    """Fix the currency field for JensonUSA derailleur records."""
    
    # The product URL
    url = "https://www.jensonusa.com/sram-x0-t-type-eagle-axs-12-spd-rear-derailleur"
    
    with sqlite3.connect(db_path) as conn:
        # First, show what we'll change
        cursor = conn.execute(
            """
            SELECT COUNT(*), MIN(timestamp), MAX(timestamp)
            FROM price_history
            WHERE product_url = ? AND currency = 'CAD'
            """,
            (url,)
        )
        count, min_ts, max_ts = cursor.fetchone()
        
        print(f"Found {count} records with currency='CAD' for JensonUSA derailleur")
        print(f"Date range: {min_ts} to {max_ts}")
        
        if count == 0:
            print("No records to fix.")
            return 0
        
        if dry_run:
            print("\nDRY RUN - no changes made")
            return count
        
        # Update the records
        cursor = conn.execute(
            """
            UPDATE price_history
            SET currency = 'USD'
            WHERE product_url = ? AND currency = 'CAD'
            """,
            (url,)
        )
        
        updated = cursor.rowcount
        conn.commit()
        
        print(f"\n✅ Updated {updated} records to currency='USD'")
        return updated


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python fix_jensonusa_currency.py <path_to_history.db> [--dry-run]")
        sys.exit(1)
    
    db_path = sys.argv[1]
    dry_run = '--dry-run' in sys.argv
    
    if not Path(db_path).exists():
        print(f"Error: Database file not found: {db_path}")
        sys.exit(1)
    
    fix_currency(db_path, dry_run)
