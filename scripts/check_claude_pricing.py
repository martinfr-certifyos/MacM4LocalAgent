#!/usr/bin/env python3
"""Compare cost/pricing.py against Anthropic's published rates.

Fetches the official pricing docs page, parses the model-pricing
table, and diffs against the local CLAUDE_PRICES dict.

Exit codes:
  0  the local table matches the published rates (or the page
     could not be fetched/parsed -- treated as a no-op so this
     is safe in CI without making CI fail on Anthropic outages)
  1  drift detected -- local table is out of sync
  2  argument / environment error

Use `--strict` to exit 2 on fetch/parse failures (off by default).

Run via `make check-pricing` or directly:
  python3 scripts/check_claude_pricing.py
  python3 scripts/check_claude_pricing.py --quiet      # for cron
  python3 scripts/check_claude_pricing.py --strict     # CI guard

This script does NOT auto-write to cost/pricing.py. Drift is
intentionally a manual update so a one-off scrape failure or
docs-page rewording can never silently corrupt the table.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cost.pricing import CLAUDE_PRICES, PRICING_LAST_UPDATED  # noqa: E402

DOCS_URL = "https://docs.anthropic.com/en/docs/about-claude/pricing"
USER_AGENT = "MacM4LocalAgent-pricing-check/1.0 (+local; informational)"


# ---- HTTP fetch --------------------------------------------------------------

def fetch_pricing_html(url: str = DOCS_URL, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (well-known URL)
        encoding = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(encoding, errors="replace")


# ---- HTML/markdown parsing ---------------------------------------------------

# Anthropic's docs template inlines the table as HTML; older snapshots
# expose it as raw markdown. Handle both.

# Match a markdown row:  | Claude Opus 4.7 | $5 / MTok | ... | $25 / MTok |
_MD_ROW = re.compile(
    r"^\|\s*(Claude [^|]+?)\s*\|"  # model display name (col 1)
    r"\s*\$([\d.]+)\s*/\s*MTok\s*\|"  # base input
    r"\s*\$[\d.]+\s*/\s*MTok\s*\|"    # 5m cache write (skipped)
    r"\s*\$[\d.]+\s*/\s*MTok\s*\|"    # 1h cache write (skipped)
    r"\s*\$[\d.]+\s*/\s*MTok\s*\|"    # cache read (skipped)
    r"\s*\$([\d.]+)\s*/\s*MTok\s*\|", # output
    re.MULTILINE,
)

# Match an HTML <table> ... </table> block, then rows/cells inside it.
# We need table-level granularity because the docs page has multiple
# tables that all have "Claude Opus 4.7" rows (Model Pricing, Batch
# API, Tool Use, Examples). Only the Model Pricing table -- the one
# whose header contains "Base Input Tokens" -- has the standard rates.
_HTML_TABLE = re.compile(r"<table[^>]*>(.*?)</table>", re.DOTALL | re.IGNORECASE)
_HTML_TABLE_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_HTML_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_MONEY = re.compile(r"\$([\d.]+)\s*/\s*MTok")

# Header strings that uniquely identify the standard Model Pricing
# table. We require BOTH "Base Input Tokens" and "Output Tokens" so we
# don't accidentally accept the Batch API table (which has "Batch
# input" / "Batch output" -- half the price).
_PRICING_HEADER_FINGERPRINT = ("base input tokens", "output tokens")


def _display_to_canonical(name: str) -> str:
    """'Claude Opus 4.7 ([deprecated](...))' -> 'claude-opus-4-7'.

    The docs page wraps deprecated models in a markdown link, so we
    have to strip those (and any other parenthesized/bracketed
    annotations) before slugifying. Also strips trailing punctuation
    that can leak in from a partially-matched markdown link.
    """
    n = name.strip()
    # Strip markdown links first: [text](url) -> ""
    n = re.sub(r"\[[^\]]*\]\([^)]*\)", "", n)
    # Then strip any remaining parenthesized/bracketed text.
    n = re.sub(r"\(.*?\)", "", n)
    n = re.sub(r"\[.*?\]", "", n)
    # And catch a stray "(deprecated)" with no link.
    n = re.sub(r"\bdeprecated\b", "", n, flags=re.IGNORECASE)
    n = n.strip().lower()
    # Trim leftover punctuation that can leak from a malformed strip.
    n = n.strip(" -()[]")
    n = n.replace(".", "-")
    n = re.sub(r"\s+", "-", n)
    n = re.sub(r"-+", "-", n)
    return n


def parse_pricing(text: str) -> dict[str, tuple[float, float]]:
    """Return {canonical_id: (input_per_mtok, output_per_mtok)}.

    Tries markdown first (for cached/rendered fixtures), falls back
    to HTML row parsing for live docs.anthropic.com.
    """
    rows: dict[str, tuple[float, float]] = {}

    for m in _MD_ROW.finditer(text):
        display, in_str, out_str = m.group(1), m.group(2), m.group(3)
        canonical = _display_to_canonical(display)
        if canonical.startswith("claude-"):
            rows[canonical] = (float(in_str), float(out_str))
    if rows:
        return rows

    # HTML fallback: pinpoint the Model Pricing table specifically
    # (the one with "Base Input Tokens" + "Output Tokens" headers),
    # then parse its body rows. This is necessary because the docs
    # page has multiple Claude-mentioning tables (Batch API at
    # half-price, Tool Use, etc.) and we must not pull from those.
    for table_html in _HTML_TABLE.findall(text):
        header_text = " ".join(
            _HTML_TAG.sub("", c).strip().lower()
            for c in _HTML_CELL.findall(table_html)[:6]  # first row only
        )
        if not all(h in header_text for h in _PRICING_HEADER_FINGERPRINT):
            continue
        # The Model Pricing table has 6 cells per body row: model
        # name + 5 money columns (input, cache_5m, cache_1h, cache_hit,
        # output). Skip rows that don't match that shape so we never
        # mis-parse a malformed/partial row.
        for tr in _HTML_TABLE_ROW.findall(table_html):
            cells = [_HTML_TAG.sub("", c).strip() for c in _HTML_CELL.findall(tr)]
            if len(cells) < 6 or not cells[0].lower().startswith("claude "):
                continue
            money = _MONEY.findall(tr)
            if len(money) < 5:
                continue
            try:
                in_val = float(money[0])
                out_val = float(money[4])
            except ValueError:
                continue
            canonical = _display_to_canonical(cells[0])
            if canonical.startswith("claude-"):
                rows[canonical] = (in_val, out_val)
        # Stop after the first (and only) Model Pricing table.
        break
    return rows


# ---- diff --------------------------------------------------------------------

def diff_against_local(
    upstream: dict[str, tuple[float, float]],
) -> tuple[list[str], list[str], list[str]]:
    """Return (mismatches, missing_local, missing_upstream).

    Each entry is a single human-readable line.
    """
    mismatches: list[str] = []
    missing_local: list[str] = []
    missing_upstream: list[str] = []

    for canonical, (in_mtok, out_mtok) in upstream.items():
        if canonical not in CLAUDE_PRICES:
            missing_local.append(
                f"  + {canonical}: upstream has it (${in_mtok}/MTok in, ${out_mtok}/MTok out) but cost/pricing.py does not"
            )
            continue
        local = CLAUDE_PRICES[canonical]
        local_in_mtok = local.input * 1_000_000
        local_out_mtok = local.output * 1_000_000
        in_ok = abs(local_in_mtok - in_mtok) < 0.005
        out_ok = abs(local_out_mtok - out_mtok) < 0.005
        if not (in_ok and out_ok):
            mismatches.append(
                f"  ! {canonical}: local=${local_in_mtok:.4g}/{local_out_mtok:.4g} "
                f"upstream=${in_mtok:.4g}/{out_mtok:.4g} (input/output per MTok)"
            )

    for canonical in CLAUDE_PRICES:
        if canonical not in upstream:
            missing_upstream.append(
                f"  - {canonical}: local has it but upstream table does not (deprecated? renamed?)"
            )

    return mismatches, missing_local, missing_upstream


# ---- main --------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", default=DOCS_URL, help="Pricing docs URL (default: %(default)s)")
    p.add_argument("--quiet", action="store_true", help="Suppress 'all good' output")
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 on fetch/parse failures (default: exit 0 to be safe in CI)",
    )
    p.add_argument("--fixture", help="Read from local file instead of network (for tests)")
    args = p.parse_args(argv)

    try:
        if args.fixture:
            text = pathlib.Path(args.fixture).read_text()
        else:
            text = fetch_pricing_html(args.url)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        msg = f"[pricing-check] could not fetch {args.url}: {e}"
        print(msg, file=sys.stderr)
        return 2 if args.strict else 0

    upstream = parse_pricing(text)
    if not upstream:
        msg = (
            f"[pricing-check] could not parse any rows from {args.url}. "
            f"Anthropic may have changed the page format. "
            f"Inspect the page manually."
        )
        print(msg, file=sys.stderr)
        return 2 if args.strict else 0

    mismatches, missing_local, missing_upstream = diff_against_local(upstream)
    drift = bool(mismatches or missing_local)

    if drift:
        print(
            f"[pricing-check] DRIFT detected against {args.url} "
            f"(local table last reconciled {PRICING_LAST_UPDATED}):"
        )
        for line in mismatches:
            print(line)
        for line in missing_local:
            print(line)
        if missing_upstream:
            print()
            print("[pricing-check] local-only entries (informational; "
                  "may be deprecated upstream):")
            for line in missing_upstream:
                print(line)
        print()
        print("To resolve: edit cost/pricing.py to match the upstream rates,")
        print("update PRICING_LAST_UPDATED to today's date, and commit.")
        return 1

    if not args.quiet:
        print(
            f"[pricing-check] OK ({len(upstream)} models, "
            f"local table last reconciled {PRICING_LAST_UPDATED})"
        )
        if missing_upstream:
            print("  (informational) local-only entries:")
            for line in missing_upstream:
                print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
