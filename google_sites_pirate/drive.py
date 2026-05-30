from googleapiclient.errors import HttpError

SITE_MIME_TYPE = "application/vnd.google-apps.site"

def discover_sites(drive_svc):
    """
    Search Google Drive for files matching the Google Site MIME type.
    """
    sites, page_token = [], None
    print("  Querying Google Drive for Sites...")
    while True:
        try:
            resp = drive_svc.files().list(
                q=f"mimeType='{SITE_MIME_TYPE}' and trashed=false",
                fields="nextPageToken,files(id,name)",
                pageSize=100,
                pageToken=page_token,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True
            ).execute()
            sites.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        except HttpError as e:
            raise RuntimeError(f"Drive API site discovery failed: {e}")
    return sites

def get_published_url(drive_svc, site_id):
    """
    Retrieve (published_url, is_public) from Drive revisions, or (None, False).
    """
    try:
        revs = drive_svc.revisions().list(
            fileId=site_id,
            fields="revisions(published,publishedLink,publishedOutsideDomain)"
        ).execute().get("revisions", [])
        
        # Traverse revisions in reverse chronological order to find the latest published one
        for rev in reversed(revs):
            if rev.get("published") and rev.get("publishedLink"):
                is_public = rev.get("publishedOutsideDomain", False)
                return rev["publishedLink"], is_public
    except HttpError as e:
        print(f"  [WARN] Failed to fetch revisions for site {site_id}: {e}")
    return None, False

def get_file_metadata(drive_svc, file_id):
    """
    Retrieve detailed metadata for a specific file ID.
    """
    try:
        meta = drive_svc.files().get(
            fileId=file_id,
            fields="id,name,mimeType,owners,createdTime,modifiedTime",
            supportsAllDrives=True
        ).execute()
        return meta
    except HttpError as e:
        raise RuntimeError(f"Drive API metadata request failed: {e}")
