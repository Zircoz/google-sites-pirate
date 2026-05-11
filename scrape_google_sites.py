#!/usr/bin/env python3
import argparse,datetime,io,json,os,re,sys,zipfile
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

def get_site_url(drive_svc,site_id):
    try:
        meta=drive_svc.files().get(fileId=site_id,fields="webViewLink",supportsAllDrives=True).execute()
        return meta.get("webViewLink","")
    except HttpError:
        return ""

def export_site_as_zip(drive_svc,site_id):
    data=drive_svc.files().export(fileId=site_id,mimeType="application/zip").execute()
    return zipfile.ZipFile(io.BytesIO(data))

def _title_from_soup(soup,fallback):
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1=soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return fallback

def parse_pages_from_zip(zf,site_url):
    pages=[]
    for name in sorted(zf.namelist()):
        if not name.endswith(".html"):
            continue
        html=zf.read(name).decode("utf-8",errors="replace")
        soup=BeautifulSoup(html,"lxml")
        parts=name.rstrip("/").split("/")
        path_title=parts[-2] if len(parts)>=2 and parts[-1]=="index.html" else parts[-1].replace(".html","")
        title=_title_from_soup(soup,path_title) or path_title
        body_html=str(soup.body) if soup.body else html
        try:
            body_md=md(body_html,strip=["script","style","head"]).strip()
        except Exception:
            body_md=soup.get_text(separator="\n").strip()
        page_slug=name.replace("index.html","").strip("/")
        source_url=f"{site_url.rstrip('/')}/{page_slug}" if site_url else name
        pages.append({"title":title,"page_path":name,"source_url":source_url,"body_md":body_md})
    return pages

def _safe(text):
    return re.sub(r"[^\w\-]","_",text).strip("_") or "unnamed"

def render_frontmatter(meta):
    def _qs(v):
        return '"'+str(v).replace("\\","\\\\").replace('"','\\"')+'"'
    lines=["---"]+[f"{k}: {_qs(v)}" for k,v in meta.items()]+["---"]
    return "\n".join(lines)

def write_site_pages(site_id,site_name,pages,output_dir):
    d=output_dir/_safe(site_name);d.mkdir(parents=True,exist_ok=True)
    scraped_at=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_pages=[];ok=0
    for i,page in enumerate(pages):
        title=page["title"] or f"page_{i}"
        filename=f"{_safe(title)}__{i:03d}.md"
        fm=render_frontmatter({"title":title,"source_url":page["source_url"],"site_id":site_id,"scraped_at":scraped_at})
        try:
            (d/filename).write_text(f"{fm}\n\n{page['body_md']}\n",encoding="utf-8")
            manifest_pages.append({"title":title,"file":filename,"source_url":page["source_url"]})
            ok+=1
        except OSError as e:
            print(f"  [WARN] {title!r}: {e}")
    manifest={"site_display_name":site_name,"site_id":site_id,"scraped_at":scraped_at,"pages_scraped":ok,"pages":manifest_pages}
    (d/"manifest.json").write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding="utf-8")
    return d,ok

def parse_args(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument("--credentials",required=True)
    p.add_argument("--site-id")
    p.add_argument("--output",default="scraped_sites")
    p.add_argument("--token-file",default="token.json")
    return p.parse_args(argv)

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
        site_url=get_site_url(svc,sid)
        try:
            zf=export_site_as_zip(svc,sid)
        except HttpError as e:
            print(f"  [FAIL] ZIP export failed: {e}")
            print("  [HINT] Google may not support ZIP export for this site. Try the Playwright approach instead.")
            continue
        pages=parse_pages_from_zip(zf,site_url)
        if not pages:
            print("  [WARN] No HTML pages found in ZIP export");continue
        print(f"  Found {len(pages)} page(s)")
        d,n=write_site_pages(sid,sname,pages,out)
        print(f"  [OK] {n} page(s) -> {d}");ok_sites+=1;total_pages+=n
    print(f"\n[DONE] {ok_sites}/{len(sites)} site(s), {total_pages} page(s) -> {out.resolve()}")

if __name__=="__main__":main()
