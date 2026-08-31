# Proposal: Enhancing `google-sites-pirate` for Google Workspace Enterprise via Google Vault API

## Executive Summary
The current `google-sites-pirate` tool successfully scrapes Google Sites into Markdown for RAG applications. However, its reliance on the Drive API for discovery and Playwright for page-by-page rendering makes it difficult to scale within large Google Workspace Enterprise environments.

This proposal outlines an architectural shift to integrate the **Google Vault API**. By leveraging Vault's eDiscovery and bulk export capabilities, we can transform `google-sites-pirate` into a high-performance, organization-wide archiving and RAG ingestion tool, capable of moving page rendering server-side (no local browser fleet) while ensuring strict compliance with enterprise data governance.

---

## Limitations of the Current Architecture in Enterprise

1. **Scalability and Speed**: The current tool renders each page using a headless browser (Playwright). For an enterprise with thousands of sites and hundreds of thousands of pages, UI rendering is unacceptably slow and resource-intensive.
2. **Discovery & Access**: The current Drive API discovery (`mimeType='application/vnd.google-apps.site'`) requires the authenticating user (or Service Account) to have explicit share access to the sites. It cannot holistically map the organization's sites without widespread permission adjustments.
3. **Robustness**: Web scraping relies on DOM selectors (`div.UtePc`, etc.) and hyperlink discovery. If a site has unlinked pages (orphaned pages) or dynamic rendering issues, data is missed.
4. **Compliance & State**: Scraping published URLs only captures the *current* state of the site. It does not easily integrate with legal holds or point-in-time compliance snapshots required by Enterprise legal departments.

---

## The Google Vault API Solution

The Google Vault API is purpose-built for enterprise data retention, eDiscovery, and exporting. Google Sites are considered part of Google Drive data within Vault.

By integrating Vault API, `google-sites-pirate` can request server-side bulk exports of all Google Sites data across an entire Organization or specific Organizational Units (OUs), without needing Playwright or explicit share permissions.

### Proposed Capabilities

1. **Organization-Wide Discovery (Super Admin Access)**
   Instead of searching a single user's Drive, we can create a Vault "Matter" and query the `DRIVE` corpus with the term `type:site`. This queries all sites across the specified OU, including privately shared and unpublished sites, bypassing the need for individual share access.

2. **Bulk Server-Side Export (no local browser fleet)**
   Vault can generate an asynchronous export of the underlying Site data directly to Google Cloud Storage. Rendering is not eliminated — Vault delivers a Chromium-rendered PDF per page — but it happens server-side on Google's infrastructure instead of via a local Playwright fleet clicking through navigations. The tool downloads the raw data sink files directly from the GCP bucket.

3. **Complete Content Coverage**
   Vault exports include exact metadata, revision history contexts, and all page content, including unlinked/orphaned and unpublished pages. This guarantees complete *coverage* for RAG systems, not complete *fidelity*: because the artifacts are PDFs, extracting text from them (pdfminer + regex de-noising) loses link targets, heading semantics, table structure, and image alt text, so structural fidelity is actually lower than the existing HTML-to-markdownify scrape path.

4. **Point-in-Time & Held Data Scraping**
   The tool can be configured to scrape data subject to Legal Holds or point-in-time constraints (e.g., "Export sites as they were on Q3 close").

---

## Technical Implementation Plan

We propose adding a new subcommand or mode to the CLI: `google-sites-pirate vault-export`.

### Step 1: Authentication & Authorization (Service Account)
To operate headlessly in an enterprise, the tool will use a **Google Cloud Service Account** instead of requiring interactive user OAuth.

There are two primary ways a Service Account can access Vault Matters created by human administrators:
1. **Domain-Wide Delegation (Recommended for Enterprise)**: A Google Workspace super admin can grant the Service Account [Domain-Wide Delegation](https://developers.google.com/identity/protocols/oauth2/service-account#delegatingauthority). This allows the Service Account to impersonate a human Vault Administrator and access all matters that the human admin can see.
2. **Direct Matter Collaboration**: Alternatively, a human Vault Administrator can create a Matter in the Vault UI and explicitly add the Service Account's email address (e.g., `my-sa@project.iam.gserviceaccount.com`) as a **Collaborator** on that specific Matter using the [matters.addPermissions](https://developers.google.com/workspace/vault/reference/rest/v1/matters/addPermissions) API or via the Vault UI.

The tool requires two OAuth scopes: `https://www.googleapis.com/auth/ediscovery` (to trigger/poll the export) and `https://www.googleapis.com/auth/devstorage.read_only` (to download the resulting artifacts from Cloud Storage). Under Domain-Wide Delegation, both scopes must be authorized for the Service Account's client ID, or token minting fails outright.

### Step 2: Matter Selection & Query Creation
*   The human admin will create a Matter (e.g., "RAG Ingestion - Sites") and either share it with the Service Account or allow the Service Account to impersonate them.
*   The tool will accept a `--matter-id` argument.
*   Construct a Query targeting the `DRIVE` corpus:
    ```python
    query = {
        'corpus': 'DRIVE',
        'dataScope': 'ALL_DATA',
        'searchMethod': 'ORG_UNIT', # or 'ACCOUNT'
        'orgUnitInfo': {'orgUnitId': 'id:my_org_unit_id'},
        'terms': 'type:site',
        'driveOptions': {'includeSharedDrives': True}
    }
    ```

### Step 3: Triggering the Export
*   Call `vault_v1.matters().exports().create()` to initiate the backend export to Cloud Storage.
*   Implement a polling mechanism to check the export status until it transitions to `COMPLETED`.

### Step 4: Secure Download & Parsing
*   Once completed, parse the `cloudStorageSink` from the Vault API response.
*   Use the Google Cloud Storage API to download the exported artifacts (which contain the Sites data and metadata XML).
*   **New Parsing Engine**: Develop a parser to convert the Vault exported format (which contains the raw text and structure of the sites) directly into the Markdown front-matter format expected by the existing `google-sites-pirate` output.
    *(Note: This replaces the Playwright/BeautifulSoup HTML scraping with direct data parsing).*

---

## Conclusion

By adopting the Google Vault API, `google-sites-pirate` will evolve from a targeted web-scraper into a robust, enterprise-grade ingestion pipeline. It will offer substantial speed and scale gains by moving rendering server-side and dropping the local browser fleet, guarantee complete page coverage (including unlinked and unpublished pages) through administrative scopes, and align perfectly with Enterprise compliance and governance standards — at the cost of lower structural fidelity than the HTML scrape path, since the exported artifacts are PDFs.
