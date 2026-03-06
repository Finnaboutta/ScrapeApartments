# Apartment Scraper (Viewit + Kijiji)

This script scrapes apartment listings from `viewit.ca` and `kijiji.ca`, keeps listings with `1+` bedrooms, and stores unique results in SQLite so duplicate listings are skipped on future runs.

## Fields captured
- Address
- Number of bedrooms
- Price
- Link to the listing

## Filters applied
- Bedrooms: `>= 1`
- Price: `<= $3500` (configurable)
- Basement units: excluded (e.g. `basement apartment`, `bsmt`, `lower level`)
- Location: starts at `2 km` from Bloor & Bathurst and expands outward until matches are found (up to a max radius)

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run
```bash
python scraper.py
```

The script always opens an interactive menu (type `1-5`) and then asks sub-options.

## Interactive menu options
- `1` Run full scrape (Viewit + Kijiji)
- `2` Run Viewit only
- `3` Run Kijiji only
- `4` Show DB entries
- `5` Reset DB
- `6` Sync unsent DB entries to Trello

Each run mode asks follow-up prompts (for example DB path, headless mode, and page counts).

## Trello sync
Set these environment variables before using menu option `6`:

```bash
export TRELLO_KEY="your_trello_key"
export TRELLO_TOKEN="your_trello_token"
export TRELLO_LIST_ID="your_list_id"
```

Optional fallback (if `TRELLO_LIST_ID` is not provided):
```bash
export TRELLO_BOARD_ID="your_board_id"
```

When synced, each listing row is marked in DB with:
- `sent_to_trello = 1`
- `trello_card_id`
- `sent_to_trello_at`

## Useful options
```bash
python scraper.py --all
python scraper.py --db-path data/listings.db
python scraper.py --kijiji-url "<custom-search-url>" --viewit-url "<custom-search-url>"
python scraper.py --max-price 3200 --start-radius-km 2 --max-radius-km 12 --radius-step-km 1
python scraper.py --center-lat 43.66564 --center-lon -79.41110
python scraper.py --kijiji-pages 3 --kijiji-delay-min 20 --kijiji-delay-max 30
python scraper.py --kijiji-location-query "Bloor Bathurst" --kijiji-location-options 6 --kijiji-radius-km 2
python scraper.py --headless
python scraper.py --http-only
python scraper.py --viewit-only
python scraper.py --kijiji-only
python scraper.py --viewit-max-price 3300 --viewit-pages 3
python scraper.py --viewit-bedroom-delay-min 1 --viewit-bedroom-delay-max 2
python scraper.py --viewit-before-list-click-delay-min 1 --viewit-before-list-click-delay-max 5
python scraper.py --viewit-page-wait-min 5 --viewit-page-wait-max 25
python scraper.py --show-db --limit 200
python scraper.py --reset-db
```

## Notes
- Deduplication is done using a unique constraint on listing URL (`url`) in `listings.db`.
- Address geocoding uses OpenStreetMap Nominatim and is cached in SQLite (`geocode_cache`) to avoid repeated lookups.
- Default Viewit URL is `https://www.viewit.ca/CityPage?CID=14`.
- Browser mode performs human-like mouse movement and scrolling before parsing page content.
- Viewit browser flow:
  - opens `CityPage?CID=14`
  - sets `maxPrice` (default `3300`)
  - clicks bedroom filters `1`, `2`, `3+`
  - clicks `Show results in List`
  - scrapes up to 5 listings per page and paginates with the next button icon
- In browser mode, Kijiji navigation visits multiple pages (`--kijiji-pages`, default `3`) with random delay (`20-30s` by default) between pages.
- Kijiji browser flow starts from `https://www.kijiji.ca/b-apartments-condos/canada/c37l0`, sets location query (default `Bloor Bathurst`), randomly picks one of first 6 suggestions, sets radius (default `2km`), clicks Apply, then paginates and scrapes.
- Site HTML can change over time, so CSS selectors/patterns may need updates if either website changes layout.
- Respect each website's terms of service and robots policies when scraping.
