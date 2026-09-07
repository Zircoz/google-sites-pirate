"""
Google Vault export ingestion for Google Sites.

Handles the three artifact types produced by a Vault GSites export:
  *-metadata.xml        — per-page provenance (DocID, timestamps, PublishedURL, …)
  *-custodian-docid.csv — custodian token → Drive parent folder ID mapping
  *_gsite_0/*.pdf       — one Chromium-rendered PDF per page

Pipeline:
  parse_metadata_xml + find_vault_export_files + link_pdfs_to_metadata
  → pdf_to_markdown (extract_pdf_text → strip_html_css_noise → _text_to_markdown)
  → write_vault_pages  (same frontmatter + manifest.json format as scraper.py)

For combining with scraper output, use merge_with_scrape.
"""

import csv
import datetime
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Noise-detection patterns ────────────────────────────────────────────────
_HTML_TAG_RE = re.compile(r'<[a-zA-Z/][^>]{0,200}>')
_CSS_PROP_LINE_RE = re.compile(r'^\s*[\w-]+\s*:\s*.+;\s*$', re.MULTILINE)

# Drive file IDs are 20-50 alphanumeric/dash/underscore chars
_DRIVE_ID_LIKE = re.compile(r'^[A-Za-z0-9_\-]{20,50}$')

# Recognized CSS property names, used to confirm a `{ … }` block is really a
# CSS rule and not, say, prose that happens to contain a brace. Not
# exhaustive — just the common ones that show up in Sites HTML-widget CSS.
_CSS_PROPERTY_NAMES = {
    'background', 'background-color', 'background-image', 'background-size',
    'background-position', 'background-repeat', 'border', 'border-radius',
    'border-color', 'border-width', 'border-style', 'border-top', 'border-bottom',
    'border-left', 'border-right', 'color', 'display', 'flex', 'flex-direction',
    'flex-wrap', 'flex-grow', 'flex-shrink', 'font', 'font-family', 'font-size',
    'font-weight', 'font-style', 'height', 'width', 'margin', 'margin-top',
    'margin-bottom', 'margin-left', 'margin-right', 'padding', 'padding-top',
    'padding-bottom', 'padding-left', 'padding-right', 'position', 'top', 'left',
    'right', 'bottom', 'z-index', 'text-align', 'text-decoration', 'text-transform',
    'line-height', 'letter-spacing', 'box-shadow', 'box-sizing', 'overflow',
    'overflow-x', 'overflow-y', 'opacity', 'transition', 'transform', 'cursor',
    'vertical-align', 'white-space', 'justify-content', 'align-items',
    'align-content', 'min-width', 'max-width', 'min-height', 'max-height',
    'float', 'clear', 'content', 'outline', 'visibility', 'list-style',
    'text-shadow', 'word-wrap', 'word-break',
}

# A CSS declaration: `prop-name: value;` (value kept short/simple; this is a
# heuristic, not a real CSS parser).
_CSS_DECLARATION_RE = re.compile(r'([a-zA-Z-]{2,32})\s*:\s*[^;{}]{1,200};')

# Selector-ish tokens: class/id selectors, @-rules, and bare element selectors.
_CSS_SELECTOR_TOKEN = (
    r'(?:\.[A-Za-z_-][\w-]*|#[A-Za-z_-][\w-]*'
    r'|@media[^{]{0,200}|@keyframes[^{]{0,200}|@font-face'
    r'|(?:html|body|div|span|p|a|ul|li|h[1-6])\b)'
)

# One or more comma-separated selector tokens immediately followed by a
# `{ … }` rule body. DOTALL so a body wrapped across many short lines (as
# pdfminer tends to do) still matches as a single block.
_CSS_RULE_BLOCK_RE = re.compile(
    _CSS_SELECTOR_TOKEN + r'(?:\s*,\s*' + _CSS_SELECTOR_TOKEN + r')*\s*\{[^{}]{0,4000}\}',
    re.DOTALL,
)

# An @-rule wrapper left empty after its inner rules were excised.
_CSS_EMPTY_ATRULE_RE = re.compile(
    r'@(?:media|supports|keyframes|font-face)[^{}]{0,200}\{\s*\}',
    re.IGNORECASE,
)

