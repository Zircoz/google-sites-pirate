# Google Sites Pirate Scraper

This repository contains tools to retrieve metadata and scrape Google Sites page-by-page into Markdown format for RAG (Retrieval-Augmented Generation) applications.

Looking to integrate this package programmatically into your Python code or contribute to its development? Check out the [Developer Guide](docs/developer_guide.md).

---

## Installation

Install the package directly from this repository:

```bash
pip install .
```

After installation, install the required Playwright browser binary:

```bash
playwright install chromium
```

---

## Command-Line Interface (`google-sites-pirate`)

This package registers a global command `google-sites-pirate` which supports two main subcommands: `scrape` and `info`.

---

### 1. `scrape` Command

Scrapes Google Sites page-by-page. It renders the site using Playwright to handle JavaScript-heavy layouts and converts the page content into Markdown with front-matter metadata.

#### Mode A: Scraping via Direct URL (No Google Cloud Platform Setup Needed)
If you have a public Google Site URL, you can scrape it directly without needing Google Cloud credentials or API tokens:
```bash
google-sites-pirate scrape --url <published_site_url>
```
*Example:*
```bash
google-sites-pirate scrape --url https://sites.google.com/view/mpi-agri-farm/
```

#### Mode B: Discovery via Google Drive (OAuth Credentials Needed)
To list and auto-scrape all sites associated with your Google Account:
1. Run the command:
   ```bash
   google-sites-pirate scrape --credentials path/to/client_secret.json
   ```
2. The command will automatically prompt you to authenticate in your browser, cache the token to `token.json`, discover your sites, resolve their published URLs, and scrape them.

To scrape a specific site ID only via OAuth:
```bash
google-sites-pirate scrape --credentials path/to/client_secret.json --site-id <google_drive_file_id>
```

#### Mode C: Google Service Account (Recommended for Non-Interactive Pipelines)
To run in a headless, non-interactive CI/CD pipeline, you can use a Google Service Account:
```bash
google-sites-pirate scrape --service-account path/to/service_account.json
```
*Note: Make sure to share your Google Sites (or their Google Drive folders) with the Service Account email address.*

#### Private Sites & Authentication
To scrape private or unpublished sites, you can pass a Playwright browser session state (containing authentication cookies):
```bash
google-sites-pirate scrape --url <private_url> --playwright-auth path/to/cookies.json
```

---

### 2. `info` Command

Retrieves basic Google Drive file metadata and published status details.

#### Retrieve Metadata via Service Account
```bash
google-sites-pirate info <file_id> --service-account path/to/service_account.json
```

#### Retrieve Metadata via User OAuth
```bash
google-sites-pirate info <file_id> --credentials path/to/client_secret.json
```

---

### 3. `vault` Command

Ingests a Google Vault GSites export (metadata XML + custodian CSV + per-page PDFs) into the same Markdown format as `scrape`, optionally merging it into existing scraper output. See `PROPOSAL_VAULT_API.md` for the full design rationale.

#### Ingest a pre-existing export directory
```bash
google-sites-pirate vault path/to/export_dir --output vault_output
```

#### Trigger and download the export via the Vault API (`--trigger-export`)
Instead of manually running the export in the Vault UI, the tool can trigger it, poll for completion, and download the artifacts itself:
```bash
google-sites-pirate vault ./vault_dl --trigger-export \
  --matter-id <VAULT_MATTER_ID> \
  --service-account path/to/service_account.json \
  --subject admin@yourdomain.com \
  --org-unit-id <ORG_UNIT_ID>
```
* Requires the Service Account to be authorized (via [Domain-Wide Delegation](https://developers.google.com/identity/protocols/oauth2/service-account#delegatingauthority) with `--subject`, or as a direct [Matter collaborator](https://developers.google.com/workspace/vault/reference/rest/v1/matters/addPermissions)) for **both** the `ediscovery` and `devstorage.read_only` scopes — missing either causes authentication to fail outright.
* Use `--account-email` (repeatable) instead of `--org-unit-id` to scope the export to specific accounts.
* If the wait exceeds `--timeout` (default 3600s), the export keeps running server-side. Re-run with `--export-id <ID>` (printed on creation) to resume polling instead of triggering a duplicate export.
* Downloads land in `<export_dir>/<export_id>` so artifacts from different runs never mix.
* `--org-unit-id` must be the full prefixed Admin SDK org unit ID (e.g. `id:03ph8a2z1k9qrst`), not the bare ID shown in the Admin console URL.
* `--trigger-export` needs the optional Cloud Storage dependency: `pip install 'google-sites-pirate[vault-export]'` (or `pip install google-cloud-storage`). It is not needed for `scrape` or for ingesting a pre-existing export directory.
* Exports land in a Google-managed Cloud Storage bucket (not one you configure) and expire after roughly 15 days; they also count against per-Matter export limits, and this tool does not delete old exports, so repeated runs without `--export-id` will accumulate them.

#### Merge vault metadata into existing scraper output
```bash
google-sites-pirate vault path/to/export_dir --merge path/to/scraped_site_dir
```

---

## Environment Variable Authentication

For stateless environments (e.g. Docker, GitLab CI/CD, GitHub Actions) where mounting secret files is undesirable, you can set the following environment variables. The CLI will automatically detect them:

*   **Google Service Account**: Set `GOOGLE_SERVICE_ACCOUNT_JSON` to the raw JSON string content of your service account key.
*   **Google Client Secrets (OAuth)**: Set `GOOGLE_CREDENTIALS_JSON` to the raw JSON string content of your client secrets.
*   **Cached OAuth Token**: Set `GOOGLE_TOKEN_JSON` to the raw JSON string content of your cached user token.

When these environment variables are set, you can run the commands without specifying the file path flags:
```bash
# Automatically picks up service account details from the environment variable
google-sites-pirate scrape
```

---

## Stateless CI/CD Pipeline Integration (GitLab CI Example)

Below is an example `.gitlab-ci.yml` showing how to run the scraper in a CI/CD job using a Google Service Account stored as an environment variable (`GOOGLE_SERVICE_ACCOUNT_JSON`):

```yaml
stages:
  - scrape

run-scraper:
  stage: scrape
  image: python:3.11-slim
  variables:
    # Ensure Playwright dependencies are handled, or use the official playwright image
    PLAYWRIGHT_BROWSERS_PATH: "$CI_PROJECT_DIR/.playwright"
  cache:
    paths:
      - .playwright/
  script:
    # Install dependencies
    - pip install .
    # Install browser and system dependencies for Playwright
    - playwright install chromium --with-deps
    # Execute scraper. Auth token is read directly from environment
    - google-sites-pirate scrape --output output_dir
  artifacts:
    paths:
      - output_dir/
    expire_in: 1 week
```
