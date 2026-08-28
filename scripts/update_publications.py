#!/usr/bin/env python3
"""Fetch peer-reviewed works from ORCID and add any missing ones to index.html.

Stdlib-only (no pip installs required). Run with:
    python scripts/update_publications.py

Writes scripts/new_entries.json with a list of whatever was added (empty list
if nothing new was found). Exits non-zero only on a hard failure (network
error, or index.html not matching the expected structure) so the calling
workflow can tell "nothing new" apart from "something broke".
"""
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

ORCID_ID = "0000-0002-3693-0168"
API_BASE = f"https://pub.orcid.org/v3.0/{ORCID_ID}"
PEER_REVIEWED_TYPES = {"journal-article", "review"}

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "index.html"
NEW_ENTRIES_JSON = Path(__file__).resolve().parent / "new_entries.json"

SECTION_START = "<!-- ============ PUBLICATIONS ============ -->"
SECTION_END = "<!-- ============ TALKS ============ -->"

# Own name, as it should appear bolded in citations (matches existing site style).
OWN_FAMILY_NAME = "boumparis"
OWN_CITATION_FORM = "Boumparis, N."

# Lowercase name particles that stay attached to the family name (e.g. "de Riedmatten").
NAME_PARTICLES = {"van", "der", "de", "von", "del", "la", "le", "da", "dos", "du", "el", "al"}


def http_get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def extract_doi(external_ids):
    ids = (external_ids or {}).get("external-id", []) or []
    dois = [e for e in ids if e.get("external-id-type") == "doi"]
    if not dois:
        return None
    preferred = [e for e in dois if e.get("external-id-relationship") == "self"]
    chosen = preferred[0] if preferred else dois[0]
    return chosen.get("external-id-value", "").strip()


def normalize_doi(doi):
    return doi.strip().strip("/").lower()


def get_existing_dois(section_html):
    found = re.findall(r'doi\.org/([^"<\s]+)', section_html, flags=re.IGNORECASE)
    return {normalize_doi(d) for d in found}


TAG_RE = re.compile(r"<[^>]+>")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Corrections/erratum notices for an already-listed paper — not a new distinct
# publication, so don't auto-add them.
CORRECTION_PREFIXES = ("corrigendum", "erratum", "correction to")


def normalize_text(s):
    s = unicodedata.normalize("NFKD", s).lower()
    return NON_ALNUM_RE.sub("", s)


def get_existing_titles_blob(section_html):
    plain = TAG_RE.sub(" ", section_html)
    return normalize_text(plain)


def is_correction_notice(title):
    return title.strip().lower().startswith(CORRECTION_PREFIXES)


# ORCID's "type" field isn't always reliable (e.g. JMIR preprint-server
# deposits are sometimes typed "journal-article" even though the title says
# otherwise) — belt-and-suspenders check on the title itself.
PREPRINT_MARKERS = ("(preprint)", "[preprint]", "(pre-print)")


def is_preprint_by_title(title):
    lowered = title.strip().lower()
    return any(marker in lowered for marker in PREPRINT_MARKERS)


def split_section(html):
    start = html.find(SECTION_START)
    end = html.find(SECTION_END)
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError(
            "Could not find PUBLICATIONS/TALKS markers in index.html — "
            "aborting without changes so the file isn't corrupted."
        )
    return html[:start], html[start:end], html[end:]


def format_author(credit_name):
    tokens = credit_name.split()
    if not tokens:
        return credit_name
    if len(tokens) == 1:
        return tokens[0]

    i = len(tokens) - 1
    family_tokens = [tokens[i]]
    i -= 1
    while i >= 0 and tokens[i].lower() in NAME_PARTICLES:
        family_tokens.insert(0, tokens[i])
        i -= 1
    family = " ".join(family_tokens)
    given_tokens = tokens[: i + 1]
    initials = " ".join(f"{t[0].upper()}." for t in given_tokens if t)

    if family.lower() == OWN_FAMILY_NAME:
        return OWN_CITATION_FORM
    return f"{family}, {initials}" if initials else family


def format_author_list(credit_names):
    formatted = [format_author(name) for name in credit_names if name]
    formatted = [f"<strong>{a}</strong>" if a == OWN_CITATION_FORM else a for a in formatted]
    if not formatted:
        return "Unknown author"
    if len(formatted) == 1:
        return formatted[0]
    if len(formatted) == 2:
        return f"{formatted[0]} &amp; {formatted[1]}"
    return ", ".join(formatted[:-1]) + f", &amp; {formatted[-1]}"


