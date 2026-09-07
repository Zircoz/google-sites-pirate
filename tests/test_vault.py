"""
Unit tests for google_sites_pirate.vault.

Covers three fixes made after running the tool against a real Google Vault
GSites export:
  1. parse_metadata_xml() recognizing the real Vault <Document DocID="…">
     + <Tag TagName="…" TagValue="…"/> schema, alongside the legacy
     <Field name="…">text</Field> and <DocID>text</DocID> encodings.
  2. link_pdfs_to_metadata() matching PDFs to pages by DocID suffix instead
     of a naive underscore-split, which corrupts DocIDs containing
     underscores.
  3. The HTML/CSS noise heuristic catching CSS wrapped across many short
     lines and CSS interspersed with real prose, without stripping prose
     that merely contains a stray brace or colon.

All tests are fully offline and do not require pdfminer.six.

Run with:
    python -m pytest tests/test_vault.py -v
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from google_sites_pirate import vault


# ---------------------------------------------------------------------------
# Bug 1: parse_metadata_xml — real Vault schema + legacy encodings
# ---------------------------------------------------------------------------

REAL_SCHEMA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Root>
  <Document DocID="1abcDEF_23wRqLk">
    <Tags>
      <Tag TagName="#Title" TagValue="Home"/>
      <Tag TagName="#DateCreated" TagValue="2023-12-01T00:00:00Z"/>
      <Tag TagName="#DateModified" TagValue="2024-01-02T03:04:05Z"/>
      <Tag TagName="PublishedURL" TagValue="https://sites.google.com/site/example/home"/>
      <Tag TagName="#Collaborators" TagValue="a@x.com"/>
      <Tag TagName="#Collaborators" TagValue="b@x.com"/>
    </Tags>
  </Document>
  <Document DocID="2ghiJKL_45mNoPq">
    <Tags>
      <Tag TagName="#Title" TagValue="About"/>
      <Tag TagName="PublishedURL" TagValue="https://sites.google.com/site/example/about"/>
    </Tags>
  </Document>
</Root>
"""

LEGACY_FIELD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Root>
  <Item>
    <Field name="#Title">Legacy Page</Field>
    <Field name="DocID">legacyDocId0000000001</Field>
    <Field name="PublishedURL">https://sites.google.com/site/legacy/page</Field>
    <Field name="Collaborators">c@x.com, d@x.com</Field>
  </Item>
</Root>
"""

LEGACY_DOCID_ELEMENT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Root>
  <Record>
    <DocID>elemDocId000000000002</DocID>
    <Title>Element Style Page</Title>
    <PublishedURL>https://sites.google.com/site/legacy/element</PublishedURL>
  </Record>
</Root>
"""


def _write(tmp_dir: Path, name: str, content: str) -> Path:
    p = tmp_dir / name
    p.write_text(content, encoding='utf-8')
    return p


def test_real_vault_schema_parses_full_records():
    with TemporaryDirectory() as td:
        xml_path = _write(Path(td), 'export-metadata.xml', REAL_SCHEMA_XML)
        pages = vault.parse_metadata_xml(xml_path)

    assert len(pages) == 2

    home = next(p for p in pages if p.doc_id == '1abcDEF_23wRqLk')
    assert home.title == 'Home'
    assert home.date_created == '2023-12-01T00:00:00Z'
    assert home.date_modified == '2024-01-02T03:04:05Z'
    assert home.published_url == 'https://sites.google.com/site/example/home'
    assert home.collaborators == ['a@x.com', 'b@x.com']

    about = next(p for p in pages if p.doc_id == '2ghiJKL_45mNoPq')
    assert about.title == 'About'
    assert about.collaborators == []


def test_legacy_field_name_encoding_still_parses():
    with TemporaryDirectory() as td:
        xml_path = _write(Path(td), 'legacy-metadata.xml', LEGACY_FIELD_XML)
        pages = vault.parse_metadata_xml(xml_path)

    assert len(pages) == 1
    page = pages[0]
    assert page.doc_id == 'legacyDocId0000000001'
    assert page.title == 'Legacy Page'
    assert page.published_url == 'https://sites.google.com/site/legacy/page'
    # Pre-existing legacy behavior (unchanged by this fix): a single
    # <Field name="Collaborators"> element is picked up whole by the
    # collaborator-child-element scan before the comma-split fallback runs.
    assert page.collaborators == ['c@x.com, d@x.com']


