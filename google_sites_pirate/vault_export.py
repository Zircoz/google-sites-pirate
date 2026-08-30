"""
Google Vault export automation for Google Sites.

Implements the half of PROPOSAL_VAULT_API.md that vault.py does not cover:
triggering an org-wide Vault export of Sites data and pulling the resulting
artifacts down from Cloud Storage, so they can be handed to
vault.ingest_vault_export()/merge_with_scrape() without a human manually
running the export in the Vault UI first.

Pipeline:
  build_vault_credentials  (Service Account, optionally impersonating an
                             admin via Domain-Wide Delegation)
  → create_sites_export     (matters().exports().create(), DRIVE corpus,
                             terms='type:site', ORG_UNIT or ACCOUNT scope)
  → poll_export              (matters().exports().get() until COMPLETED,
                             retrying transient API errors)
  → download_export          (pull each Cloud Storage object to a local
                             dir, extracting the zip archives that Vault
                             Drive exports actually deliver)
"""

import json
import os
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import List, Optional

from google.auth import exceptions as google_auth_exceptions
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

VAULT_SCOPE = "https://www.googleapis.com/auth/ediscovery"
GCS_READONLY_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"

# Export reaches a terminal state once status leaves these.
_IN_PROGRESS_STATUSES = ("EXPORT_STATUS_UNSPECIFIED", "IN_PROGRESS")

# HTTP statuses worth retrying on a transient API hiccup during a
# potentially hour-long poll loop.
_RETRYABLE_HTTP_STATUSES = (429, 500, 502, 503, 504)

# HTTP statuses that mean "the caller is not allowed to touch this matter",
# as opposed to a transient failure worth retrying.
_ACCESS_DENIED_HTTP_STATUSES = (401, 403, 404)

_DWD_HINT = (
    "This usually means the Service Account is missing Domain-Wide "
    "Delegation authorization for BOTH required scopes:\n"
    f"    {VAULT_SCOPE}\n"
    f"    {GCS_READONLY_SCOPE}\n"
    "A Workspace super admin must authorize the Service Account's client ID "
    "for both scopes in Admin console -> Security -> API controls -> "
    "Domain-wide delegation."
)

_MATTER_ACCESS_HINT = (
    "The Service Account authenticated successfully but is not authorized on "
    "this Matter (or the Matter ID is wrong / the Matter is closed).\n"
    "Check, in order:\n"
    "  1. --matter-id matches an OPEN Matter in Vault.\n"
    "  2. --subject names a human Vault Administrator with access to that "
    "Matter, and the Service Account is authorized to impersonate them via "
    "Domain-Wide Delegation for both scopes:\n"
    f"       {VAULT_SCOPE}\n"
    f"       {GCS_READONLY_SCOPE}\n"
    "  3. If you are relying on the Service Account being a direct Matter "
    "collaborator rather than on impersonation, verify that grant actually "
    "took effect — Vault matter permissions are granted to Workspace users "
    "holding Vault privileges, which a *.iam.gserviceaccount.com identity "
    "generally is not. Passing --subject is the supported headless path."
)


def build_vault_credentials(
    service_account_path: Optional[str] = None,
    service_account_info: Optional[dict] = None,
    subject: Optional[str] = None,
):
    """Build Service Account credentials scoped for Vault + Cloud Storage.

    `subject` is the email of a human Vault Administrator to impersonate via
    Domain-Wide Delegation — required unless the Service Account has been
    added directly as a Matter collaborator (see PROPOSAL_VAULT_API.md).

    Falls back to the GOOGLE_SERVICE_ACCOUNT_JSON environment variable when
    neither `service_account_info` nor `service_account_path` is given, for
    parity with auth.get_google_creds().
    """
    scopes = [VAULT_SCOPE, GCS_READONLY_SCOPE]

    if not service_account_info and not service_account_path:
        env_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if env_json:
            try:
                service_account_info = json.loads(env_json)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON env var: {e}")

    if service_account_info:
        creds = service_account.Credentials.from_service_account_info(
            service_account_info, scopes=scopes
        )
    elif service_account_path:
        if not Path(service_account_path).exists():
            raise FileNotFoundError(f"Service account file not found: {service_account_path}")
        creds = service_account.Credentials.from_service_account_file(
            service_account_path, scopes=scopes
        )
    else:
        raise ValueError(
            "Either service_account_info, service_account_path, or the "
            "GOOGLE_SERVICE_ACCOUNT_JSON environment variable is required"
        )

    if subject:
        creds = creds.with_subject(subject)
    return creds


def build_vault_service(creds):
    return build("vault", "v1", credentials=creds, cache_discovery=False)