# Raw <style>/<script> blocks that leaked into the PDF text layer verbatim.
_STYLE_SCRIPT_BLOCK_RE = re.compile(r'<(style|script)\b[^>]*>.*?</\1\s*>', re.IGNORECASE | re.DOTALL)


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class VaultPage:
    doc_id: str
    title: str
    date_created: str
    date_modified: str
    published_url: str
    shared_drive_id: str
    doc_parent_id: str
    collaborators: List[str] = field(default_factory=list)
    pdf_path: Optional[Path] = None


# ── Metadata XML parsing ──────────────────────────────────────────────────────

def parse_metadata_xml(xml_path: Path) -> List[VaultPage]:
    """Parse Vault *-metadata.xml into VaultPage records.

    Handles three encodings seen in the wild:
      Attribute-style:   <Field name="#Title">value</Field>
      Element-style:     <DocID>value</DocID>
      Real Vault export: <Document DocID="…"><Tag TagName="#Title" TagValue="…"/></Document>
        (both the field name and its value live in attributes on a
        self-closing <Tag> element; the doc_id itself is an attribute on
        the enclosing <Document> element, not a nested field.)
    """
    tree = ET.parse(str(xml_path))
    root = tree.getroot()

    def _local(tag: str) -> str:
        return tag.split('}')[-1] if '}' in tag else tag

    # Find document-level containers
    doc_elements: List[ET.Element] = []
    for elem in root.iter():
        if _local(elem.tag) in ('Document', 'document', 'Item', 'item', 'Record', 'Entry', 'Doc'):
            doc_elements.append(elem)
    if not doc_elements:
        doc_elements = list(root)  # treat direct children as docs

    pages: List[VaultPage] = []
    for doc in doc_elements:
        # Strategy 1: <Field name="…">value</Field> and <Tag TagName="…" TagValue="…"/>
        field_map: Dict[str, str] = {}
        for child in doc.iter():
            name_attr = child.get('name') or child.get('Name') or child.get('fieldName')
            if name_attr:
                field_map[name_attr] = (child.text or '').strip()
            tag_name = child.get('TagName')
            if tag_name:
                field_map[tag_name] = (child.get('TagValue') or '').strip()

        # Strategy 2: <DocID>value</DocID>
        elem_map: Dict[str, str] = {}
        for child in doc:
            elem_map[_local(child.tag)] = (child.text or '').strip()

        def _get(*keys: str) -> str:
            for k in keys:
                if k in field_map:
                    return field_map[k]
                if k in elem_map:
                    return elem_map[k]
                k_lower = k.lower().lstrip('#')
                for fk, fv in field_map.items():
                    if fk.lower().lstrip('#') == k_lower:
                        return fv
                for ek, ev in elem_map.items():
                    if ek.lower().lstrip('#') == k_lower:
                        return ev
            return ''

        # Collaborators may repeat as separate child elements — either as
        # dedicated <Collaborator> elements or as repeated
        # <Tag TagName="#Collaborators" TagValue="…"/> entries. field_map is a
        # plain dict so repeats there would overwrite each other; collect them
        # explicitly instead.
        collaborators: List[str] = []
        for child in doc.iter():
            tag_name = child.get('TagName')
            if tag_name in ('Collaborators', '#Collaborators', 'Collaborator'):
                val = (child.get('TagValue') or '').strip()
                if val:
                    collaborators.append(val)
                continue
            if _local(child.tag) in ('Collaborator', 'collaborator') or \
               child.get('name') in ('Collaborators', '#Collaborators', 'Collaborator'):
                val = (child.text or '').strip()
                if val:
                    collaborators.append(val)
        if not collaborators:
            raw = _get('Collaborators', '#Collaborators')
            if raw:
                collaborators = [c.strip() for c in raw.split(',') if c.strip()]

        # doc_id: real Vault exports carry it as an attribute on the
        # <Document> element itself — check that before falling back to the
        # nested-field lookup used by the legacy encodings.
        doc_id = doc.get('DocID') or doc.get('docId') or doc.get('DocumentId')
        if not doc_id:
            doc_id = _get('DocID', 'docId', 'DocumentId')
        if not doc_id:
            continue

        pages.append(VaultPage(
            doc_id=doc_id,
            title=_get('#Title', 'Title', 'title', 'Name'),
            date_created=_get('#DateCreated', 'DateCreated', 'CreatedTime', 'createdTime'),
            date_modified=_get('#DateModified', 'DateModified', 'ModifiedTime', 'modifiedTime'),
            published_url=_get('PublishedURL', 'publishedUrl', 'PublishedUrl', 'URL'),
            shared_drive_id=_get('SharedDriveID', 'SharedDriveId', 'sharedDriveId'),
            doc_parent_id=_get('DocParentId', 'ParentId', 'parentId'),
            collaborators=collaborators,
        ))

    return pages


