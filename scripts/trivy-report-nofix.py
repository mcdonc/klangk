#!/usr/bin/env python3
"""Render a Trivy image-scan JSON result as a focused markdown report.

The report splits HIGH/CRITICAL findings into two buckets:

* **fix available** — a fixed version exists, so an ordinary package/node
  upgrade resolves the vulnerability (these are actionable now).
* **no upstream fix yet** — the vulnerability has no fixed Debian/Node
  package available yet (Trivy statuses ``affected``, ``fix_deferred``,
  ``will_not_fix`` or ``end_of_life``, i.e. no ``FixedVersion``). These are
  tracked in the security backlog (issue #570) and re-scanned periodically.

Designed to be piped into ``$GITHUB_STEP_SUMMARY`` by the periodic scan
workflow, but it is also usable locally::

    trivy-workspace --severity CRITICAL,HIGH --format json \
        | python3 scripts/trivy-report-nofix.py

Only markdown is written to stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import json
import os
import sys

# Vulnerability is considered to have NO upstream fix when Trivy reports one
# of these statuses (equivalently: no FixedVersion present). Anything else
# (status == "fixed" / a populated FixedVersion) is upgrade-resolvable.
NO_FIX_STATUSES = {"affected", "fix_deferred", "will_not_fix", "end_of_life"}
TRACKING_ISSUE = "https://github.com/mcdonc/klangk/issues/570"
IMAGE = os.environ.get("KLANGK_IMAGE_NAME", "klangk-workspace") + ":latest"
SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}


def _short(text: str | None, limit: int = 90) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _md_escape(text: str) -> str:
    # Keep table cells safe without over-escaping readable text.
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def load_results(path: str | None) -> list[dict]:
    raw = sys.stdin.read() if not path or path == "-" else open(path).read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"trivy-report-nofix: invalid JSON: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print("trivy-report-nofix: unexpected JSON shape", file=sys.stderr)
        sys.exit(2)
    return data.get("Results", []) or []


def collect(results: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (no_fix_vulns, fixable_vulns) restricted to HIGH/CRITICAL."""
    no_fix: list[dict] = []
    fixable: list[dict] = []
    for res in results:
        for v in res.get("Vulnerabilities") or []:
            if v.get("Severity") not in ("CRITICAL", "HIGH"):
                continue
            if v.get("FixedVersion"):
                fixable.append(v)
            elif v.get("Status") in NO_FIX_STATUSES:
                no_fix.append(v)
            else:
                # Severity HIGH/CRITICAL with neither a fixed version nor a
                # known no-fix status — treat as no-fix (can't act on it).
                no_fix.append(v)
    return no_fix, fixable


def group_by_cve(vulns: list[dict]) -> list[dict]:
    """Collapse a list of vulns into one entry per CVE.

    ``packages`` maps package name -> (installed_version, fixed_version).
    """
    by_cve: dict[str, dict] = {}
    for v in vulns:
        cve = v.get("VulnerabilityID") or "UNKNOWN"
        entry = by_cve.setdefault(
            cve,
            {
                "cve": cve,
                "severity": v.get("Severity", "UNKNOWN"),
                "status": v.get("Status", "unknown"),
                "title": v.get("Title") or v.get("Description", ""),
                "packages": {},  # pkg -> (installed, fixed)
            },
        )
        # Keep the most severe severity observed.
        if SEVERITY_RANK.get(v.get("Severity"), 99) < SEVERITY_RANK.get(
            entry["severity"], 99
        ):
            entry["severity"] = v.get("Severity")
        pkg = v.get("PkgName") or "?"
        entry["packages"].setdefault(
            pkg, (v.get("InstalledVersion") or "", v.get("FixedVersion") or "")
        )
    # Sort: CRITICAL first, then HIGH, then alphabetical.
    return sorted(
        by_cve.values(),
        key=lambda e: (SEVERITY_RANK.get(e["severity"], 99), e["cve"]),
    )


def render(no_fix: list[dict], fixable: list[dict]) -> str:
    no_fix_cves = group_by_cve(no_fix)
    fixable_cves = group_by_cve(fixable)
    total = len(no_fix) + len(fixable)

    lines: list[str] = []
    lines.append("## 🛡️ Trivy workspace scan — no-fix CVE report")
    lines.append("")
    lines.append(
        f"Image: `{IMAGE}` · Severity: **HIGH, CRITICAL** · "
        f"Source: Debian + Node package metadata"
    )
    lines.append("")
    lines.append(f"- **{total}** HIGH/CRITICAL findings")
    lines.append(
        f"- **{len(fixable)}** with an available fix "
        f"(a package/Node upgrade resolves them)"
    )
    lines.append(
        f"- **{len(no_fix)}** ({len(no_fix_cves)} distinct CVEs) with "
        f"**no upstream fix yet** — tracked in "
        f"[#570]({TRACKING_ISSUE})"
    )
    lines.append("")
    lines.append(
        "> This scan is **informational** — it never fails the workflow. "
        "Re-runs weekly (Mon 06:00 UTC) or on demand via `workflow_dispatch`."
    )
    lines.append("")

    if fixable_cves:
        lines.append(
            f"### 🔧 Fix available — resolve by upgrading ({len(fixable_cves)} CVEs)"
        )
        lines.append("")
        lines.append("| CVE | Severity | Package | Installed | Fixed |")
        lines.append("| --- | --- | --- | --- | --- |")
        for e in fixable_cves:
            for pkg, (inst, fixed) in sorted(e["packages"].items()):
                lines.append(
                    f"| {e['cve']} | {e['severity']} | "
                    f"{_md_escape(pkg)} | {_md_escape(inst)} | "
                    f"{_md_escape(fixed)} |"
                )
        lines.append("")

    lines.append(
        f"### ⏳ No upstream fix yet ({len(no_fix_cves)} CVEs, {len(no_fix)} findings)"
    )
    lines.append("")
    if not no_fix_cves:
        lines.append("_None — all HIGH/CRITICAL findings have an available fix. 🎉_")
        lines.append("")
    else:
        lines.append(
            "Grouped by CVE. Re-scanned periodically; revisit when Debian "
            f"publishes fixed packages. See [#570]({TRACKING_ISSUE})."
        )
        lines.append("")
        lines.append("| CVE | Severity | Status | Packages | Title |")
        lines.append("| --- | --- | --- | --- | --- |")
        for e in no_fix_cves:
            pkgs = ", ".join(sorted(e["packages"]))
            if len(pkgs) > 60:
                pkgs = pkgs[:59] + "…"
            lines.append(
                f"| {e['cve']} | {e['severity']} | `{e['status']}` | "
                f"{_md_escape(pkgs)} | {_md_escape(_short(e['title']))} |"
            )
        lines.append("")

    lines.append("<details><summary>Status legend</summary>")
    lines.append("")
    lines.append("- `affected` — no fixed package available yet (fix expected).")
    lines.append("- `fix_deferred` — Debian has explicitly deferred the fix.")
    lines.append("- `will_not_fix` / `end_of_life` — no fix planned.")
    lines.append("- A populated **Fixed** column means an upgrade resolves it.")
    lines.append("")
    lines.append("</details>")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else None
    if path in ("-h", "--help"):
        print(__doc__)
        return 0
    results = load_results(path)
    no_fix, fixable = collect(results)
    print(render(no_fix, fixable))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
