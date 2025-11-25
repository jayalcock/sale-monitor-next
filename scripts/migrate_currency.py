#!/usr/bin/env python3
"""
One-time migration script to add currency information to existing price_history records.

This script:
1. Detects currency from product URLs using the same heuristics as runtime
   - Known USD domains (e.g., jensonusa.com) -> USD
   - .ca TLD -> CAD
   - No generic ".com -> USD" fallback (avoids mislabeling CAD-priced .com stores)
2. Updates all existing records in price_history with currency field
3. Backfills price_cad for existing records (CAD records: price_cad = price, USD records: set to NULL)

Run this once after upgrading to the multi-currency version.

Usage:
    PYTHONPATH=src python scripts/migrate_currency.py --db data/history.db
"""
import argparse
import sqlite3
import sys
from pathlib import Path


def detect_currency_from_url(url: str) -> str:
    """Detect currency based on URL host.

    Rules aligned with PriceExtractor._guess_currency_from_url:
    - Known USD-only retailers -> USD
    - .ca TLD -> CAD
    - Otherwise, remain undecided and default to CAD for migration safety
    """
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""

    usd_hosts = (
        'jensonusa.com', 'jensenusa.com', 'amazon.com', 'competitivecyclist.com',
        'backcountry.com', 'rei.com', 'trekbikes.com', 'specialized.com',
    )
    if any(host.endswith(h) for h in usd_hosts):
        return 'USD'

    if host.endswith('.ca'):
        return 'CAD'

    # Default to CAD if uncertain (conservative for migration)
    return 'CAD'


def migrate_currency_data(db_path: str, dry_run: bool = False):
    """Migrate existing price_history records to include currency."""
    
    if not Path(db_path).exists():
        print(f"Error: Database not found at {db_path}")
        return False
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check if currency column exists
    cursor.execute("PRAGMA table_info(price_history)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if 'currency' not in columns:
        print("Error: Database schema not updated. Run the app once to migrate schema first.")
        conn.close()
        return False
    
    # Get all unique product URLs
    cursor.execute("""
        SELECT DISTINCT product_url, product_name
        FROM price_history
        WHERE currency IS NULL OR currency = ''
    """)
    
    products = cursor.fetchall()
    
    if not products:
        print("✓ No records need migration (all records already have currency set)")
        conn.close()
        return True
    
    print(f"Found {len(products)} products with records needing migration:")
    print()
    
    # Group by detected currency
    updates = []
    for url, name in products:
        detected_currency = detect_currency_from_url(url)
        
        # Count records for this product
        cursor.execute("""
            SELECT COUNT(*) FROM price_history 
            WHERE product_url = ? AND (currency IS NULL OR currency = '')
        """, (url,))
        count = cursor.fetchone()[0]
        
        print(f"  {name}")
        print(f"    URL: {url}")
        print(f"    Currency: {detected_currency}")
        print(f"    Records: {count}")
        print()
        
        updates.append((url, detected_currency, count))
    
    if dry_run:
        print("DRY RUN: No changes made to database")
        total_records = sum(count for _, _, count in updates)
        print(f"Would update {total_records} records across {len(updates)} products")
        conn.close()
        return True
    
    # Apply updates
    print("Applying updates...")
    total_updated = 0
    
    for url, currency, _ in updates:
        # Update currency field
        cursor.execute("""
            UPDATE price_history
            SET currency = ?
            WHERE product_url = ? AND (currency IS NULL OR currency = '')
        """, (currency, url))
        
        # For CAD records, set price_cad = price
        if currency == 'CAD':
            cursor.execute("""
                UPDATE price_history
                SET price_cad = price
                WHERE product_url = ? AND price_cad IS NULL
            """, (url,))
        
        updated = cursor.rowcount
        total_updated += updated
        print(f"  Updated {updated} records for {url}")
    
    conn.commit()
    conn.close()
    
    print()
    print(f"✓ Migration complete! Updated {total_updated} records")
    print()
    print("Notes:")
    print("  - CAD records: price_cad set to original price")
    print("  - USD records: price_cad left NULL (will be converted on next price check)")
    
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Migrate price_history database to include currency information"
    )
    parser.add_argument(
        '--db',
        default='data/history.db',
        help='Path to history database (default: data/history.db)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be changed without modifying database'
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("Currency Migration Script")
    print("=" * 60)
    print()
    
    success = migrate_currency_data(args.db, dry_run=args.dry_run)
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