# ── Custodian CSV ────────────────────────────────────────────────────────────

def parse_custodian_csv(csv_path: Path) -> Dict[str, str]:
    """Parse *-custodian-docid.csv → {custodian_token: drive_folder_id}."""
    result: Dict[str, str] = {}
    with open(csv_path, newline='', encoding='utf-8') as f:
        for row in csv.reader(f):
            if len(row) >= 2:
                result[row[0].strip()] = row[1].strip()
    return result


# ── Export directory discovery ────────────────────────────────────────────────

def find_vault_export_files(export_dir: Path) -> Tuple[Optional[Path], Optional[Path], List[Path]]:
    """Locate metadata XML, custodian CSV, and PDFs in a Vault export directory.

    Returns (xml_path, csv_path, pdf_paths); xml/csv may be None if absent.
    """
    export_dir = Path(export_dir)

    xml_path: Optional[Path] = next(export_dir.rglob('*-metadata.xml'), None)
    if xml_path is None:
        xml_path = next(export_dir.rglob('*.xml'), None)

    csv_path: Optional[Path] = next(export_dir.rglob('*-custodian-docid.csv'), None)
    if csv_path is None:
        csv_path = next(export_dir.rglob('*.csv'), None)

    pdf_paths: List[Path] = sorted(export_dir.rglob('*.pdf'))

    return xml_path, csv_path, pdf_paths


# ── PDF ↔ metadata linking ────────────────────────────────────────────────────

def _parse_doc_id_from_filename(pdf_stem: str) -> str:
    """Extract doc_id from PDF filename: {site}_{title}_{parent_id}_{doc_id}.

    Walks from the right looking for the last Drive-ID-shaped segment.
    """
    for part in reversed(pdf_stem.split('_')):
        if _DRIVE_ID_LIKE.match(part):
            return part
    return ''


def link_pdfs_to_metadata(vault_pages: List[VaultPage], pdf_paths: List[Path]) -> List[VaultPage]:
    """Attach each PDF to the VaultPage whose doc_id matches its filename.

    Real Drive/Vault DocIDs commonly contain underscores (e.g.
    "1abc...XYZ_23wRqLk"), so splitting the filename on "_" and grabbing the
    last segment (as _parse_doc_id_from_filename does) slices such IDs in
    half and misses almost every match. Instead, match each PDF against the
    already-known real DocIDs from the metadata XML by checking whether the
    filename stem ENDS WITH a known DocID — longest DocIDs first, so a DocID
    that happens to be a suffix of a longer one can't steal its PDF.
    """
    id_to_page = {p.doc_id: p for p in vault_pages if p.doc_id}
    # Longest first: a shorter DocID that is a suffix of a longer one must
    # not be allowed to match before the longer, more specific one is tried.
    known_ids = sorted(id_to_page.keys(), key=len, reverse=True)

    unmatched_pdfs: List[Path] = []
    for pdf_path in pdf_paths:
        stem = pdf_path.stem
        # Take the LONGEST DocID this filename ends with, whether or not that
        # page already has a PDF. Falling through to a shorter DocID that is
        # merely a suffix of the real one would attach this PDF to a
        # different page — the exact mislinking this matching exists to stop.
        matched_id = next((d for d in known_ids if stem.endswith(d)), None)
        if matched_id and id_to_page[matched_id].pdf_path is None:
            id_to_page[matched_id].pdf_path = pdf_path
        elif matched_id:
            print(
                f'    [WARN] {pdf_path.name}: DocID {matched_id} already has a PDF '
                f'({id_to_page[matched_id].pdf_path.name}); leaving this one unlinked.'
            )
        else:
            unmatched_pdfs.append(pdf_path)

    # Last-resort fallback: if there are still-unmatched PDFs and pages left
    # over after suffix matching, and the counts happen to align, pair them
    # in listing order. This is inherently a guess, so make it loud.
    unmatched_pages = [p for p in vault_pages if p.pdf_path is None]
    if unmatched_pdfs and unmatched_pages and len(unmatched_pdfs) == len(unmatched_pages):
        print(
            f'    [WARN] {len(unmatched_pdfs)} PDF(s) could not be matched by DocID; '
            'pairing with remaining unmatched pages by file-listing order (best effort).'
        )
        for page, pdf in zip(unmatched_pages, unmatched_pdfs):
            page.pdf_path = pdf

    return vault_pages


