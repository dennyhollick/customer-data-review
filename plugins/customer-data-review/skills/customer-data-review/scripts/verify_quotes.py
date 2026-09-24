"""Verify that each quote in a mentions object actually appears in the source text."""

import argparse
import json
import re
import sys


_SMART_QUOTES = str.maketrans({
    "\u2018": "'", "\u2019": "'",   # single curly quotes
    "\u201c": '"', "\u201d": '"',   # double curly quotes
    "\u2013": "-", "\u2014": "-",   # en-dash, em-dash
    "\u2026": "...",                 # ellipsis
})


def normalize_text(text: str) -> str:
    """Normalize text for quote matching.

    Collapses whitespace, strips markdown escape backslashes,
    normalizes smart quotes/dashes, and lowercases.
    """
    # Strip markdown escape backslashes (e.g., \$400K → $400K)
    result = re.sub(r"\\([^\\])", r"\1", text)
    # Normalize smart quotes and dashes
    result = result.translate(_SMART_QUOTES)
    # Collapse whitespace
    result = re.sub(r"\s+", " ", result).strip().lower()
    return result


def verify_quotes(mention_obj: dict, source_text: str) -> list[dict]:
    """Check each quote against the source text.

    Args:
        mention_obj: Parsed mentions.jsonl line with file_id and mentions.
        source_text: The full text content of the source file.

    Returns:
        List of dicts with mention_id, quote_snippet, and found (bool).
    """
    normalized_source = normalize_text(source_text)
    results = []

    for mention in mention_obj.get("mentions", []):
        quote = mention.get("quote", "")
        normalized_quote = normalize_text(quote)

        found = bool(normalized_quote and normalized_quote in normalized_source)

        # quote_snippet: first 80 chars of the quote for readability
        snippet = quote[:80] + ("..." if len(quote) > 80 else "")

        results.append({
            "mention_id": mention.get("mention_id", ""),
            "quote_snippet": snippet,
            "found": found,
        })

    return results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Verify quotes against source text."
    )
    parser.add_argument("mentions_json", help="Path to mentions JSON file")
    parser.add_argument("source_file", help="Path to the source text file")
    args = parser.parse_args(argv)

    with open(args.mentions_json, encoding="utf-8") as f:
        mention_obj = json.load(f)
    with open(args.source_file, encoding="utf-8") as f:
        source_text = f.read()

    results = verify_quotes(mention_obj, source_text)

    print(json.dumps(results, indent=2))

    # Exit with error if any quotes not found
    if any(not r["found"] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