def test_legacy_docid_element_encoding_still_parses():
    with TemporaryDirectory() as td:
        xml_path = _write(Path(td), 'legacy2-metadata.xml', LEGACY_DOCID_ELEMENT_XML)
        pages = vault.parse_metadata_xml(xml_path)

    assert len(pages) == 1
    page = pages[0]
    assert page.doc_id == 'elemDocId000000000002'
    assert page.title == 'Element Style Page'
    assert page.published_url == 'https://sites.google.com/site/legacy/element'


# ---------------------------------------------------------------------------
# Bug 2: link_pdfs_to_metadata — underscore-containing DocIDs
# ---------------------------------------------------------------------------

def _page(doc_id: str, title: str) -> vault.VaultPage:
    return vault.VaultPage(
        doc_id=doc_id,
        title=title,
        date_created='',
        date_modified='',
        published_url='',
        shared_drive_id='',
        doc_parent_id='',
    )


def test_underscore_containing_doc_ids_link_correctly():
    pages = [
        _page('1abcDEF_23wRqLk', 'Home'),
        _page('2ghiJKL_45mNoPq', 'About'),
    ]
    pdfs = [
        Path('mysite_Home_parent123_1abcDEF_23wRqLk.pdf'),
        Path('mysite_About_parent456_2ghiJKL_45mNoPq.pdf'),
    ]

    result = vault.link_pdfs_to_metadata(pages, pdfs)

    home = next(p for p in result if p.doc_id == '1abcDEF_23wRqLk')
    about = next(p for p in result if p.doc_id == '2ghiJKL_45mNoPq')
    assert home.pdf_path == pdfs[0]
    assert about.pdf_path == pdfs[1]


def test_doc_id_that_is_suffix_of_longer_id_does_not_steal_its_pdf():
    # 'wRqLk' is a suffix of '23wRqLk' is a suffix of '1abcDEF_23wRqLk'.
    # Longest-first matching must ensure the long page gets the long-ID PDF
    # and the short page does NOT accidentally also match against it.
    pages = [
        _page('1abcDEF_23wRqLk', 'Home'),
        _page('wRqLk', 'Short'),
    ]
    pdfs = [
        Path('site_Home_parent_1abcDEF_23wRqLk.pdf'),
    ]

    result = vault.link_pdfs_to_metadata(pages, pdfs)

    home = next(p for p in result if p.doc_id == '1abcDEF_23wRqLk')
    short = next(p for p in result if p.doc_id == 'wRqLk')
    assert home.pdf_path == pdfs[0]
    assert short.pdf_path is None


def test_extra_unmatched_pdf_does_not_clobber_already_linked_page():
    pages = [
        _page('1abcDEF_23wRqLk', 'Home'),
    ]
    pdfs = [
        Path('site_Home_parent_1abcDEF_23wRqLk.pdf'),
        Path('site_totally_unrelated_stray_file.pdf'),
    ]

    result = vault.link_pdfs_to_metadata(pages, pdfs)

    home = result[0]
    assert home.pdf_path == pdfs[0]


def test_order_fallback_only_pairs_still_unmatched_after_suffix_matching():
    pages = [
        _page('1abcDEF_23wRqLk', 'Home'),
        _page('2ghiJKL_45mNoPq', 'About'),
    ]
    pdfs = [
        Path('site_Home_parent_1abcDEF_23wRqLk.pdf'),
        Path('site_unrecognizable_filename.pdf'),
    ]

    result = vault.link_pdfs_to_metadata(pages, pdfs)

    home = next(p for p in result if p.doc_id == '1abcDEF_23wRqLk')
    about = next(p for p in result if p.doc_id == '2ghiJKL_45mNoPq')
    # Home matched by suffix; the one remaining unmatched page/pdf pair up
    # via the order-based fallback.
    assert home.pdf_path == pdfs[0]
    assert about.pdf_path == pdfs[1]