# ── PDF text extraction ───────────────────────────────────────────────────────

def extract_pdf_text(pdf_path: Path) -> str:
    """Extract raw text from a PDF with pdfminer.six.

    Page breaks are preserved as form-feed (\\f) characters.
    Returns empty string on failure.
    """
    try:
        from pdfminer.high_level import extract_text as _extract
        return _extract(str(pdf_path))
    except ImportError:
        raise RuntimeError(
            "pdfminer.six is required for PDF text extraction.\n"
            "Install it with: pip install pdfminer.six"
        )
    except Exception as e:
        print(f'    [WARN] PDF extraction failed for {pdf_path.name}: {e}')
        return ''


# ── HTML/CSS noise stripping ──────────────────────────────────────────────────

def _is_html_css_noise(paragraph: str) -> bool:
    """Return True if a paragraph is raw HTML/CSS source code rather than prose.

    Pages built with the Google Sites HTML widget dump their raw source into the
    PDF text layer.  We detect this by looking for high densities of HTML tags,
    CSS property lines, curly braces, and minified (very long, space-sparse) lines.
    """
    stripped = paragraph.strip()
    if not stripped:
        return False

    words = stripped.split()
    word_count = max(len(words), 1)
    char_count = max(len(stripped), 1)

    html_tags = _HTML_TAG_RE.findall(stripped)
    if len(html_tags) / word_count > 0.2:
        return True

    css_props = _CSS_PROP_LINE_RE.findall(stripped)
    if len(css_props) >= 3:
        return True

    brace_density = (stripped.count('{') + stripped.count('}')) / char_count
    if brace_density > 0.03 and stripped.count('{') >= 4:
        return True

    special = sum(1 for c in stripped if c in '{}[]();:<>')
    if word_count > 5 and special / char_count > 0.15:
        return True

    # Minified lines: very long with almost no whitespace
    for line in stripped.splitlines():
        if len(line) > 300 and line.count(' ') < len(line) * 0.05:
            return True

    return False


def _excise_css_blocks(text: str) -> str:
    """Remove CSS/script noise embedded inside otherwise-prose text.

    Density-based paragraph filtering (`_is_html_css_noise`) only works when
    an entire paragraph is noise. Pages built with the Google Sites embedded
    HTML-widget feature often interleave real prose with raw CSS in the same
    paragraph, and pdfminer frequently wraps that CSS across many short
    lines rather than a few long minified ones — so neither the per-line nor
    the per-paragraph density signals fire. Instead, find CSS constructs by
    PATTERN (a selector-ish token immediately followed by a `{ … }` block
    whose body contains real `prop: value;` declarations) and excise just
    those spans, leaving surrounding prose untouched.

    Deliberately conservative: a `{ … }` block only counts as CSS if its body
    contains at least two declarations and at least one uses a recognized
    CSS property name — a stray brace or colon in prose won't match.
    """
    text = _STYLE_SCRIPT_BLOCK_RE.sub(' ', text)

    def _replace(match: 're.Match') -> str:
        block = match.group(0)
        declarations = [d.lower() for d in _CSS_DECLARATION_RE.findall(block)]
        if not declarations:
            return block
        # A single `prop: value;` is enough when the property is one we
        # recognize — real widget CSS is full of one-line rules such as
        # `.hero { background-color: #fff; }`. Without a recognized property
        # we demand a denser block before calling it CSS.
        recognized = any(d in _CSS_PROPERTY_NAMES for d in declarations)
        if recognized or len(declarations) >= 3:
            return ' '
        return block  # not confidently CSS — leave it alone

    text = _CSS_RULE_BLOCK_RE.sub(_replace, text)
    # Excising the inner rules of an @media/@supports/@keyframes block leaves
    # its now-empty wrapper behind; drop that too.
    text = _CSS_EMPTY_ATRULE_RE.sub(' ', text)
    return text


