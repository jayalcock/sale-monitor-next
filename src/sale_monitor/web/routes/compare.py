"""Competitive comparison endpoints."""
import hashlib
import logging
import os
import sqlite3

from flask import Blueprint, jsonify, request

from sale_monitor.storage.config_store import get_base_currency
from sale_monitor.storage.json_state import mutate_state
from sale_monitor.web.auth import require_api_key, require_api_key_for_reads
from sale_monitor.web.comparison import (
    build_comparison_groups,
    normalize_name,
    numeric_tokens,
    similar,
)
from sale_monitor.web.extensions import rate_limit
from sale_monitor.web.helpers import (
    config_file,
    get_check_service,
    get_product_store,
    get_state_cache,
    paginate,
    safe_error,
    state_file,
)

logger = logging.getLogger(__name__)

bp = Blueprint('compare', __name__)


@bp.route('/api/compare/groups')
@require_api_key_for_reads
def api_compare_groups():
    """Return competitive groups built from identifiers in state."""
    try:
        products = get_product_store().get_all()
        state = get_state_cache().get()
        base_currency = get_base_currency(config_file()).upper()
        try:
            groups = build_comparison_groups(state or {}, products or [], base_currency)
            return jsonify(paginate(groups))
        except (TypeError, ValueError, KeyError) as e:
            logger.error("Comparison group build failed: %s", e)
            return jsonify(paginate([]))
    except (OSError, ValueError, sqlite3.Error) as e:
        return safe_error(e)


@bp.route('/api/compare/backfill-identifiers', methods=['POST'])
@require_api_key
@rate_limit("2 per minute")
def api_compare_backfill_identifiers():
    """Backfill product identifiers in state by scraping pages.

    Returns {updated: n, failed: m} and does not modify prices.
    """
    try:
        products = get_product_store().get_all()
        service = get_check_service()
        collected = {}
        failed = 0
        for p in products:
            try:
                idents = service.new_extractor().extract_identifiers(p.url)
                if idents:
                    collected[p.url] = idents
                else:
                    failed += 1
            except Exception:  # noqa: BLE001 - continue with other products
                failed += 1

        def _apply(state):
            for url, idents in collected.items():
                prev = state.get(url)
                if not isinstance(prev, dict):
                    prev = {}
                prev['identifiers'] = idents
                state[url] = prev

        mutate_state(state_file(), _apply)
        return jsonify({'success': True, 'updated': len(collected), 'failed': failed})
    except (OSError, ValueError) as e:
        return safe_error(e)


@bp.route('/api/compare/suggest')
@require_api_key_for_reads
def api_compare_suggest():
    """Suggest likely product matches based on fuzzy name matching.

    Two types of suggestions:
    1. Ungrouped↔ungrouped: two solo products that look similar
    2. Ungrouped→group: a solo product that matches an existing group

    Returns list of suggestions: [{nameA, urlA, nameB, urlB, similarity, group_key?}]
    When group_key is present, urlB is already in that group and linking
    will merge urlA into it.
    """
    try:
        products = get_product_store().get_all()
        state = get_state_cache().get()
        base_currency = get_base_currency(config_file()).upper()

        groups = build_comparison_groups(state or {}, products or [], base_currency)
        grouped_urls = set()
        # Map grouped URL → (group_key, representative name)
        url_to_group_info = {}
        for g in groups:
            rep_name = g['items'][0]['name'] if g.get('items') else g['group_key']
            for it in g.get('items', []):
                grouped_urls.add(it.get('url'))
                url_to_group_info[it['url']] = (g['group_key'], rep_name)

        ungrouped = []
        name_by_url = {}
        for p in products:
            name_by_url[p.url] = p.name
            if p.url not in grouped_urls:
                ungrouped.append({
                    'url': p.url, 'name': p.name,
                    'norm': normalize_name(p.name),
                    'nums': numeric_tokens(p.name),
                })

        suggestions = []
        threshold = float(os.getenv('COMPARE_NAME_SIMILARITY', '0.88'))
        seen_pairs = set()

        # 1) Ungrouped ↔ ungrouped
        for i in range(len(ungrouped)):
            for j in range(i + 1, len(ungrouped)):
                a, b = ungrouped[i], ungrouped[j]
                # Different model/size/version numbers → different SKUs,
                # regardless of how similar the names read.
                if a['nums'] != b['nums']:
                    continue
                sim = similar(a['norm'], b['norm'])
                if sim >= threshold:
                    suggestions.append({
                        'nameA': a['name'], 'urlA': a['url'],
                        'nameB': b['name'], 'urlB': b['url'],
                        'similarity': round(sim, 3)
                    })
                    seen_pairs.add((a['url'], b['url']))

        # 2) Ungrouped → existing group (compare against first member of each group)
        group_reps = []
        for g in groups:
            if g.get('items'):
                first = g['items'][0]
                group_reps.append({
                    'url': first['url'],
                    'name': first.get('name', ''),
                    'norm': normalize_name(first.get('name', '')),
                    'nums': numeric_tokens(first.get('name', '')),
                    'group_key': g['group_key'],
                })

        for item in ungrouped:
            for rep in group_reps:
                pair = tuple(sorted([item['url'], rep['url']]))
                if pair in seen_pairs:
                    continue
                if item['nums'] != rep['nums']:
                    continue
                sim = similar(item['norm'], rep['norm'])
                if sim >= threshold:
                    suggestions.append({
                        'nameA': item['name'], 'urlA': item['url'],
                        'nameB': rep['name'], 'urlB': rep['url'],
                        'similarity': round(sim, 3),
                        'group_key': rep['group_key'],
                    })
                    seen_pairs.add(pair)

        suggestions.sort(key=lambda s: s['similarity'], reverse=True)
        return jsonify(paginate(suggestions))
    except (OSError, ValueError) as e:
        return safe_error(e)


