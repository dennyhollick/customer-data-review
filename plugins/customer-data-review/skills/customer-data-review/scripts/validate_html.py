"""Structural and accessibility validation of generated HTML reports.

Uses stdlib only (html.parser.HTMLParser + regex). No BeautifulSoup/lxml.

Usage: python -m v2.scripts.validate_html report.html
"""

import argparse
import re
import sys
from html.parser import HTMLParser


class _IdCollector(HTMLParser):
    """Collect all id attributes from HTML."""

    def __init__(self):
        super().__init__()
        self.ids = set()
        self.tags = []
        self.in_svg = False
        self.svg_has_title = False
        self.svg_has_aria = False
        self.svgs = []  # list of (has_title, has_aria)

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if "id" in attrs_dict:
            self.ids.add(attrs_dict["id"])
        self.tags.append(tag)

        if tag == "svg":
            self.in_svg = True
            self.svg_has_title = False
            self.svg_has_aria = "aria-label" in attrs_dict

        if tag == "title" and self.in_svg:
            self.svg_has_title = True

    def handle_endtag(self, tag):
        if tag == "svg" and self.in_svg:
            self.svgs.append((self.svg_has_title, self.svg_has_aria))
            self.in_svg = False


def _check_sections_present(html_str):
    """Check required sections exist."""
    issues = []
    required = [
        ("executive-summary", "Executive Summary"),
        ("methodology", "Methodology"),
    ]
    for section_id, label in required:
        if f'id="{section_id}"' not in html_str:
            issues.append({
                "severity": "error",
                "check": "sections_present",
                "message": f"Missing required section: {label} (id={section_id})",
                "context": "HTML structure",
            })

    # Check question sections (Q1, Q2, etc.)
    question_ids = re.findall(r'id="(Q\d+)"', html_str)
    if not question_ids:
        issues.append({
            "severity": "warning",
            "check": "sections_present",
            "message": "No question sections found (expected id=Q1, Q2, ...)",
            "context": "HTML structure",
        })

    return issues


def _check_charts_present(html_str):
    """Check chart elements exist for question sections."""
    issues = []
    question_ids = re.findall(r'id="(Q\d+)"', html_str)

    if question_ids and "chart-container" not in html_str:
        issues.append({
            "severity": "warning",
            "check": "charts_present",
            "message": "No chart containers found (expected class=chart-container)",
            "context": "HTML structure",
        })

    return issues


def _check_nav_links(html_str):
    """Check sidebar href='#id' links match existing ids."""
    issues = []

    # Collect all ids
    parser = _IdCollector()
    parser.feed(html_str)

    # Find all nav href="#..." links
    nav_hrefs = re.findall(r'href="#([^"]+)"', html_str)
    for href_id in nav_hrefs:
        if href_id not in parser.ids:
            issues.append({
                "severity": "error",
                "check": "nav_links",
                "message": f"Nav link #{href_id} has no matching id in document",
                "context": "sidebar navigation",
            })

    return issues


def _check_accessibility(html_str):
    """Check accessibility features."""
    issues = []

    # Skip link
    if "skip-link" not in html_str:
        issues.append({
            "severity": "error",
            "check": "accessibility",
            "message": "Missing skip link",
            "context": "a11y",
        })

    # aria-label on interactive elements
    if 'aria-label=' not in html_str:
        issues.append({
            "severity": "warning",
            "check": "accessibility",
            "message": "No aria-label attributes found on interactive elements",
            "context": "a11y",
        })

    # Focus styles in CSS
    if "outline:" not in html_str or ":focus" not in html_str:
        issues.append({
            "severity": "warning",
            "check": "accessibility",
            "message": "No focus outline styles found in CSS",
            "context": "a11y",
        })

    return issues


def _check_no_markdown_artifacts(html_str):
    """Check for leftover markdown artifacts."""
    issues = []
    artifacts = [
        (r"^### ", "markdown H3 heading"),
        (r"^---\s*$", "markdown horizontal rule"),
        (r"\[\[", "double bracket reference"),
        (r"\*\*[^*]+\*\*", "unconverted bold markdown"),
    ]
    for pattern, label in artifacts:
        if re.search(pattern, html_str, re.MULTILINE):
            issues.append({
                "severity": "error",
                "check": "markdown_artifacts",
                "message": f"Found {label} in HTML output",
                "context": "content cleanup",
            })

    return issues


def _check_html_closes(html_str):
    """Check HTML document ends properly."""
    issues = []
    stripped = html_str.strip()
    if not stripped.endswith("</html>"):
        issues.append({
            "severity": "error",
            "check": "html_closes",
            "message": "HTML document does not end with </html> — may be truncated",
            "context": "document structure",
        })

    return issues


def _check_design_constraints(html_str):
    """Check design constraints: no box-shadow, no gradients."""
    issues = []
    forbidden = [
        ("box-shadow", "box-shadow"),
        ("linear-gradient", "gradient"),
        ("radial-gradient", "gradient"),
    ]
    for pattern, label in forbidden:
        if pattern in html_str:
            issues.append({
                "severity": "warning",
                "check": "design_constraints",
                "message": f"Found {label} in HTML — violates design constraints",
                "context": "CSS",
            })

    # Check required color variables
    required_colors = {
        "--primary": "primary color",
    }
    for css_var, label in required_colors.items():
        if css_var not in html_str:
            issues.append({
                "severity": "warning",
                "check": "design_constraints",
                "message": f"Missing {label} ({css_var})",
                "context": "CSS variables",
            })

    return issues


def validate_html(html_str):
    """Run all HTML validation checks.

    Args:
        html_str: Complete HTML string to validate.

    Returns:
        list of Issue dicts.
    """
    issues = []
    issues.extend(_check_sections_present(html_str))
    issues.extend(_check_charts_present(html_str))
    issues.extend(_check_nav_links(html_str))
    issues.extend(_check_accessibility(html_str))
    issues.extend(_check_no_markdown_artifacts(html_str))
    issues.extend(_check_html_closes(html_str))
    issues.extend(_check_design_constraints(html_str))
    return issues


def main():
    parser = argparse.ArgumentParser(description="Validate HTML report")
    parser.add_argument("html_file", help="Path to HTML file")
    args = parser.parse_args()

    with open(args.html_file, encoding="utf-8") as f:
        html_str = f.read()

    issues = validate_html(html_str)
    for issue in issues:
        print(f"[{issue['severity'].upper()}] {issue['message']}")
    sys.exit(1 if any(i["severity"] == "error" for i in issues) else 0)


if __name__ == "__main__":
    main()