def _execute_with_retry(request_factory, description: str, max_attempts: int = 5,
                         base_delay_seconds: float = 2.0, sleep_fn=time.sleep):
    """Call `request_factory().execute()`, retrying transient (429/5xx) errors.

    Non-retryable HttpErrors (403 access denied, 404 unknown matter, etc.)
    and auth failures are raised immediately with an actionable message.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return request_factory().execute()
        except google_auth_exceptions.RefreshError as e:
            raise RuntimeError(f"{description} failed: could not obtain an access token ({e}).\n{_DWD_HINT}")
        except HttpError as e:
            status = getattr(e.resp, "status", None)
            if status in _RETRYABLE_HTTP_STATUSES and attempt < max_attempts:
                delay = base_delay_seconds * (2 ** (attempt - 1))
                print(f"    [RETRY] {description} got HTTP {status}, retrying in {delay:.0f}s "
                      f"(attempt {attempt}/{max_attempts})...")
                sleep_fn(delay)
                continue
            if status in _ACCESS_DENIED_HTTP_STATUSES:
                raise RuntimeError(f"{description} failed with HTTP {status}: {e}\n{_MATTER_ACCESS_HINT}")
            raise RuntimeError(f"{description} failed: {e}")


def create_sites_export(
    vault_svc,
    matter_id: str,
    name: str,
    org_unit_id: Optional[str] = None,
    account_emails: Optional[List[str]] = None,
    include_shared_drives: bool = True,
):
    """Trigger a Vault export of the DRIVE corpus filtered to Google Sites.

    Exactly one of `org_unit_id` (organization-wide discovery) or
    `account_emails` (per-account discovery) must be given.
    """
    if bool(org_unit_id) == bool(account_emails):
        raise ValueError("Exactly one of org_unit_id or account_emails must be provided")

    query = {
        "corpus": "DRIVE",
        "dataScope": "ALL_DATA",
        "terms": "type:site",
        "driveOptions": {"includeSharedDrives": include_shared_drives},
    }
    if org_unit_id:
        query["searchMethod"] = "ORG_UNIT"
        query["orgUnitInfo"] = {"orgUnitId": org_unit_id}
    else:
        query["searchMethod"] = "ACCOUNT"
        query["accountInfo"] = {"emails": account_emails}

    body = {
        "name": name,
        "query": query,
        "exportOptions": {"driveOptions": {"includeAccessInfo": True}},
    }

    return _execute_with_retry(
        lambda: vault_svc.matters().exports().create(matterId=matter_id, body=body),
        description="Creating Vault export",
    )


def get_export(vault_svc, matter_id: str, export_id: str, sleep_fn=time.sleep):
    return _execute_with_retry(
        lambda: vault_svc.matters().exports().get(matterId=matter_id, exportId=export_id),
        description="Fetching Vault export status",
        sleep_fn=sleep_fn,
    )


def poll_export(
    vault_svc,
    matter_id: str,
    export_id: str,
    poll_interval_seconds: int = 30,
    timeout_seconds: int = 3600,
    sleep_fn=time.sleep,
):
    """Block until the export reaches a terminal state, returning the resource.

    Raises RuntimeError if the export fails, TimeoutError if it does not
    finish within `timeout_seconds`. On timeout, the export keeps running
    server-side — re-invoke with the same matter_id/export_id (--export-id
    on the CLI) to resume waiting instead of creating a duplicate export.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        export = get_export(vault_svc, matter_id, export_id, sleep_fn=sleep_fn)
        status = export.get("status", "EXPORT_STATUS_UNSPECIFIED")
        if status == "COMPLETED":
            return export
        if status not in _IN_PROGRESS_STATUSES:
            raise RuntimeError(
                f"Vault export {export_id} (matter {matter_id}) ended with status {status!r}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Vault export {export_id} (matter {matter_id}) did not complete within "
                f"{timeout_seconds}s (last status: {status!r}). The export is still running "
                f"server-side — re-run with --export-id {export_id} to resume waiting for it "
                f"instead of triggering a new one."
            )
        print(f"    [WAIT] export status={status!r}, retrying in {poll_interval_seconds}s...")
        sleep_fn(poll_interval_seconds)


def _safe_relative_path(object_name: str) -> Path:
    """Turn a Cloud Storage object name into a safe path under a dest dir.

    Keeps the last two path segments (parent directory + filename) so files
    from Vault's per-page/per-chunk subfolders don't collide, while
    rejecting any '..' traversal segments defensively.
    """
    parts = [p for p in PurePosixPath(object_name).parts if p not in ("", ".", "..")]
    if not parts:
        raise ValueError(f"Unusable Cloud Storage object name: {object_name!r}")
    return Path(*parts[-2:]) if len(parts) > 1 else Path(parts[-1])


