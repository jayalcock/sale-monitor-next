# Sale Monitor Next — Improvement Areas

## 1. Concurrency & File Locking
- `state.json` uses a basic spinlock (0.1s polling) — fragile when CLI + Flask compete for access
- Could upgrade to `fcntl` or `portalocker` for proper OS-level locking

## 2. Scalability
- No pagination on API history endpoints — could load the entire 22MB+ DB into memory
- Charts render all data points client-side — will lag with many products
- Product discovery searches 6+ retailers sequentially (blocking)

## 3. Price Extraction Brittleness
- 60+ hardcoded CSS selectors in `auto_detector.py` — break when sites redesign
- Amazon detection relies on response size heuristics and captcha string matching
- No retry after selector detection failure

## 4. Observability
- No structured logging (all plain text), inconsistent coverage
- No metrics, traces, or performance monitoring
- No monitoring/alerting guide

## 5. Security
- No authentication — anyone with network access can use the API
- No rate limiting on endpoints
- No CSRF protection on state-changing operations

## 6. Notification Reliability
- No retry logic for failed SMTP sends
- Plain text emails (could use HTML templates with links)

## 7. Testing Gaps
- Tests are mostly mock-heavy unit tests
- Missing edge cases: malformed CSV, corrupted DB, network timeouts
- No performance or load tests

## 8. State Management
- `state.json` grows unbounded with identifiers per product
- No schema migration path, no cleanup of stale data
