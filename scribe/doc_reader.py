import argparse
import re
import pymupdf  # PyMuPDF


# -----------------------------
# Regex constants / helpers
# -----------------------------
CO_TAIL_RE = re.compile(r"\b(?:SDN BHD|BHD|BERHAD)\b", re.IGNORECASE)
CO_NO_RE = re.compile(r"\(Co\. ?No\. ?: ?([A-Z0-9\-]+)\)", re.IGNORECASE)

REJECT_BOLD_PREFIXES = (
    "directory of", "business activities", "email", "web site",
    "tel.no", "fax.no", "authorised capital", "paid up capital",
    "incorporation date", "no of employees", "management"
)

# Phones / emails
TEL_LABEL_RE = re.compile(r"Tel\.?\s*No\.?\s*:\s*([^\n\r]+)", re.IGNORECASE)
PHONE_RE = re.compile(r"\b(?:\+?6?0)?\d{2,3}-?\d{5,8}\b")
EMAIL_BREAK_RE = re.compile(r"""
([A-Za-z0-9._%+-]+)         # local
(?:\s+|\n)?@(?:\s+|\n)?      # @ (maybe broken)
([A-Za-z0-9.-]+)             # domain
(?:\s+|\n)?\.(?:\s+|\n)?     # dot (maybe broken)
([A-Za-z]{2,})               # TLD
(?:\.(?:\s+|\n)?([A-Za-z]{2,}))?  # optional extra TLD part
""", re.VERBOSE)


def dedup_preserve_order(items):
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# -----------------------------
# Text extraction utilities
# -----------------------------
def is_bold_span(span: dict) -> bool:
    font = span.get("font", "").lower()
    weight = span.get("weight", 0)
    return ("bold" in font) or (weight and weight >= 700)


def get_flat_spans_with_lines(page):
    spans = []
    lines = []
    d = page.get_text("dict")
    line_no = -1
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for l in b.get("lines", []):
            line_no += 1
            acc_line = []
            for s in l.get("spans", []):
                t = re.sub(r"\s+", " ", s.get("text", "")).strip()
                if not t:
                    continue
                spans.append({
                    "text": t,
                    "is_bold": is_bold_span(s),
                    "line_no": line_no,
                })
                acc_line.append(t)
            lines.append(" ".join(acc_line).strip())
    return spans, lines


def extract_headers_from_bold(spans):
    results, buf, last_idx = [], [], None

    def flush_if_company():
        nonlocal buf, last_idx, results
        if not buf:
            return
        joined = " ".join(buf).strip()
        if joined.lower().startswith(REJECT_BOLD_PREFIXES):
            buf.clear()
            last_idx = None
            return
        if CO_TAIL_RE.search(joined) or "(Co. No." in joined:
            clean = CO_NO_RE.sub("", joined).strip()
            clean = re.sub(r"\s{2,}", " ", clean)
            if clean and len(clean) <= 120:
                results.append((clean, last_idx))
        buf.clear()
        last_idx = None

    for i, sp in enumerate(spans):
        txt = sp["text"]
        if sp["is_bold"]:
            if txt.lower().startswith(REJECT_BOLD_PREFIXES):
                flush_if_company()
                continue
            buf.append(txt)
            last_idx = i
            if CO_TAIL_RE.search(" ".join(buf)) or "(Co. No." in txt:
                flush_if_company()
        else:
            flush_if_company()

    flush_if_company()
    out, seen = [], set()
    for name, idx in results:
        if name not in seen:
            seen.add(name)
            out.append((name, idx))
    return out


def slice_company_blocks(headers, spans, lines):
    out = []
    for i, (name, end_idx) in enumerate(headers):
        start_line = spans[end_idx]["line_no"] + 1 if end_idx is not None else 0
        stop_line = (
            spans[headers[i + 1][1]]["line_no"] - 1
            if i + 1 < len(headers)
            else len(lines) - 1
        )
        if stop_line < start_line:
            stop_line = start_line
        block_lines = [ln for ln in lines[start_line:stop_line + 1] if ln]
        block_text = "\n".join(block_lines)
        out.append((name, block_text))
    return out


# -----------------------------
# Parsers
# -----------------------------
def normalize_emails(text: str):
    emails = []
    for m in EMAIL_BREAK_RE.finditer(text):
        local, dom, tld1, tld2 = m.groups()
        email = f"{local}@{dom}.{tld1}"
        if tld2:
            email += f".{tld2}"
        emails.append(email.lower())
    return dedup_preserve_order(emails)


def extract_contacts_from_block(block_text: str):
    phones = []
    for m in TEL_LABEL_RE.finditer(block_text):
        tail = m.group(1)
        for p in PHONE_RE.findall(tail):
            phones.append(p)
    phones = dedup_preserve_order(phones)
    emails = normalize_emails(block_text)
    return phones, emails


def extract_management_stub(*args, **kwargs):
    """
    Placeholder for future NLP-based role/name extraction.
    Currently returns [] to avoid dirty data.
    """
    return []


# -----------------------------
# CLI / main
# -----------------------------
def main():
    parser = argparse.ArgumentParser(description="Extract company info (V3 clean version).")
    parser.add_argument("pdf_path", type=str, help="Path to PDF")
    parser.add_argument("--pages", nargs="+", type=int, help="Pages to parse (0-based)")
    args = parser.parse_args()

    doc = pymupdf.open(args.pdf_path)
    print(f"Loaded {args.pdf_path} with {doc.page_count} pages.")

    pages_to_check = args.pages if args.pages else [3]
    for p in pages_to_check:
        if p < 0 or p >= doc.page_count:
            print(f"⚠️ Skipping page {p}")
            continue

        page = doc.load_page(p)
        spans, lines = get_flat_spans_with_lines(page)
        headers = extract_headers_from_bold(spans)
        blocks = slice_company_blocks(headers, spans, lines)

        records = []
        for name, block_text in blocks:
            phones, emails = extract_contacts_from_block(block_text)
            mgmt = extract_management_stub()
            records.append({
                "company": name,
                "emails": emails,
                "phones": phones,
                "management": mgmt,
            })

        print(f"\n--- Page {p} ---")
        print("Companies:", [{"company": r["company"]} for r in records])
        print("Emails:", dedup_preserve_order([e for r in records for e in r["emails"]]))
        print("Phones:", dedup_preserve_order([ph for r in records for ph in r["phones"]]))
        print("Management:", dedup_preserve_order([m for r in records for m in r["management"]]))


if __name__ == "__main__":
    main()
