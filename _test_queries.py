import json, csv, re
from urllib.parse import urlparse

with open('data/state.json') as f:
    state = json.load(f)
with open('data/products.csv') as f:
    reader = csv.DictReader(f)
    products = list(reader)

for p in products:
    url = p['url']
    user_name = p['name']
    ids = state.get(url, {}).get('identifiers', {})
    brand = ids.get('brand', '')
    id_name = ids.get('name', '')
    merchant = urlparse(url).hostname.replace('www.', '') if url else ''
    merchant_clean = re.sub(r'[^a-z]', '', merchant.lower())
    id_name_clean = re.sub(r'[^a-z]', '', id_name.lower())
    name_is_store = bool(id_name and merchant_clean and id_name_clean and id_name_clean in merchant_clean)
    usable_id_name = id_name if (id_name and not name_is_store) else ''
    product_name = usable_id_name or user_name
    if brand and not product_name.lower().startswith(brand.lower()):
        query = brand + ' ' + product_name
    else:
        query = product_name
    print(f'{user_name:50s} -> "{query}"')
