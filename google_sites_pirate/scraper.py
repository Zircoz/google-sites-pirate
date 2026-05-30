import datetime
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from markdownify import markdownify as md

CONTENT_SELECTORS = [
    "div.UtePc",            # New Google Sites main content div
    "div[role='main']",
    "main",
    "article",
    "div.sites-layout-tile",
    "#sites-canvas-main-content",
]

def _ensure_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        print("  [ERROR] Playwright is not installed. To run scraping, install playwright:")
        print("    pip install playwright && playwright install chromium")
        return None

def _extract_content(soup):
    """Return (title, body_html) from a fully-rendered page BeautifulSoup."""
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    for sel in CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 20:
            # strip any nested nav/script/style inside the content block
            for tag in el.find_all(["script", "style"]):
                tag.decompose()
            return title, str(el)
    # fallback: strip known noise from body and return what's left
    body = soup.body
    if body:
        for tag in body.find_all(["nav", "header", "footer", "script", "style"]):
            tag.decompose()
        for tag in body.find_all(attrs={"role": ["banner", "navigation"]}):
            tag.decompose()
        return title, str(body)
    return title, str(soup)

def discover_page_urls(published_url, auth_state=None):
    """
    Fetch the published site home page and collect internal nav links.
    Returns list of absolute page URLs (deduplicated, same-site only).
    """
    sync_playwright = _ensure_playwright()
    if not sync_playwright:
        raise RuntimeError("Playwright is required for page discovery")

    base = published_url.rstrip("/")
    parsed = urlparse(base)
    path_prefix = parsed.path.rstrip("/")

    pages = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx_kwargs = {"viewport": {"width": 1280, "height": 900}}
        if auth_state and os.path.exists(auth_state):
            ctx_kwargs["storage_state"] = auth_state
        ctx = browser.new_context(**ctx_kwargs)
        bpage = ctx.new_page()

        try:
            bpage.goto(base, wait_until="networkidle", timeout=45000)
            
            # Check if we landed on login page
            final_url = bpage.url
            if "accounts.google.com" in final_url:
                print(f"    [WARN] Page discovery redirected to Google sign-in. Check your --playwright-auth state.")
            
            html = bpage.content()
            soup = BeautifulSoup(html, "lxml")
            
            seen = set()
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                # normalise relative → absolute
                if href.startswith("/"):
                    href = f"{parsed.scheme}://{parsed.netloc}{href}"
                if path_prefix and path_prefix in href:
                    # strip query / fragment
                    href = href.split("?")[0].split("#")[0]
                    if href not in seen:
                        seen.add(href)
                        title = a.get_text(strip=True) or href.rstrip("/").split("/")[-1]
                        pages.append({"url": href, "title": title})
            
            # Ensure the home page itself is included (as first entry)
            if base not in seen:
                pages.insert(0, {"url": base, "title": "Home"})
        finally:
            ctx.close()
            browser.close()
            
    return pages

def render_pages_playwright(page_infos, auth_state=None):
    """
    Render a list of page URLs with Playwright, return list of dicts:
      {url, title, body_md}
    """
    sync_playwright = _ensure_playwright()
    if not sync_playwright:
        return []

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx_kwargs = {"viewport": {"width": 1280, "height": 900}}
        if auth_state and os.path.exists(auth_state):
            ctx_kwargs["storage_state"] = auth_state
        ctx = browser.new_context(**ctx_kwargs)
        bpage = ctx.new_page()

        seen_final_urls = set()
        seen_content_hashes = set()
        for info in page_infos:
            url = info["url"]
            try:
                bpage.goto(url, wait_until="networkidle", timeout=45000)
                final_url = bpage.url
                # check if we landed on a login page (private site)
                if "accounts.google.com" in final_url:
                    print(f"    [SKIP] {url} requires login (site is private)")
                    continue
                # deduplicate pages that redirect to the same URL
                canonical = final_url.rstrip("/")
                if canonical in seen_final_urls:
                    print(f"    [SKIP] {url} (duplicate of already-scraped page)")
                    continue
                seen_final_urls.add(canonical)
                html = bpage.content()
                soup = BeautifulSoup(html, "lxml")
                title, body_html = _extract_content(soup)
                if not title:
                    title = info.get("title", "")
                body_mdtext = md(body_html, strip=["script", "style"]).strip()
                content_hash = hashlib.md5(body_mdtext.encode()).hexdigest()
                if content_hash in seen_content_hashes:
                    print(f"    [SKIP] {url} (duplicate content)")
                    continue
                seen_content_hashes.add(content_hash)
                results.append({"url": final_url, "title": title, "body_md": body_mdtext})
                print(f"    [OK] {title!r}")
            except Exception as e:
                print(f"    [FAIL] {url}: {e}")

        ctx.close()
        browser.close()
    return results

def _safe(text):
    return re.sub(r"[^\w\-]", "_", text).strip("_") or "unnamed"

def render_frontmatter(meta):
    def _qs(v):
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "\n".join(["---"] + [f"{k}: {_qs(v)}" for k, v in meta.items()] + ["---"])

def write_site_pages(site_id, site_name, scraped_pages, output_dir):
    output_dir = Path(output_dir)
    d = output_dir / _safe(site_name)
    d.mkdir(parents=True, exist_ok=True)
    scraped_at = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_pages = []
    ok = 0
    for i, pg in enumerate(scraped_pages):
        title = pg.get("title") or f"page_{i}"
        filename = f"{_safe(title)}__{i:03d}.md"
        fm = render_frontmatter({
            "title": title,
            "source_url": pg["url"],
            "site_id": site_id,
            "scraped_at": scraped_at,
        })
        try:
            (d / filename).write_text(f"{fm}\n\n{pg['body_md']}\n", encoding="utf-8")
            manifest_pages.append({"title": title, "file": filename, "source_url": pg["url"]})
            ok += 1
        except OSError as e:
            print(f"  [WARN] {title!r}: {e}")
    manifest = {
        "site_display_name": site_name,
        "site_id": site_id,
        "scraped_at": scraped_at,
        "pages_scraped": ok,
        "pages": manifest_pages,
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return d, ok