def strip_html_css_noise(text: str) -> str:
    """Remove HTML/CSS noise paragraphs from extracted PDF text.

    Pages with embedded Slides/Docs iframes produce blank sections; pages with
    HTML widgets produce CSS/HTML blobs.  Both are filtered here.
    Page breaks (\\f) become Markdown horizontal rules.
    """
    sections: List[str] = re.split(r'\f', text)
    clean_sections: List[str] = []

    for section in sections:
        section = _excise_css_blocks(section)
        paragraphs = re.split(r'\n{2,}', section)
        clean_paras: List[str] = []
        for para in paragraphs:
            if _is_html_css_noise(para):
                continue
            # Strip any residual inline HTML tags from otherwise-clean paragraphs
            cleaned = _HTML_TAG_RE.sub('', para).strip()
            if cleaned:
                clean_paras.append(cleaned)
        if clean_paras:
            clean_sections.append('\n\n'.join(clean_paras))

    return '\n\n---\n\n'.join(clean_sections)


# ── Text → Markdown ───────────────────────────────────────────────────────────

def _text_to_markdown(text: str) -> str:
    """Apply light heuristic structure to cleaned PDF text.

    Short isolated lines that do not end with sentence-terminal punctuation
    are promoted to ## headings.  Multiple blank lines are collapsed.
    """
    lines = text.splitlines()
    output: List[str] = []

    for i, raw_line in enumerate(lines):
        line = raw_line.rstrip()

        if not line:
            output.append('')
            continue

        prev_blank = (i == 0) or (not lines[i - 1].strip())
        next_blank = (i == len(lines) - 1) or (not lines[i + 1].strip())

        is_heading_candidate = (
            prev_blank
            and next_blank
            and len(line) < 80
            and not line.rstrip().endswith(('.', ',', ';', ':', '?', '!'))
            and len(line.split()) >= 2
            and line == line.strip()
        )

        output.append(f'## {line}' if is_heading_candidate else line)

    return re.sub(r'\n{3,}', '\n\n', '\n'.join(output)).strip()


def pdf_to_markdown(pdf_path: Path) -> str:
    """Full PDF → Markdown pipeline: extract → strip noise → structure."""
    raw = extract_pdf_text(pdf_path)
    if not raw:
        return ''
    cleaned = strip_html_css_noise(raw)
    if not cleaned:
        return ''
    return _text_to_markdown(cleaned)


# ── Output helpers (mirrors scraper.py) ──────────────────────────────────────

def _safe(text: str) -> str:
    return re.sub(r'[^\w\-]', '_', text).strip('_') or 'unnamed'


def _render_frontmatter(meta: dict) -> str:
    def _qs(v: str) -> str:
        return '"' + str(v).replace('\\', '\\\\').replace('"', '\\"') + '"'
    return '\n'.join(['---'] + [f'{k}: {_qs(v)}' for k, v in meta.items()] + ['---'])


# ── Write vault pages ─────────────────────────────────────────────────────────

