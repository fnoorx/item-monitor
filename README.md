# Item Monitor

Automated monitoring tool for an authenticated item listing. It logs into a protected site, scrapes the full catalog with Selenium, stores a dated CSV snapshot, and generates a second CSV containing only newly detected items.

This repository is intentionally portfolio-safe. Target URLs, selectors, and credentials are kept out of source control so the project demonstrates the engineering work without exposing the original data source.

## Why this project matters

This is a practical automation project that shows:

- browser automation against a real authenticated workflow
- resilient scraping across dynamic page states, lazy loading, and pagination
- daily snapshot generation and diff-based change detection
- separation of public code from private runtime configuration
- packaging readiness for local distribution with PyInstaller

## Recruiter Summary

**Problem solved:** manually checking a private listing for new items was incredibly repetitive, time consuming, and easy to miss.

**Approach:** automate login, navigate the listing, normalize and export scraped records, then compare the latest snapshot against a recent prior run.

**Result:** the script produces a complete daily archive and a focused `new_items_YYYY-MM-DD.csv` report for anything newly listed.

## Technical Highlights

- **Authenticated Selenium flow** using explicit waits and multi-selector fallback logic for login fields.
- **Dynamic page handling** that waits for listing content to stabilize before scraping lazy-loaded results.
- **Pagination resilience** with overlay dismissal and guarded next-page clicking to reduce brittle failures.
- **Structured extraction pipeline** that derives item identifiers from image URLs and falls back when markup is inconsistent.
- **Operational safety** through environment-based credential loading and local-only site configuration.
- **Change detection** using Pandas to compare current and prior snapshots and isolate net-new items.
- **Interrupt handling** so a user can stop a run cleanly with `Ctrl+C`.

## Stack

- Python
- Selenium
- Pandas
- Chrome WebDriver
- PyInstaller

## Project Structure

- `item_monitor.py` - main automation and comparison workflow
- `site_config.example.json` - example target configuration schema
- `item_monitor.spec` - PyInstaller build configuration
- `csv/` - local output folder for generated snapshots

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Copy `site_config.example.json` to `site_config.json`.
4. Add your private target URL and selector values to `site_config.json`.
5. Provide credentials through environment variables:

```powershell
$env:PORTFOLIO_SCRAPER_USERNAME="your_username"
$env:PORTFOLIO_SCRAPER_PASSWORD="your_password"
```

## Run

```powershell
python item_monitor.py
```

Generated outputs:

- `csv/all_items_YYYY-MM-DD.csv`
- `csv/new_items_YYYY-MM-DD.csv` when new items are found

## Privacy and Publishing Notes

Keep these out of Git:

- `site_config.json`
- `csv/`
- `build/`
- `dist/`
- `*.exe`

Before publishing, review `git status` and confirm no target-specific data, generated outputs, or packaged binaries are tracked.
