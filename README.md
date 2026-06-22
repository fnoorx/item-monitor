# Item Monitor

A portfolio-safe Python automation project for monitoring an authenticated web listing. The script signs in with Selenium, walks a paginated listing, extracts stable entry data, saves a JSON snapshot, and writes a separate report for entries that were not present in the previous run.

The public repository keeps target URLs, credentials, and site-specific selectors out of source control. Runtime details live in local environment variables and an ignored `site_config.json` file.

## What It Demonstrates

- Authenticated browser automation with Selenium and explicit waits.
- Resilient handling for dynamic pages, lazy-loaded content, overlays, and pagination.
- Local snapshot persistence with repeat-run change detection.
- Config-driven selectors so target-specific details stay out of public code.
- Graceful interruption support that preserves partial scrape results.

## Workflow

1. Load local runtime settings from environment variables or `.env`.
2. Load selector configuration from `site_config.json`.
3. Open the target entry point and complete the sign-in flow.
4. Navigate to the listing URL and collect entries across pages.
5. Compare the current snapshot with the previous snapshot.
6. Save `data/listing_snapshot.json` and `data/new_items.json`.

## Demo

![Item Monitor demo](demo/item-monitor-demo.gif)

## Project Structure

- `item_monitor.py` - main automation, extraction, snapshot, and comparison workflow.
- `site_config.example.json` - public example of the selector configuration shape.
- `requirements.txt` - external Python packages required to run the script.
- `demo/` - portfolio demo media location.
- `data/` - ignored local output directory created at runtime.

## Setup

Create and activate a virtual environment, then install dependencies:

```powershell
pip install -r requirements.txt
```

Copy the example selector config and fill in local values:

```powershell
Copy-Item site_config.example.json site_config.json
```

Provide runtime settings through environment variables or an ignored `.env` file:

```powershell
$env:PORTFOLIO_TARGET_BASE_URL="<base URL>"
$env:PORTFOLIO_TARGET_LISTING_URL="<listing URL>"
$env:PORTFOLIO_SCRAPER_USERNAME="<username>"
$env:PORTFOLIO_SCRAPER_PASSWORD="<password>"
```

Run the monitor:

```powershell
python item_monitor.py
```

## Outputs

- `data/listing_snapshot.json` - latest complete snapshot keyed by normalized link.
- `data/new_items.json` - entries found in the latest run that were absent from the previous snapshot.

## Publishing Notes

The following files and folders should stay local:

- `.env`
- `site_config.json`
- `data/`
- logs, screenshots, browser profiles, and packaged binaries

Before publishing, run `git status --ignored --short` and confirm only portfolio-safe source files are tracked.