@bp.route('/api/compare/link', methods=['POST'])
@require_api_key
def api_compare_link():
    """Link two product URLs under a shared manual group_key.

    Body: { urlA, urlB, group_key? }

    If either product already belongs to a manual group, the other product
    (and all members of its old group, if any) are merged into that group.
    This keeps groups transitive: linking A↔B then B↔C puts all three
    under the same key.
    """
    try:
        data = request.get_json() or {}
        urlA = (data.get('urlA') or '').strip()
        urlB = (data.get('urlB') or '').strip()
        explicit_key = (data.get('group_key') or '').strip()
        if not urlA or not urlB:
            return jsonify({'error': 'urlA and urlB required'}), 400

        chosen = {}

        def _apply(state):
            a = state.get(urlA, {})
            b = state.get(urlB, {})
            key_a = a.get('group_key') or ''
            key_b = b.get('group_key') or ''

            # Decide the canonical group_key: explicit > existing > new hash
            if explicit_key:
                group_key = explicit_key
            elif key_a:
                group_key = key_a
            elif key_b:
                group_key = key_b
            else:
                pair = '|'.join(sorted([urlA, urlB]))
                group_key = 'manual:' + hashlib.sha1(pair.encode('utf-8')).hexdigest()[:8]

            # Merge: any product that had the "losing" key gets the winner
            old_keys = {k for k in (key_a, key_b) if k and k != group_key}
            for url_key, st in state.items():
                if isinstance(st, dict) and st.get('group_key') in old_keys:
                    st['group_key'] = group_key

            a['group_key'] = group_key
            b['group_key'] = group_key
            state[urlA] = a
            state[urlB] = b
            chosen['group_key'] = group_key

        mutate_state(state_file(), _apply)
        return jsonify({'success': True, 'group_key': chosen['group_key']})
    except (OSError, ValueError) as e:
        return safe_error(e)


@bp.route('/api/compare/unlink', methods=['POST'])
@require_api_key
def api_compare_unlink():
    """Remove a product from its comparison group.

    Body: { url }
    """
    try:
        data = request.get_json() or {}
        url = (data.get('url') or '').strip()
        if not url:
            return jsonify({'error': 'url required'}), 400

        removed = {}

        def _apply(state):
            st = state.get(url)
            if not isinstance(st, dict) or not st.get('group_key'):
                return
            removed['from'] = st.pop('group_key', None)
            state[url] = st

        mutate_state(state_file(), _apply)
        if 'from' not in removed:
            return jsonify({'error': 'Product is not in a group'}), 400
        return jsonify({'success': True, 'removed_from': removed['from']})
    except (OSError, ValueError) as e:
        return safe_error(e)
