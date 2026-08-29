import argparse
import os
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
from google_sites_pirate.vault import (
    find_vault_export_files,
    parse_metadata_xml,
    parse_custodian_csv,
    link_pdfs_to_metadata,
    ingest_vault_export,
    merge_with_scrape,
)
from google_sites_pirate.vault_export import run_vault_export

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


def run_vault(args):
    export_dir = Path(args.export_dir)

    if args.trigger_export:
        try:
            export_dir = run_vault_export(
                matter_id=args.matter_id,
                dest_dir=export_dir,
                service_account_path=args.service_account,
                subject=args.subject,
                org_unit_id=args.org_unit_id,
                account_emails=args.account_email,
                export_name=args.export_name,
                export_id=args.export_id,
                include_shared_drives=not args.exclude_shared_drives,
                poll_interval_seconds=args.poll_interval,
                timeout_seconds=args.timeout,
            )
        except Exception as e:
            print(f'[ERROR] Vault export failed: {e}')
            sys.exit(1)
    elif not export_dir.exists():
        print(f'[ERROR] Export directory not found: {export_dir}')
        sys.exit(1)

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    if args.merge:
        # Two-phase mode: ingest vault, then merge into existing scraper output
        merge_dir = Path(args.merge)
        if not merge_dir.exists():
            print(f'[ERROR] Merge target directory not found: {merge_dir}')
            sys.exit(1)

        print(f'[VAULT] Mode: merge into existing scraper output at {merge_dir}')

        # Parse vault export to get page metadata + link PDFs
        _, _, pdf_paths = find_vault_export_files(export_dir)
        xml_path = next(export_dir.rglob('*-metadata.xml'), None) or next(export_dir.rglob('*.xml'), None)

        vault_pages = []
        if xml_path:
            print(f'[VAULT] Parsing {xml_path.name}...')
            vault_pages = parse_metadata_xml(xml_path)
            print(f'  {len(vault_pages)} page record(s) found')
        if pdf_paths:
            vault_pages = link_pdfs_to_metadata(vault_pages, pdf_paths)
            linked = sum(1 for p in vault_pages if p.pdf_path)
            print(f'  {linked}/{len(vault_pages)} pages linked to PDFs')

        if not vault_pages:
            print('[WARN] No vault pages found — nothing to merge.')
            return

        result_dir, enriched, added = merge_with_scrape(merge_dir, vault_pages)
        print(
            f'\n[DONE] Merge complete → {result_dir.resolve()}\n'
            f'  {enriched} existing page(s) enriched with vault metadata\n'
            f'  {added} hidden-nav page(s) added from vault'
        )
    else:
        # Standalone mode: write vault export as its own Markdown directory
        d, n = ingest_vault_export(
            export_dir=export_dir,
            output_dir=out,
            site_name=args.site_name or '',
        )
        print(f'\n[DONE] {n} page(s) written → {d.resolve()}')


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

    # Vault subcommand
    vault_parser = subparsers.add_parser(
        "vault",
        help=(
            "Ingest a Google Vault GSites export (metadata XML + PDFs) into Markdown, "
            "optionally triggering and downloading the export itself via --trigger-export"
        ),
    )
    vault_parser.add_argument(
        "export_dir",
        help=(
            "Path to the Vault export directory (containing *-metadata.xml and "
            "*_gsite_0/*.pdf). With --trigger-export, used as the download "
            "destination instead."
        ),
    )
    vault_parser.add_argument(
        "--output",
        default="vault_output",
        help="Output directory for Markdown files (default: vault_output)",
    )
    vault_parser.add_argument(
        "--merge",
        metavar="SCRAPE_DIR",
        help=(
            "Path to an existing google-sites-pirate scraper output directory. "
            "When set, vault metadata is merged into that output: matched pages are "
            "enriched with vault_* frontmatter fields, and hidden-nav pages are added "
            "as new files."
        ),
    )
    vault_parser.add_argument(
        "--site-name",
        default="",
        help="Override the site display name used for the output subdirectory",
    )
    vault_parser.add_argument(
        "--trigger-export",
        action="store_true",
        help=(
            "Trigger the Vault export via the Vault API instead of reading a "
            "pre-existing export directory. export_dir is used as the download "
            "destination and does not need to exist beforehand."
        ),
    )
    vault_parser.add_argument(
        "--matter-id",
        help="Vault Matter ID to export from (required with --trigger-export)",
    )
    vault_parser.add_argument(
        "--export-id",
        help=(
            "Resume an already-created export instead of triggering a new one "
            "(e.g. after a previous run timed out while polling). Requires "
            "--matter-id; --org-unit-id/--account-email are not needed."
        ),
    )
    vault_parser.add_argument(
        "--org-unit-id",
        help="Organizational Unit ID for org-wide Sites discovery (searchMethod=ORG_UNIT)",
    )
    vault_parser.add_argument(
        "--account-email",
        action="append",
        help="Account email to scope the export to (searchMethod=ACCOUNT); repeatable",
    )
    vault_parser.add_argument(
        "--service-account",
        help=(
            "Path to a Service Account JSON key with Vault access. Required with "
            "--trigger-export unless GOOGLE_SERVICE_ACCOUNT_JSON is set in the environment."
        ),
    )
    vault_parser.add_argument(
        "--subject",
        help="Email of a Vault Administrator to impersonate via Domain-Wide Delegation",
    )
    vault_parser.add_argument(
        "--export-name",
        help="Display name for the Vault export (default: auto-generated)",
    )
    vault_parser.add_argument(
        "--exclude-shared-drives",
        action="store_true",
        help="Exclude shared drives from the export query (included by default)",
    )
    vault_parser.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Seconds between export status checks, minimum 10 (default: 30)",
    )
    vault_parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Max seconds to wait for the export to complete (default: 3600)",
    )

    # Info subcommand
    info_parser = subparsers.add_parser("info", help="Retrieve metadata info of a Google Drive file")
    info_parser.add_argument("file_id", help="The Google Drive file ID to inspect")
    info_parser.add_argument("--credentials", help="Path to Google OAuth client secret JSON file")
    info_parser.add_argument("--service-account", help="Path to Google Service Account credentials JSON file")
    info_parser.add_argument("--token-file", default="token.json", help="Path to write/read cached OAuth token")
    info_parser.add_argument("--non-interactive", action="store_true", help="Disable interactive OAuth web browser prompt")

    args = parser.parse_args(argv)

    if args.command == "vault":
        if args.trigger_export:
            if not args.matter_id:
                vault_parser.error("--matter-id is required with --trigger-export")
            if not args.service_account and not os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
                vault_parser.error(
                    "--service-account (or GOOGLE_SERVICE_ACCOUNT_JSON env var) is required with --trigger-export"
                )
            if not args.export_id and bool(args.org_unit_id) == bool(args.account_email):
                vault_parser.error(
                    "exactly one of --org-unit-id or --account-email must be provided with "
                    "--trigger-export, unless resuming via --export-id"
                )
            if args.poll_interval < 10:
                vault_parser.error("--poll-interval must be at least 10 seconds")
        else:
            trigger_only_args = {
                "--matter-id": args.matter_id,
                "--export-id": args.export_id,
                "--org-unit-id": args.org_unit_id,
                "--account-email": args.account_email,
                "--subject": args.subject,
                "--export-name": args.export_name,
            }
            ignored = [name for name, value in trigger_only_args.items() if value]
            if ignored:
                print(
                    f"[WARN] {', '.join(ignored)} only apply with --trigger-export and will be ignored."
                )
        run_vault(args)
    elif args.command == "scrape":
        # Validate that we have some way to identify the target
        if not args.url and not args.credentials and not args.service_account:
            # Check environment variables before complaining
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
