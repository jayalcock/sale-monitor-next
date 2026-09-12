"""Competitive comparison-group helpers shared by several blueprints."""
import re
from difflib import SequenceMatcher


def build_comparison_groups(state: dict, products: list, base_currency: str) -> list:
    """Group products by shared identifiers or explicit CSV group.

    Priority: CSV group > manual group_key in state > auto-detected identifiers (mpn/sku/gtin).
    Returns a list of groups: [{group_key, source, identifiers, items: [{name,url,current_price,price_in_base,currency,last_checked}]}]
    """
    # Map url->product for names and CSV group
    name_by_url = {p.url: p.name for p in products}
    csv_group_by_url = {p.url: p.group for p in products if p.group}

    # Helper to compute price_in_base from state when available
    def _item_row(u: str, st: dict) -> dict:
        cur_price = st.get('current_price')
        cur_currency = (st.get('currency') or 'CAD').upper()
        price_in_base = st.get('price_in_base')
        return {
            'name': name_by_url.get(u, u),
            'url': u,
            'current_price': cur_price,
            'currency': cur_currency,
            'price_in_base': price_in_base if isinstance(price_in_base, (int, float)) else None,
            'last_checked': st.get('last_checked'),
        }

    groups: dict = {}
    for url, st in state.items():
        if not isinstance(st, dict):
            continue

        # 1) Explicit CSV group (strongest)
        csv_group = csv_group_by_url.get(url)
        # 2) Manual group_key in state
        manual_key = st.get('group_key')
        # 3) Auto-detected identifiers
        ident = st.get('identifiers') or {}
        auto_key = ident.get('mpn') or ident.get('sku') or ident.get('gtin') or ident.get('gtin13')

        key = csv_group or manual_key or auto_key
        if not key:
            continue

        source = 'csv' if csv_group else ('manual' if manual_key else 'auto')
        g = groups.setdefault(key, {'group_key': key, 'source': source, 'identifiers': ident, 'items': []})
        g['items'].append(_item_row(url, st))

    # Only keep groups with at least 2 items (competitive set)
    result = [g for g in groups.values() if len(g['items']) >= 2]
    # Sort items by price_in_base when available
    for g in result:
        try:
            g['items'].sort(key=lambda x: (float(x['price_in_base']) if isinstance(x.get('price_in_base'), (int, float, str)) and str(x.get('price_in_base')).strip() != '' else float('inf')))
        except (TypeError, ValueError):
            pass
    return result


def normalize_name(name: str) -> str:
    """Normalize product names for fuzzy comparison.

    Lowercase, remove punctuation, common tokens (brand noise), and collapse spaces.
    """
    if not isinstance(name, str):
        return ''
    n = name.lower()
    # Remove punctuation
    n = re.sub(r"[\-_/\\.,'\"]", " ", n)
    # Remove common noise tokens
    stop = {
        'inc', 'llc', 'ltd', 'co', 'company', 'shop', 'store', 'official',
        'bike', 'cycles', 'bikes', 'usa', 'canada', 'u.s.', 'ca',
    }
    tokens = [t for t in re.split(r"\s+", n) if t and t not in stop]
    return ' '.join(tokens).strip()


def similar(a: str, b: str) -> float:
    """Return similarity ratio between two strings using SequenceMatcher."""
    return SequenceMatcher(None, a, b).ratio()