def write_vault_pages(
    vault_pages: List[VaultPage],
    output_dir: Path,
    site_name: str = 'vault_export',
) -> Tuple[Path, int]:
    """Write vault pages to Markdown files with YAML frontmatter + manifest.json.

    Output format is compatible with write_site_pages in scraper.py.
    Vault-specific fields are prefixed with vault_ to avoid colliding with
    scraper fields when the two outputs are later merged.
    """
    output_dir = Path(output_dir)
    d = output_dir / _safe(site_name)
    d.mkdir(parents=True, exist_ok=True)

    exported_at = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
    manifest_pages: List[dict] = []
    ok = 0

    for i, page in enumerate(vault_pages):
        title = page.title or f'page_{i}'
        filename = f'{_safe(title)}__{i:03d}.md'

        fm_data: dict = {
            'title': title,
            'source_url': page.published_url,
            'vault_doc_id': page.doc_id,
            'vault_parent_id': page.doc_parent_id,
            'vault_shared_drive_id': page.shared_drive_id,
            'vault_created_at': page.date_created,
            'vault_modified_at': page.date_modified,
            'vault_exported_at': exported_at,
        }
        if page.collaborators:
            fm_data['vault_collaborators'] = ', '.join(page.collaborators)

        if page.pdf_path:
            body_md = pdf_to_markdown(page.pdf_path)
            print(f'    [OK] {title!r}  ← {page.pdf_path.name}')
        else:
            body_md = '_No PDF found for this page in the Vault export._'
            print(f'    [WARN] {title!r}: no PDF linked')

        try:
            (d / filename).write_text(
                f'{_render_frontmatter(fm_data)}\n\n{body_md}\n', encoding='utf-8'
            )
            manifest_pages.append({
                'title': title,
                'file': filename,
                'source_url': page.published_url,
                'vault_doc_id': page.doc_id,
            })
            ok += 1
        except OSError as e:
            print(f'    [WARN] could not write {filename!r}: {e}')

    manifest = {
        'site_display_name': site_name,
        'vault_export': True,
        'exported_at': exported_at,
        'pages_total': len(vault_pages),
        'pages_written': ok,
        'pages': manifest_pages,
    }
    (d / 'manifest.json').write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8'
    )
    return d, ok


# ── Merge vault into existing scraper output ──────────────────────────────────

def merge_with_scrape(
    scrape_output_dir: Path,
    vault_pages: List[VaultPage],
) -> Tuple[Path, int, int]:
    """Merge vault metadata into an existing scraper output directory.

    For pages that exist in both outputs (matched by PublishedURL or title):
      → inserts vault_* fields into the existing frontmatter

    For vault-only pages (hidden-nav pages the scraper never saw):
      → writes new Markdown files and appends entries to manifest.json

    Returns (output_path, enriched_count, new_hidden_pages_count).
    """
    scrape_dir = Path(scrape_output_dir)
    manifest_path = scrape_dir / 'manifest.json'

    if not manifest_path.exists():
        raise FileNotFoundError(f'manifest.json not found in {scrape_dir}')

    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))

    def _norm(url: str) -> str:
        return url.rstrip('/').lower()

    scrape_by_url: Dict[str, dict] = {}
    scrape_by_title: Dict[str, dict] = {}
    for pg in manifest.get('pages', []):
        url = pg.get('source_url', '')
        title = pg.get('title', '')
        if url:
            scrape_by_url[_norm(url)] = pg
        if title:
            scrape_by_title[title.lower()] = pg

    enriched = 0
    hidden_pages: List[VaultPage] = []

    for vp in vault_pages:
        norm_vurl = _norm(vp.published_url) if vp.published_url else ''
        matched = scrape_by_url.get(norm_vurl) or scrape_by_title.get(vp.title.lower())

        vault_extra = {
            'vault_doc_id': vp.doc_id,
            'vault_parent_id': vp.doc_parent_id,
            'vault_shared_drive_id': vp.shared_drive_id,
            'vault_created_at': vp.date_created,
            'vault_modified_at': vp.date_modified,
        }
        if vp.collaborators:
            vault_extra['vault_collaborators'] = ', '.join(vp.collaborators)

        if matched:
            md_path = scrape_dir / matched['file']
            if md_path.exists():
                content = md_path.read_text(encoding='utf-8')
                extra_lines = '\n'.join(f'{k}: "{v}"' for k, v in vault_extra.items())
                # Insert vault fields right after the opening ---
                content = re.sub(
                    r'^(---\n)',
                    lambda m: m.group(1) + extra_lines + '\n',
                    content,
                    count=1,
                )
                md_path.write_text(content, encoding='utf-8')
                enriched += 1
                print(f'    [ENRICH] {vp.title!r}')
        else:
            hidden_pages.append(vp)

    # Write hidden-nav pages as new files
    added = 0
    if hidden_pages:
        merged_at = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        start_idx = len(manifest.get('pages', []))

        for j, vp in enumerate(hidden_pages):
            title = vp.title or f'hidden_page_{j}'
            filename = f'{_safe(title)}__{start_idx + j:03d}.md'
            fm_data = {
                'title': title,
                'source_url': vp.published_url,
                'vault_doc_id': vp.doc_id,
                'vault_parent_id': vp.doc_parent_id,
                'vault_shared_drive_id': vp.shared_drive_id,
                'vault_created_at': vp.date_created,
                'vault_modified_at': vp.date_modified,
                'vault_hidden_from_nav': 'true',
                'vault_exported_at': merged_at,
            }
            if vp.collaborators:
                fm_data['vault_collaborators'] = ', '.join(vp.collaborators)

            body_md = (
                pdf_to_markdown(vp.pdf_path)
                if vp.pdf_path
                else '_No PDF content available for this page._'
            )
            try:
                (scrape_dir / filename).write_text(
                    f'{_render_frontmatter(fm_data)}\n\n{body_md}\n', encoding='utf-8'
                )
                manifest['pages'].append({
                    'title': title,
                    'file': filename,
                    'source_url': vp.published_url,
                    'vault_doc_id': vp.doc_id,
                    'vault_hidden_from_nav': True,
                })
                added += 1
                print(f'    [NEW] {title!r}  (hidden-nav, vault only)')
            except OSError as e:
                print(f'    [WARN] {title!r}: {e}')

        manifest['vault_merged_at'] = merged_at
        manifest['vault_pages_enriched'] = enriched
        manifest['vault_hidden_pages_added'] = added
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8'
        )

    return scrape_dir, enriched, added


