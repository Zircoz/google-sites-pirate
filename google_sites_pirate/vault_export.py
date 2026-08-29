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
  → poll_export              (matters().exports().get() until COMPLETED)
  → download_export          (pull each Cloud Storage object to a local dir)
"""

import time
from pathlib import Path
from typing import List, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

VAULT_SCOPE = "https://www.googleapis.com/auth/ediscovery"
GCS_READONLY_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"

# Export reaches a terminal state once status leaves these.
_IN_PROGRESS_STATUSES = ("EXPORT_STATUS_UNSPECIFIED", "IN_PROGRESS")


def build_vault_credentials(
    service_account_path: Optional[str] = None,
    service_account_info: Optional[dict] = None,
    subject: Optional[str] = None,
):
    """Build Service Account credentials scoped for Vault + Cloud Storage.

    `subject` is the email of a human Vault Administrator to impersonate via
    Domain-Wide Delegation — required unless the Service Account has been
    added directly as a Matter collaborator (see PROPOSAL_VAULT_API.md).
    """
    scopes = [VAULT_SCOPE, GCS_READONLY_SCOPE]

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
        raise ValueError("Either service_account_info or service_account_path is required")

    if subject:
        creds = creds.with_subject(subject)
    return creds


def build_vault_service(creds):
    return build("vault", "v1", credentials=creds, cache_discovery=False)


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

    try:
        return vault_svc.matters().exports().create(matterId=matter_id, body=body).execute()
    except HttpError as e:
        raise RuntimeError(f"Failed to create Vault export: {e}")


def get_export(vault_svc, matter_id: str, export_id: str):
    try:
        return vault_svc.matters().exports().get(matterId=matter_id, exportId=export_id).execute()
    except HttpError as e:
        raise RuntimeError(f"Failed to fetch Vault export status: {e}")


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
    finish within `timeout_seconds`.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        export = get_export(vault_svc, matter_id, export_id)
        status = export.get("status", "EXPORT_STATUS_UNSPECIFIED")
        if status == "COMPLETED":
            return export
        if status not in _IN_PROGRESS_STATUSES:
            raise RuntimeError(f"Vault export {export_id} ended with status {status!r}: {export}")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Vault export {export_id} did not complete within {timeout_seconds}s "
                f"(last status: {status!r})"
            )
        print(f"    [WAIT] export status={status!r}, retrying in {poll_interval_seconds}s...")
        sleep_fn(poll_interval_seconds)


def download_export(export: dict, dest_dir: Path, creds) -> List[Path]:
    """Download every Cloud Storage object listed in a completed export's sink.

    Returns the list of local file paths written.
    """
    try:
        from google.cloud import storage
    except ImportError:
        raise RuntimeError(
            "google-cloud-storage is required to download Vault exports.\n"
            "Install it with: pip install google-cloud-storage"
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
        bucket_name = f["bucketName"]
        object_name = f["objectName"]
        local_path = dest_dir / Path(object_name).name
        print(f"    [DOWNLOAD] gs://{bucket_name}/{object_name} -> {local_path}")
        blob = client.bucket(bucket_name).blob(object_name)
        blob.download_to_filename(str(local_path))
        downloaded.append(local_path)

    return downloaded


def run_vault_export(
    matter_id: str,
    dest_dir: Path,
    service_account_path: Optional[str] = None,
    subject: Optional[str] = None,
    org_unit_id: Optional[str] = None,
    account_emails: Optional[List[str]] = None,
    export_name: Optional[str] = None,
    include_shared_drives: bool = True,
    poll_interval_seconds: int = 30,
    timeout_seconds: int = 3600,
) -> Path:
    """End-to-end: authenticate, trigger export, poll, download.

    Returns the directory the exported artifacts were downloaded into.
    """
    export_name = export_name or f"google-sites-pirate-{int(time.time())}"

    print("[VAULT-EXPORT] Authenticating Service Account...")
    creds = build_vault_credentials(
        service_account_path=service_account_path, subject=subject
    )
    vault_svc = build_vault_service(creds)

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
    print(f"[VAULT-EXPORT] Export created: id={export_id}, polling for completion...")

    export = poll_export(
        vault_svc,
        matter_id=matter_id,
        export_id=export_id,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
    )
    print("[VAULT-EXPORT] Export completed, downloading artifacts...")

    downloaded = download_export(export, dest_dir, creds)
    print(f"[VAULT-EXPORT] Downloaded {len(downloaded)} file(s) -> {dest_dir}")
    return dest_dir