def _extract_zip_safely(zip_path: Path, dest_dir: Path) -> None:
    """Extract a zip archive into dest_dir, guarding against zip-slip paths."""
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            member_path = (dest_dir / member.filename).resolve()
            if not str(member_path).startswith(str(dest_dir.resolve()) + os.sep) and member_path != dest_dir.resolve():
                print(f"    [WARN] skipping unsafe zip entry: {member.filename!r}")
                continue
            zf.extract(member, path=dest_dir)


def download_export(export: dict, dest_dir: Path, creds) -> List[Path]:
    """Download every Cloud Storage object listed in a completed export's sink.

    Vault Drive exports typically deliver documents packaged as zip
    archives alongside a separate metadata XML/CSV; any downloaded .zip is
    extracted in place so the loose files land where vault.py's rglob-based
    discovery (find_vault_export_files) expects them.

    Returns the list of local file paths written (pre-extraction).
    """
    try:
        from google.cloud import storage
    except ImportError:
        raise RuntimeError(
            "google-cloud-storage is required to download Vault exports.\n"
            "Install it with: pip install 'google-sites-pirate[vault-export]'\n"
            "(or directly: pip install google-cloud-storage)"
        )

    sink = export.get("cloudStorageSink", {})
    files = sink.get("files", [])
    if not files:
        raise RuntimeError(
            f"Export {export.get('id')} has no cloudStorageSink.files — nothing to download"
        )

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    client = storage.Client(credentials=creds, project=getattr(creds, "project_id", None))
    downloaded: List[Path] = []
    for f in files:
        bucket_name = f.get("bucketName")
        object_name = f.get("objectName")
        if not bucket_name or not object_name:
            print(f"    [WARN] skipping malformed cloudStorageSink entry: {f!r}")
            continue
        local_path = dest_dir / _safe_relative_path(object_name)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        print(f"    [DOWNLOAD] gs://{bucket_name}/{object_name} -> {local_path}")
        blob = client.bucket(bucket_name).blob(object_name)
        blob.download_to_filename(str(local_path))
        downloaded.append(local_path)

        if local_path.suffix.lower() == ".zip":
            print(f"    [EXTRACT] {local_path.name} -> {local_path.parent}")
            try:
                _extract_zip_safely(local_path, local_path.parent)
            except zipfile.BadZipFile as e:
                print(f"    [WARN] could not extract {local_path.name}: {e}")

    return downloaded


def run_vault_export(
    matter_id: str,
    dest_dir: Path,
    service_account_path: Optional[str] = None,
    subject: Optional[str] = None,
    org_unit_id: Optional[str] = None,
    account_emails: Optional[List[str]] = None,
    export_name: Optional[str] = None,
    export_id: Optional[str] = None,
    include_shared_drives: bool = True,
    poll_interval_seconds: int = 30,
    timeout_seconds: int = 3600,
) -> Path:
    """End-to-end: authenticate, trigger (or resume) export, poll, download.

    If `export_id` is given, an existing export is polled/downloaded rather
    than a new one created — use this to resume after a timeout instead of
    triggering a duplicate export.

    Downloads land in `dest_dir / export_id`, keeping artifacts from
    different export runs isolated so a stale metadata XML from a previous
    run is never picked up alongside a new one.

    Returns the directory the exported artifacts were downloaded into.
    """
    print("[VAULT-EXPORT] Authenticating Service Account...")
    creds = build_vault_credentials(
        service_account_path=service_account_path, subject=subject
    )
    vault_svc = build_vault_service(creds)

    if export_id:
        print(f"[VAULT-EXPORT] Resuming export id={export_id}, polling for completion...")
    else:
        export_name = export_name or f"google-sites-pirate-{int(time.time())}"
        scope_desc = f"org unit {org_unit_id}" if org_unit_id else f"{len(account_emails)} account(s)"
        print(f"[VAULT-EXPORT] Creating export {export_name!r} for {scope_desc}...")
        export = create_sites_export(
            vault_svc,
            matter_id=matter_id,
            name=export_name,
            org_unit_id=org_unit_id,
            account_emails=account_emails,
            include_shared_drives=include_shared_drives,
        )
        export_id = export["id"]
        print(f"[VAULT-EXPORT] Export created: id={export_id}. "
              f"Save this ID to resume with --export-id if the wait times out.")

    export = poll_export(
        vault_svc,
        matter_id=matter_id,
        export_id=export_id,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
    )
    print("[VAULT-EXPORT] Export completed, downloading artifacts...")

    target_dir = Path(dest_dir) / export_id
    downloaded = download_export(export, target_dir, creds)
    print(f"[VAULT-EXPORT] Downloaded {len(downloaded)} file(s) -> {target_dir}")
    return target_dir