# ── Top-level entry point ─────────────────────────────────────────────────────

def ingest_vault_export(
    export_dir: Path,
    output_dir: Path,
    site_name: str = '',
) -> Tuple[Path, int]:
    """Parse a Vault export directory and write Markdown output.

    For merging with existing scraper output use merge_with_scrape separately.
    """
    export_dir = Path(export_dir)

    print(f'[VAULT] Scanning: {export_dir}')
    xml_path, csv_path, pdf_paths = find_vault_export_files(export_dir)
    print(f'  Metadata XML : {xml_path or "(not found)"}')
    print(f'  Custodian CSV: {csv_path or "(not found)"}')
    print(f'  PDFs         : {len(pdf_paths)}')

    vault_pages: List[VaultPage] = []

    if xml_path:
        print('[VAULT] Parsing metadata XML...')
        vault_pages = parse_metadata_xml(xml_path)
        print(f'  Parsed {len(vault_pages)} page record(s)')

    if pdf_paths:
        vault_pages = link_pdfs_to_metadata(vault_pages, pdf_paths)
        linked = sum(1 for p in vault_pages if p.pdf_path)
        print(f'  Linked {linked}/{len(vault_pages)} pages to PDFs')

    # If there is no metadata XML, synthesise stubs from PDF filenames
    if not vault_pages and pdf_paths:
        print('  [WARN] No metadata XML — building stubs from PDF filenames')
        for pdf in pdf_paths:
            doc_id = _parse_doc_id_from_filename(pdf.stem)
            vault_pages.append(VaultPage(
                doc_id=doc_id,
                title=pdf.stem,
                date_created='',
                date_modified='',
                published_url='',
                shared_drive_id='',
                doc_parent_id='',
                pdf_path=pdf,
            ))

    if not vault_pages:
        print('[VAULT] No pages found. Nothing to write.')
        return output_dir, 0

    if not site_name:
        site_name = (
            export_dir.name
            .replace('_gsite_0', '')
            .replace('-', ' ')
            .strip()
        ) or 'vault_export'

    print(f'[VAULT] Writing {len(vault_pages)} page(s)...')
    d, n = write_vault_pages(vault_pages, output_dir, site_name)
    return d, n
