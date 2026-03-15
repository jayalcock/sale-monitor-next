# Sale Monitor Next — Improvement Areas

## 1. Concurrency & File Locking  ✅ Resolved
- ~~`state.json` uses a basic spinlock (0.1s polling)~~
- Now uses OS-level `fcntl.flock()` via `FileLock` class in `storage/file_lock.py`

## 2. Scalability
- ~~No pagination on API history endpoints~~ ✅ Pagination added
- Charts render all data points client-side — will lag with many products
- Product discovery searches 6+ retailers sequentially (blocking)

## 3. Price Extraction Brittleness
- 60+ hardcoded CSS selectors in `auto_detector.py` — break when sites redesign
- Amazon detection relies on response size heuristics and captcha string matching
- No retry after selector detection failure

## 4. Observability  ✅ Resolved
- ~~No structured logging~~ Now supports JSON + text auto-detection via `logging_config.py` + `python-json-logger`
- ~~No metrics~~ Health endpoint added at `/api/health` and `/api/health/detailed`
- No monitoring/alerting guide

## 5. Security  ✅ Resolved
- ~~No authentication~~ Optional API key auth via `API_KEY` env var
- ~~No rate limiting~~ Flask-Limiter with 60/min default + custom per-route limits
- ~~No CSRF protection~~ JSON Content-Type enforcement on state-changing `/api/*` endpoints

## 6. Notification Reliability  ✅ Resolved
- ~~No retry logic~~ 3 attempts with exponential backoff in `NotificationManager`
- ~~Plain text emails~~ HTML + plain text multipart emails with product details

## 7. Testing Gaps
- Tests are mostly mock-heavy unit tests
- Missing edge cases: malformed CSV, corrupted DB, network timeouts
- No performance or load tests

## 8. State Management  ✅ Partially Resolved
- ~~Unbounded identifiers~~ `prune_stale_entries()` caps identifiers to 50 per product
- ~~No schema migration path~~ `storage/migrations.py` provides versioned SQLite migrations
- ~~No cleanup of stale data~~ Stale state entries pruned on CLI startup

## 9. Dashboard UX  ✅ Resolved
- Sparkline trend charts in product table/cards (7-day history via Chart.js)
- System health widget showing status, product counts, alerts, exchange rate freshness
- Tag-based filtering dropdown alongside status/sort filters
- Tag pills rendered on product rows and cards

## 10. Notification Center  ✅ Resolved
- Full settings page (`/settings`) for SMTP config, Discord/Slack webhooks
- Per-product notification channels (smtp, discord, slack) in add/edit modals
- `WebhookNotifier` service with Discord embed + Slack text formatting
- Config-based notification storage in `config.json`
- Test button for each notification channel

## 11. Alert Rules  ✅ Resolved
- Per-product alert rules: target, discount, any_change, price_drop, below_avg
- Configurable via checkboxes in add/edit product modals
- CLI evaluates product-specific rules (defaults to target+discount for backward compat)

## 12. Product Tags  ✅ Resolved
- Tags stored per-product in CSV (comma-separated)
- Tags filter on dashboard, tag pills in table/cards
- Editable in add/edit product modals

## 13. Bulk Import  ✅ Resolved
- Bulk Import modal on manage page with paste and CSV upload tabs
- Preview table before import, per-row validation
- `/api/products/bulk-import` endpoint (max 100 per batch, duplicate detection)

## 14. Bookmarklet  ✅ Resolved
- Rewritten with 3-step install/use/test flow
- Browser-specific notes in collapsible details
- "Test on This Page" button for in-app verification
