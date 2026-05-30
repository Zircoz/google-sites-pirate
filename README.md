# Google Sites Pirate Scraper

This repository contains tools to retrieve metadata and scrape Google Sites page-by-page into Markdown format for RAG applications.

---

## Google Sites Scraper (`scrape_google_sites.py`)

This script discovers Google Sites via the Google Drive API or scrapes a direct published URL. It uses Playwright to render JavaScript-heavy layouts and converts the content to clean Markdown with front-matter metadata.

### Setup

1. **Install Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Install Playwright Browser Binaries**:
   ```bash
   playwright install chromium
   ```

### Usage

#### Mode A: Scraping via Direct URL (No Google Cloud Platform Setup Needed)
If you have a public Google Site URL, you can scrape it directly without needing Google Cloud credentials or API tokens:
```bash
python scrape_google_sites.py --url <published_site_url>
```
*Example:*
```bash
python scrape_google_sites.py --url https://sites.google.com/view/mpi-agri-farm/
```

#### Mode B: Auto-Discovering & Scraping via Google Drive (OAuth Credentials Needed)
To list and auto-scrape all sites associated with your Google Account:

1. Obtain an **OAuth Client ID JSON** file (configured as a Web Application with redirect URI `http://localhost:8080`) from the Google Cloud Console.
2. Run the script:
   ```bash
   python scrape_google_sites.py --credentials path/to/client_secret.json
   ```
3. The script will automatically prompt you to authenticate in your browser, cache the token, discover your sites, resolve their published URLs, and scrape them.

*To scrape a specific site ID only:*
```bash
python scrape_google_sites.py --credentials path/to/client_secret.json --site-id <google_drive_file_id>
```

#### Private Sites & Authentication
To scrape private/unpublished sites, you can pass a Playwright browser session state (containing authentication cookies):
```bash
python scrape_google_sites.py --url <private_site_url> --playwright-auth path/to/cookies.json
```

---

## Drive File Info Utilities

### 1. Service Account Metadata Lookup (`get_drive_file_info.py`)
Retrieves basic Drive file metadata using a Google Cloud Service Account:
```bash
python get_drive_file_info.py <file_id> <path_to_credential.json>
```

### 2. User OAuth Metadata Lookup (`get_drive_file_info_oauth.py`)
Retrieves basic Drive file metadata using user OAuth authentication:
```bash
python get_drive_file_info_oauth.py <file_id> <path_to_client_secret.json>
```
