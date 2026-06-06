# Developer Guide

Welcome to the **Google Sites Pirate** developer guide. This document provides technical details, API references, and code examples for developers who want to integrate this package programmatically or contribute to its development.

---

## 1. Project Architecture

The codebase is structured into a lightweight, modular package:

- **[`google_sites_pirate/scraper.py`](file:///c:/Users/Anshul%20B/Codes/google-sites-pirate/google_sites_pirate/scraper.py)**: Handles scraping pages using Playwright (rendering JavaScript-heavy layouts), extracting main body content via BeautifulSoup selectors, converting HTML to Markdown, and writing pages to disk with front-matter.
- **[`google_sites_pirate/auth.py`](file:///c:/Users/Anshul%20B/Codes/google-sites-pirate/google_sites_pirate/auth.py)**: Handles Google authentication (User OAuth flow and Service Account credentials) with automated environment variable fallback and token caching.
- **[`google_sites_pirate/drive.py`](file:///c:/Users/Anshul%20B/Codes/google-sites-pirate/google_sites_pirate/drive.py)**: Communicates with the Google Drive API to discover Sites files, resolve their published URLs, and query file metadata.
- **[`google_sites_pirate/cli.py`](file:///c:/Users/Anshul%20B/Codes/google-sites-pirate/google_sites_pirate/cli.py)**: Command-line interface logic using `argparse`.

---

## 2. Programmatic API Reference

### Scraper Module (`google_sites_pirate.scraper`)

#### `discover_page_urls(published_url, auth_state=None)`
Loads the home page of a Google Site using Playwright and extracts all unique, internal navigation links belonging to the same site path.
- **Arguments:**
  - `published_url` *(str)*: The home page URL of the published Google Site.
  - `auth_state` *(str, optional)*: Path to a Playwright storage state JSON file containing session cookies (useful for private sites).
- **Returns:** A list of dictionaries: `[{"url": "...", "title": "..."}, ...]`

#### `render_pages_playwright(page_infos, auth_state=None)`
Renders a list of page URLs using Playwright to handle JavaScript rendering, strips noisy boilerplate elements (like navigation, headers, footers), and converts the page content to Markdown. It automatically skips duplicate content or login-walled pages.
- **Arguments:**
  - `page_infos` *(list[dict])*: A list of dicts containing page details (typically returned by `discover_page_urls`).
  - `auth_state` *(str, optional)*: Path to a Playwright storage state JSON file containing session cookies.
- **Returns:** A list of dictionaries containing rendered Markdown: `[{"url": "...", "title": "...", "body_md": "..."}, ...]`

#### `write_site_pages(site_id, site_name, scraped_pages, output_dir)`
Saves the scraped pages to the disk. Each page is written as a separate Markdown file with front-matter metadata containing the title, source URL, site ID, and scrape timestamp. It also generates a `manifest.json` outlining all scraped pages.
- **Arguments:**
  - `site_id` *(str)*: Unique identifier for the Google Site.
  - `site_name` *(str)*: Display name of the Google Site.
  - `scraped_pages` *(list[dict])*: Output from `render_pages_playwright`.
  - `output_dir` *(str / Path)*: Target directory for writing files.
- **Returns:** A tuple: `(Path to site output directory, number of successfully written pages)`

---

### Authentication Module (`google_sites_pirate.auth`)

#### `get_google_creds(credentials_file=None, token_file="token.json", service_account_path=None, non_interactive=False)`
Initializes Google credentials for API requests. It dynamically prioritizes credentials in the following order:
1. Google Service Account key from the `GOOGLE_SERVICE_ACCOUNT_JSON` environment variable.
2. Google Service Account key from the file path (`service_account_path`).
3. User OAuth token loaded from the `GOOGLE_TOKEN_JSON` environment variable.
4. User OAuth token loaded from the file path (`token_file`).
5. Brand new User OAuth flow using client secrets from the `GOOGLE_CREDENTIALS_JSON` environment variable or client secrets file (`credentials_file`).
- **Returns:** A `google.oauth2` credentials object.

---

### Drive Module (`google_sites_pirate.drive`)

#### `discover_sites(drive_svc)`
Queries Google Drive for all files matching the Google Site MIME type (`application/vnd.google-apps.site`).
- **Arguments:**
  - `drive_svc`: An authenticated Google Drive API service object (built via `googleapiclient.discovery.build`).
- **Returns:** A list of dictionaries: `[{"id": "...", "name": "..."}, ...]`

#### `get_published_url(drive_svc, site_id)`
Inspects Google Drive revision history for the given file ID to locate the latest published URL and determine if the site is publicly visible.
- **Returns:** A tuple: `(published_link_str or None, is_public_bool)`

---

## 3. Programmatic Usage Examples

### Example A: Scraping a Public Site Directly
If you already know the public URL of a Google Site, you can scrape it programmatically without setting up Google API credentials:

```python
from google_sites_pirate.scraper import (
    discover_page_urls,
    render_pages_playwright,
    write_site_pages
)

# Define the target Google Site URL
site_url = "https://sites.google.com/view/mpi-agri-farm/"

# 1. Discover all internal pages
print("Discovering page URLs...")
pages = discover_page_urls(site_url)

# 2. Render pages and convert to Markdown
print("Scraping and rendering pages...")
scraped_data = render_pages_playwright(pages)

# 3. Save to disk
output_dir = "./scraped_sites"
site_name = "MPI Agri Farm"
site_id = "manual-url-scrape"

saved_dir, count = write_site_pages(
    site_id=site_id,
    site_name=site_name,
    scraped_pages=scraped_data,
    output_dir=output_dir
)

print(f"Scraped {count} pages. Output saved in {saved_dir}")
```

### Example B: Crawling Google Drive & Scraping via OAuth
If you want to authenticate as a user, fetch all available Google Sites from Google Drive, and scrape them:

```python
from googleapiclient.discovery import build
from google_sites_pirate.auth import get_google_creds
from google_sites_pirate.drive import discover_sites, get_published_url
from google_sites_pirate.scraper import (
    discover_page_urls,
    render_pages_playwright,
    write_site_pages
)

# 1. Get OAuth credentials
creds = get_google_creds(
    credentials_file="client_secret.json",
    token_file="token.json"
)

# 2. Build Drive Service
drive_service = build("drive", "v3", credentials=creds)

# 3. Discover sites in Drive
sites = discover_sites(drive_service)

# 4. Iterate and scrape each site
for site in sites:
    site_id = site["id"]
    site_name = site["name"]
    print(f"\nProcessing Site: {site_name} (ID: {site_id})")

    published_url, is_public = get_published_url(drive_service, site_id)
    if not published_url:
        print("  - Site is not published or has no accessible link.")
        continue

    print(f"  - Published URL: {published_url}")
    
    # Discover, render and write
    page_urls = discover_page_urls(published_url)
    scraped_pages = render_pages_playwright(page_urls)
    
    write_site_pages(
        site_id=site_id,
        site_name=site_name,
        scraped_pages=scraped_pages,
        output_dir="./scraped_sites"
    )
```

---

## 4. Local Development Setup

To modify or contribute to this project, set up your development environment as follows:

1. **Clone the repository:**
   ```bash
   git clone https://github.com/Zircoz/google-sites-pirate.git
   cd google-sites-pirate
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv venv
   # On Windows (Powershell):
   .\venv\Scripts\Activate.ps1
   # On macOS/Linux:
   source venv/bin/activate
   ```

3. **Install the package in editable/development mode:**
   ```bash
   pip install -e .
   ```

4. **Install Playwright browser binaries:**
   ```bash
   playwright install chromium
   ```

5. **Contributing Guidelines:**
   - Follow PEP 8 guidelines for formatting python files.
   - If adding new elements or custom selectors to handle distinct Google Sites templates, update `CONTENT_SELECTORS` in [`google_sites_pirate/scraper.py`](file:///c:/Users/Anshul%20B/Codes/google-sites-pirate/google_sites_pirate/scraper.py).