def build_citation_li(work_summary, contributors, doi):
    title = xml_escape(work_summary["title"]["title"]["value"].strip())
    journal = work_summary.get("journal-title") or {}
    journal_title = xml_escape((journal.get("value") or "").strip())
    year = work_summary["publication-date"]["year"]["value"]

    credit_names = [
        c["credit-name"]["value"]
        for c in contributors
        if c.get("credit-name") and c["credit-name"].get("value")
    ]
    authors = format_author_list(credit_names)

    doi_url = f"https://doi.org/{doi}"
    journal_part = f" <em>{journal_title}</em>." if journal_title else ""
    li = (
        f'          <li>{authors} ({year}). {title}.{journal_part} '
        f'<a href="{doi_url}" target="_blank" rel="noopener">{doi_url}</a></li>\n'
    )
    return li, year


def insert_into_section(section_html, year, li_html):
    year_block_re = re.compile(
        r'(<div class="pub-year">\s*<div class="y">' + re.escape(year) + r'</div>\s*<ul>\n)',
        re.MULTILINE,
    )
    m = year_block_re.search(section_html)
    if m:
        insert_at = m.end(1)
        return section_html[:insert_at] + li_html + section_html[insert_at:]

    # No block for this year yet — create one in descending-year order.
    new_block = (
        f'      <div class="pub-year">\n'
        f'        <div class="y">{year}</div>\n'
        f'        <ul>\n'
        f'{li_html}'
        f'        </ul>\n'
        f'      </div>\n\n'
    )
    year_divs = list(re.finditer(r'<div class="pub-year">\s*<div class="y">(\d+)</div>', section_html))
    target_year = int(year)
    insert_before_pos = None
    for md in year_divs:
        if int(md.group(1)) < target_year:
            insert_before_pos = md.start()
            break
    if insert_before_pos is not None:
        return section_html[:insert_before_pos] + new_block + section_html[insert_before_pos:]
    # Newest year seen so far (or no year blocks at all) — append at the end of the section.
    return section_html.rstrip("\n") + "\n\n" + new_block


def main():
    try:
        works = http_get_json(f"{API_BASE}/works")
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Failed to fetch ORCID works list: {exc}", file=sys.stderr)
        return 1

    candidates = []
    for group in works.get("group", []):
        summaries = group.get("work-summary") or []
        if not summaries:
            continue
        summary = summaries[0]
        if summary.get("type") not in PEER_REVIEWED_TYPES:
            continue
        doi = extract_doi(summary.get("external-ids"))
        if not doi:
            continue
        candidates.append((summary, doi))

    html = INDEX_HTML.read_text(encoding="utf-8")
    before, section, after = split_section(html)
    existing_dois = get_existing_dois(section)
    existing_titles_blob = get_existing_titles_blob(section)

    added = []
    for summary, doi in candidates:
        if normalize_doi(doi) in existing_dois:
            continue
        title = summary["title"]["title"]["value"].strip()
        if is_correction_notice(title) or is_preprint_by_title(title):
            continue
        normalized_title = normalize_text(title)
        if normalized_title and normalized_title in existing_titles_blob:
            continue
        put_code = summary["put-code"]
        try:
            full_work = http_get_json(f"{API_BASE}/work/{put_code}")
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"Failed to fetch work {put_code}: {exc}", file=sys.stderr)
            continue
        contributors = (full_work.get("contributors") or {}).get("contributor", []) or []

        li_html, year = build_citation_li(summary, contributors, doi)
        section = insert_into_section(section, year, li_html)
        existing_dois.add(normalize_doi(doi))
        existing_titles_blob += normalized_title
        added.append(
            {
                "title": summary["title"]["title"]["value"].strip(),
                "journal": (summary.get("journal-title") or {}).get("value", ""),
                "year": year,
                "doi_url": f"https://doi.org/{doi}",
            }
        )

    if added:
        INDEX_HTML.write_text(before + section + after, encoding="utf-8")
        print(f"Added {len(added)} new publication(s).")
    else:
        print("No new peer-reviewed publications found.")

    NEW_ENTRIES_JSON.write_text(json.dumps(added, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
