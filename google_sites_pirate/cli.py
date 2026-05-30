import argparse
import sys
from pathlib import Path
from googleapiclient.discovery import build

from google_sites_pirate.auth import get_google_creds
from google_sites_pirate.drive import (
    discover_sites,
    get_published_url,
    get_file_metadata,
    SITE_MIME_TYPE,
)
from google_sites_pirate.scraper import (
    discover_page_urls,
    render_pages_playwright,
    write_site_pages,
)

def run_scrape(args):
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    if args.url:
        from urllib.parse import urlparse
        parsed = urlparse(args.url)
        # extract last slug or default name
        sname = parsed.path.rstrip("/").split("/")[-1] or "scraped_site"
        sid = "url_input"
        sites = [{"id": sid, "name": sname, "url": args.url}]
        drive_svc = None
    else:
        # Build credentials and service
        try:
            creds = get_google_creds(
                credentials_file=args.credentials,
                token_file=args.token_file,
                service_account_path=args.service_account,
                non_interactive=args.non_interactive
            )
            drive_svc = build("drive", "v3", credentials=creds)
        except Exception as e:
            print(f"[ERROR] Authentication failed: {e}")
            sys.exit(1)

        if args.site_id:
            print(f"\n[TARGET] Fetching Site metadata for ID: {args.site_id}")
            try:
                meta = get_file_metadata(drive_svc, args.site_id)
                sites = [meta]
            except Exception as e:
                print(f"[ERROR] {e}")
                sys.exit(1)
        else:
            print("\n[SEARCH] Discovering sites...")
            try:
                sites = discover_sites(drive_svc)
                print(f"  Found {len(sites)} site(s)")
            except Exception as e:
                print(f"[ERROR] {e}")
                sys.exit(1)

    if not sites:
        print("[WARN] No sites found.")
        return

    total_pages = 0
    ok_sites = 0
    for s in sites:
        sid = s["id"]
        sname = s.get("name", s["id"])
        print(f"\n[SCRAPE] Starting scrape for site: {sname!r}")

        # 1. Get published URL
        if "url" in s:
            pub_url = s["url"]
            is_public = True
        else:
            pub_url, is_public = get_published_url(drive_svc, sid)
            if not pub_url:
                print("  [SKIP] Site is not published — no published URL found.")
                print("  Publish the site in Google Sites and re-run.")
                continue
        print(f"  Published URL : {pub_url}")
        print(f"  Public access : {is_public}")

        # 2. Discover page URLs from nav links
        try:
            page_infos = discover_page_urls(pub_url, auth_state=args.playwright_auth)
        except Exception as e:
            print(f"  [FAIL] Could not fetch nav links: {e}")
            continue
        print(f"  Discovered {len(page_infos)} page URL(s):")
        for pi in page_infos:
            print(f"    {pi['url']}")

        # 3. Render each page with Playwright
        print("  Rendering pages with Playwright...")
        scraped = render_pages_playwright(page_infos, auth_state=args.playwright_auth)
        if not scraped:
            print("  [WARN] No pages rendered")
            continue

        # 4. Write output
        d, n = write_site_pages(sid, sname, scraped, out)
        print(f"  [OK] {n}/{len(page_infos)} page(s) -> {d}")
        ok_sites += 1
        total_pages += n

    print(f"\n[DONE] Successfully scraped {ok_sites}/{len(sites)} site(s), {total_pages} page(s) -> {out.resolve()}")


def run_info(args):
    try:
        creds = get_google_creds(
            credentials_file=args.credentials,
            token_file=args.token_file,
            service_account_path=args.service_account,
            non_interactive=args.non_interactive
        )
        drive_svc = build("drive", "v3", credentials=creds)
    except Exception as e:
        print(f"[ERROR] Authentication failed: {e}")
        sys.exit(1)

    print(f"[INFO] Querying file ID: {args.file_id}...")
    try:
        meta = get_file_metadata(drive_svc, args.file_id)
        print(f"File ID: {meta.get('id')}")
        print(f"File Name: {meta.get('name')}")
        print(f"MIME Type: {meta.get('mimeType')}")
        
        if "owners" in meta:
            owners = [o.get("displayName", o.get("emailAddress", "Unknown")) for o in meta["owners"]]
            print(f"Owners: {', '.join(owners)}")
        if "createdTime" in meta:
            print(f"Created: {meta['createdTime']}")
        if "modifiedTime" in meta:
            print(f"Modified: {meta['modifiedTime']}")
            
        if meta.get("mimeType") == SITE_MIME_TYPE:
            pub_url, is_public = get_published_url(drive_svc, args.file_id)
            if pub_url:
                print(f"Published Link: {pub_url}")
                print(f"Public Access: {is_public}")
            else:
                print("Published Link: (Not Published)")
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Google Sites per-page scraper and Drive metadata tool."
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommands")

    # Scrape subcommand
    scrape_parser = subparsers.add_parser("scrape", help="Scrape a Google Site into Markdown")
    scrape_parser.add_argument("--url", help="Direct published URL of a Google Site to scrape (no auth required)")
    scrape_parser.add_argument("--site-id", help="Drive file ID of a specific site to scrape")
    scrape_parser.add_argument("--credentials", help="Path to Google OAuth client secret JSON file")
    scrape_parser.add_argument("--service-account", help="Path to Google Service Account credentials JSON file")
    scrape_parser.add_argument("--token-file", default="token.json", help="Path to write/read cached OAuth token")
    scrape_parser.add_argument("--output", default="scraped_sites", help="Output directory for markdown files")
    scrape_parser.add_argument("--playwright-auth", help="Path to Playwright storage state JSON (cookies) for private sites")
    scrape_parser.add_argument("--non-interactive", action="store_true", help="Disable interactive OAuth web browser prompt")

    # Info subcommand
    info_parser = subparsers.add_parser("info", help="Retrieve metadata info of a Google Drive file")
    info_parser.add_argument("file_id", help="The Google Drive file ID to inspect")
    info_parser.add_argument("--credentials", help="Path to Google OAuth client secret JSON file")
    info_parser.add_argument("--service-account", help="Path to Google Service Account credentials JSON file")
    info_parser.add_argument("--token-file", default="token.json", help="Path to write/read cached OAuth token")
    info_parser.add_argument("--non-interactive", action="store_true", help="Disable interactive OAuth web browser prompt")

    args = parser.parse_args(argv)

    if args.command == "scrape":
        # Validate that we have some way to identify the target
        if not args.url and not args.credentials and not args.service_account:
            # Check environment variables before complaining
            import os
            has_env_auth = any(os.environ.get(var) for var in [
                "GOOGLE_SERVICE_ACCOUNT_JSON",
                "GOOGLE_CREDENTIALS_JSON",
                "GOOGLE_TOKEN_JSON"
            ])
            if not has_env_auth:
                scrape_parser.error("either --url, --credentials, --service-account or credentials environment variables must be provided")
        run_scrape(args)
    elif args.command == "info":
        run_info(args)


if __name__ == "__main__":
    main()
