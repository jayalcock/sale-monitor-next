# Sale Monitor

I got tired of manually checking prices on things I was watching. Price tracking services exist, but they're limited to specific retailers, don't let you define your own alert rules, and you have no control over your data. I wanted something self-hosted that could monitor any product on any site, track history, and notify me across multiple channels when a deal hits.

What started as a simple script to scrape prices and send an email turned into a full application with a web dashboard, multi-currency support, product grouping for cross-retailer comparison, and purchase tracking to quantify actual savings.

## How It Works

The core loop is straightforward: a background process checks product pages on a schedule, extracts the price using either a user-defined CSS selector or auto-detection (60+ built-in selectors for major retailers), converts to a base currency, and stores the result in SQLite. When a price triggers an alert rule, notifications go out over email, Discord, or Slack.

The web dashboard is where most of the interaction happens. Products are displayed in a sortable table or card grid with live price data, trend indicators, and quick actions. Each product has a detail page with an interactive price history chart and statistics. Products selling the same item across different retailers are automatically grouped using identifiers extracted from the page (MPN, SKU, GTIN), so you can compare pricing across vendors at a glance.

## Alert Rules

There are five configurable alert types per product, which can be combined:

- **Target price** -- triggers when the price drops to or below a set amount
- **Discount threshold** -- triggers on a percentage drop from the last known price
- **Price drop** -- any decrease from the previous check
- **Below average** -- price falls under the 30-day rolling average
- **Any change** -- notifies on any price movement, up or down

Each product has its own notification cooldown to prevent spam, and can override the default notification channels.

## Features

- Dark-themed responsive dashboard with table and card views
- Interactive price history charts with selectable time ranges (7d to all-time)
- Auto-detection of prices on 60+ retailer platforms (Amazon, Best Buy, Shopify, WooCommerce, etc.)
- Multi-currency support with automatic conversion and cached exchange rates
- Product grouping and cross-retailer price comparison
- Purchase tracking with savings calculation
- Failure diagnostics page with per-product extraction health metrics
- Bulk CSV import/export
- Browser bookmarklet for identifying price selectors on any page
- Optional API key authentication and rate limiting

## Tech Stack

- **Backend:** Python, Flask, Gunicorn
- **Storage:** SQLite with WAL mode for concurrent reads/writes
- **Frontend:** Jinja2 templates, Tailwind CSS, Chart.js
- **Notifications:** SMTP (with retry), Discord webhooks, Slack webhooks
- **Deployment:** Docker with supervisord managing the web server and monitor process
- **Hosting:** Self-hosted on Unraid

The price extraction runs in a thread pool (4 workers) for parallel checks. SQLite's WAL mode lets the web dashboard serve reads while the background monitor writes new data without locking. Schema changes are handled through versioned migrations.

## What I Learned

Building the auto-detection was the most interesting challenge. Every retailer structures their pricing differently, and even sites on the same platform (Shopify, WooCommerce) have variations. The approach I settled on is a priority chain: try the user's custom selector first, then platform-specific selectors, then generic fallbacks. Currency detection follows a similar pattern -- JSON-LD first, meta tags, platform-specific data attributes, then fall back to configured defaults.

Product grouping was another area that required more thought than expected. Matching products across retailers by name is unreliable, but standardized identifiers (MPN, GTIN) extracted from structured data on the page work well. The system uses a three-tier matching strategy: explicit user-defined groups take priority, then manually assigned keys, then auto-detected identifiers.