# ---------------------------------------------------------------------------
# Bug 3: HTML/CSS noise stripping
# ---------------------------------------------------------------------------

def test_css_wrapped_across_many_short_lines_is_stripped():
    text = (
        "Welcome to our site.\n\n"
        ".header\n{\ncolor:\n#ffffff;\nbackground-color:\n#000000;\npadding:\n10px;\n}\n\n"
        "Thanks for visiting."
    )
    cleaned = vault.strip_html_css_noise(text)
    assert 'background-color' not in cleaned
    assert 'Welcome to our site.' in cleaned
    assert 'Thanks for visiting.' in cleaned


def test_css_interspersed_with_prose_leaves_prose_intact():
    text = (
        "Welcome to our page. .header { background-color: #fff; padding: 10px; } "
        "We hope you enjoy your stay and learn more about us."
    )
    cleaned = vault.strip_html_css_noise(text)
    assert 'background-color' not in cleaned
    assert 'padding' not in cleaned
    assert 'Welcome to our page.' in cleaned
    assert 'We hope you enjoy your stay and learn more about us.' in cleaned


def test_prose_with_stray_brace_or_colon_is_not_stripped():
    text = (
        "Here is a quote: \"life is what happens { while you make other plans }\". "
        "It has punctuation but it is not CSS."
    )
    cleaned = vault.strip_html_css_noise(text)
    assert 'life is what happens' in cleaned
    assert 'not CSS' in cleaned


def test_style_and_script_blocks_are_excised():
    text = (
        "Intro text.\n\n"
        "<style>.foo { color: red; font-size: 12px; }</style>"
        "<script>console.log('hello');</script>\n\n"
        "Outro text."
    )
    cleaned = vault.strip_html_css_noise(text)
    assert 'color: red' not in cleaned
    assert 'console.log' not in cleaned
    assert 'Intro text.' in cleaned
    assert 'Outro text.' in cleaned


def test_dense_minified_css_paragraph_still_flagged_as_noise():
    # Existing density heuristic: a long, densely-packed CSS paragraph on a
    # single line should still be caught outright.
    css_blob = '.a{color:#fff;padding:0}' * 20
    assert vault._is_html_css_noise(css_blob) is True


# ── Regression tests for review findings ─────────────────────────────────────

def test_single_declaration_css_rules_are_stripped():
    """`.foo { color: #hex; }` — one declaration per rule — is real widget CSS."""
    text = (
        'Welcome to our site.\n\n'
        '.some-class { color: #ff0000; } .other-class { background-color: #fff; }\n\n'
        'Real prose here.'
    )
    out = vault._excise_css_blocks(text)
    assert 'some-class' not in out
    assert 'background-color' not in out
    assert 'Welcome to our site.' in out
    assert 'Real prose here.' in out


def test_media_query_wrapper_is_not_left_behind():
    """Excising an @media block's inner rules must not leave the wrapper."""
    text = '@media screen and (max-width: 600px) {\n .a { font-size: 12px; color: red; }\n}\nProse'
    out = vault._excise_css_blocks(text)
    assert '@media' not in out
    assert 'font-size' not in out
    assert 'Prose' in out


def test_claimed_doc_id_does_not_fall_through_to_shorter_suffix_id():
    """A second PDF for an already-linked DocID must not attach to a page whose
    DocID is merely a suffix of it."""
    long_id = 'AAAAAAAAAAAAAAAAAAAAAAA_zzzYYY'
    short_id = long_id[-24:]  # a genuine suffix of long_id
    pages = [
        vault.VaultPage(long_id, 'Long', '', '', '', '', '', []),
        vault.VaultPage(short_id, 'Short', '', '', '', '', '', []),
    ]
    pdfs = [Path(f's_p1_{long_id}.pdf'), Path(f's_p2_{long_id}.pdf')]
    vault.link_pdfs_to_metadata(pages, pdfs)
    assert pages[0].pdf_path == pdfs[0]
    assert pages[1].pdf_path is None, 'suffix page must not receive the longer ID\'s PDF'
