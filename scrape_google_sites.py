#!/usr/bin/env python3
"""
Google Sites per-page scraper.

Strategy:
  1. Drive API  → get site name + published URL (from revisions)
  2. requests   → fetch published URL, discover all page URLs from nav links
  3. Playwright → render each page with full JS, extract content
  4. markdownify → convert HTML to Markdown with YAML front-matter per page

For private / unpublished sites the script will print a message and skip;
a future --auth flag can add Playwright cookie-based auth.
"""
import argparse,datetime,hashlib,json,os,re,sys
from pathlib import Path
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES=["https://www.googleapis.com/auth/drive.readonly"]
SITE_MIME_TYPE="application/vnd.google-apps.site"
OAUTH_PORT=8080

# ── auth ──────────────────────────────────────────────────────────────────────

def build_oauth_creds(credentials_file,token_file="token.json"):
    creds=None
    if os.path.exists(token_file):
        creds=Credentials.from_authorized_user_file(token_file,SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("  Refreshing expired token...");creds.refresh(Request())
        else:
            print("  Opening browser for Google sign-in...")
            flow=InstalledAppFlow.from_client_secrets_file(credentials_file,SCOPES)
            creds=flow.run_local_server(port=OAUTH_PORT)
        open(token_file,"w").write(creds.to_json())
        print(f"  Token cached -> {token_file}")
    return creds

# ── Drive API helpers ─────────────────────────────────────────────────────────

def discover_sites(drive_svc):
    sites,page_token=[],None
    print("  Querying Drive...")
    while True:
        resp=drive_svc.files().list(
            q=f"mimeType='{SITE_MIME_TYPE}' and trashed=false",
            fields="nextPageToken,files(id,name)",pageSize=100,
            pageToken=page_token,includeItemsFromAllDrives=True,supportsAllDrives=True
        ).execute()
        sites.extend(resp.get("files",[]))
        page_token=resp.get("nextPageToken")
        if not page_token:break
    return sites

def get_published_url(drive_svc,site_id):
    """Return (published_url, is_public) from Drive revisions, or (None,False)."""
    try:
        revs=drive_svc.revisions().list(
            fileId=site_id,
            fields="revisions(published,publishedLink,publishedOutsideDomain)"
        ).execute().get("revisions",[])
        for rev in reversed(revs):
            if rev.get("published") and rev.get("publishedLink"):
                is_public=rev.get("publishedOutsideDomain",False)
                return rev["publishedLink"],is_public
    except HttpError:
        pass
    return None,False

# ── page discovery ────────────────────────────────────────────────────────────

def discover_page_urls(published_url):
    """
    Fetch the published site home page and collect internal nav links.
    Returns list of absolute page URLs (deduplicated, same-site only).
    """
    import requests as req
    base=published_url.rstrip("/")
    # derive the base path prefix (e.g. /view/my-site)
    from urllib.parse import urlparse
    parsed=urlparse(base)
    path_prefix=parsed.path.rstrip("/")

    resp=req.get(base,timeout=20)
    resp.raise_for_status()
    soup=BeautifulSoup(resp.text,"lxml")

    seen=set()
    pages=[]
    for a in soup.find_all("a",href=True):
        href=a["href"].strip()
        # normalise relative → absolute
        if href.startswith("/"):
            href=f"{parsed.scheme}://{parsed.netloc}{href}"
        if path_prefix and path_prefix in href:
            # strip query / fragment
            href=href.split("?")[0].split("#")[0]
            if href not in seen:
                seen.add(href)
                title=a.get_text(strip=True) or href.rstrip("/").split("/")[-1]
                pages.append({"url":href,"title":title})
    # Ensure the home page itself is included (as first entry)
    if base not in seen:
        pages.insert(0,{"url":base,"title":"Home"})
    return pages

# ── Playwright rendering ──────────────────────────────────────────────────────

def _ensure_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        print("  [ERROR] playwright not installed. Run:")
        print("    pip install playwright && playwright install chromium")
        return None

CONTENT_SELECTORS=[
    "div.UtePc",            # New Google Sites main content div
    "div[role='main']",
    "main",
    "article",
    "div.sites-layout-tile",
    "#sites-canvas-main-content",
]

def _extract_content(soup):
    """Return (title, body_html) from a fully-rendered page BeautifulSoup."""
    title=""
    if soup.title and soup.title.string:
        title=soup.title.string.strip()
    for sel in CONTENT_SELECTORS:
        el=soup.select_one(sel)
        if el and len(el.get_text(strip=True))>20:
            # strip any nested nav/script/style inside the content block
            for tag in el.find_all(["script","style"]):
                tag.decompose()
            return title,str(el)
    # fallback: strip known noise from body and return what's left
    body=soup.body
    if body:
        for tag in body.find_all(["nav","header","footer","script","style"]):
            tag.decompose()
        for tag in body.find_all(attrs={"role":["banner","navigation"]}):
            tag.decompose()
        return title,str(body)
    return title,str(soup)

def render_pages_playwright(page_infos,auth_state=None):
    """
    Render a list of page URLs with Playwright, return list of dicts:
      {url, title, body_md}
    """
    sync_playwright=_ensure_playwright()
    if not sync_playwright:
        return []

    results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        ctx_kwargs={"viewport":{"width":1280,"height":900}}
        if auth_state and os.path.exists(auth_state):
            ctx_kwargs["storage_state"]=auth_state
        ctx=browser.new_context(**ctx_kwargs)
        bpage=ctx.new_page()

        seen_final_urls=set()
        seen_content_hashes=set()
        for info in page_infos:
            url=info["url"]
            try:
                bpage.goto(url,wait_until="networkidle",timeout=45000)
                final_url=bpage.url
                # check if we landed on a login page (private site)
                if "accounts.google.com" in final_url:
                    print(f"    [SKIP] {url} requires login (site is private)")
                    continue
                # deduplicate pages that redirect to the same URL
                canonical=final_url.rstrip("/")
                if canonical in seen_final_urls:
                    print(f"    [SKIP] {url} (duplicate of already-scraped page)")
                    continue
                seen_final_urls.add(canonical)
                html=bpage.content()
                soup=BeautifulSoup(html,"lxml")
                title,body_html=_extract_content(soup)
                if not title:
                    title=info.get("title","")
                body_mdtext=md(body_html,strip=["script","style"]).strip()
                content_hash=hashlib.md5(body_mdtext.encode()).hexdigest()
                if content_hash in seen_content_hashes:
                    print(f"    [SKIP] {url} (duplicate content)")
                    continue
                seen_content_hashes.add(content_hash)
                results.append({"url":final_url,"title":title,"body_md":body_mdtext})
                print(f"    [OK] {title!r}")
            except Exception as e:
                print(f"    [FAIL] {url}: {e}")

        ctx.close();browser.close()
    return results

# ── output helpers ────────────────────────────────────────────────────────────

def _safe(text):
    return re.sub(r"[^\w\-]","_",text).strip("_") or "unnamed"

def render_frontmatter(meta):
    def _qs(v):
        return '"'+str(v).replace("\\","\\\\").replace('"','\\"')+'"'
    return "\n".join(["---"]+[f"{k}: {_qs(v)}" for k,v in meta.items()]+["---"])

def write_site_pages(site_id,site_name,scraped_pages,output_dir):
    d=output_dir/_safe(site_name);d.mkdir(parents=True,exist_ok=True)
    scraped_at=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_pages=[];ok=0
    for i,pg in enumerate(scraped_pages):
        title=pg.get("title") or f"page_{i}"
        filename=f"{_safe(title)}__{i:03d}.md"
        fm=render_frontmatter({
            "title":title,
            "source_url":pg["url"],
            "site_id":site_id,
            "scraped_at":scraped_at,
        })
        try:
            (d/filename).write_text(f"{fm}\n\n{pg['body_md']}\n",encoding="utf-8")
            manifest_pages.append({"title":title,"file":filename,"source_url":pg["url"]})
            ok+=1
        except OSError as e:
            print(f"  [WARN] {title!r}: {e}")
    manifest={
        "site_display_name":site_name,"site_id":site_id,
        "scraped_at":scraped_at,"pages_scraped":ok,"pages":manifest_pages,
    }
    (d/"manifest.json").write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding="utf-8")
    return d,ok

# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    p=argparse.ArgumentParser(description="Scrape Google Sites page-by-page for RAG")
    p.add_argument("--credentials",required=True,help="OAuth client ID JSON")
    p.add_argument("--site-id",help="Drive file ID of a specific site")
    p.add_argument("--output",default="scraped_sites",help="Output directory")
    p.add_argument("--token-file",default="token.json",help="OAuth token cache")
    p.add_argument("--playwright-auth",default=None,
                   help="Path to Playwright storage state JSON for private sites")
    return p.parse_args(argv)

# ── main ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    args=parse_args(argv)
    print("\n[AUTH]  Authenticating via OAuth...")
    creds=build_oauth_creds(args.credentials,args.token_file)
    print("  [OK] Credentials ready")
    svc=build("drive","v3",credentials=creds)
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)

    if args.site_id:
        print(f"\n[TARGET]  Site ID: {args.site_id}")
        try:
            meta=svc.files().get(fileId=args.site_id,fields="id,name",supportsAllDrives=True).execute()
            sites=[meta]
        except HttpError as e:
            print(f"[ERROR] {e}");sys.exit(1)
    else:
        print("\n[SEARCH]  Discovering sites...")
        sites=discover_sites(svc)
        print(f"  Found {len(sites)} site(s)")

    if not sites:
        print("[WARN] No sites found.");return

    total_pages=0;ok_sites=0
    for s in sites:
        sid,sname=s["id"],s.get("name",s["id"])
        print(f"\n[SCRAPE]  {sname!r}")

        # 1. Get published URL
        pub_url,is_public=get_published_url(svc,sid)
        if not pub_url:
            print("  [SKIP] Site is not published — no published URL found.")
            print("  Publish the site in Google Sites and re-run.")
            continue
        print(f"  Published URL : {pub_url}")
        print(f"  Public access : {is_public}")

        # 2. Discover page URLs from nav links
        try:
            page_infos=discover_page_urls(pub_url)
        except Exception as e:
            print(f"  [FAIL] Could not fetch nav links: {e}");continue
        print(f"  Discovered {len(page_infos)} page URL(s):")
        for pi in page_infos:
            print(f"    {pi['url']}")

        # 3. Render each page with Playwright
        print("  Rendering pages with Playwright...")
        scraped=render_pages_playwright(page_infos,auth_state=args.playwright_auth)
        if not scraped:
            print("  [WARN] No pages rendered");continue

        # 4. Write output
        d,n=write_site_pages(sid,sname,scraped,out)
        print(f"  [OK] {n}/{len(page_infos)} page(s) -> {d}")
        ok_sites+=1;total_pages+=n

    print(f"\n[DONE] {ok_sites}/{len(sites)} site(s), {total_pages} page(s) -> {out.resolve()}")

if __name__=="__main__":main()
