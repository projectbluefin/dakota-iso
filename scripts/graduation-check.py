#!/usr/bin/env python3
"""Measure the post-boot assertions in scripts/verify-post-install.sh against a
measured graduation bar and recommend graduating the checks that qualify
(dakota-iso#381).

Each soft assertion in verify-post-install.sh prints a TAP line
(`ok - <id>` / `not ok - <id>`). This tool collects those lines from the last
N scheduled E2E runs on `main`, applies the graduation bar from
.github/post-boot-criteria.yml, and opens/refreshes an issue RECOMMENDING which
checks have earned the right to block by being added to STRICT_IDS. It only
recommends; a human enforces.

Usage:
  python3 scripts/graduation-check.py [--dry-run] [--json] [--repo OWNER/REPO]
  python3 scripts/graduation-check.py --runs FILE [--json]

`--runs FILE` loads run history from a fixture (no network) for testing: a
JSON object mapping each check id to a chronological list of its TAP status
(`"ok"` / `"not ok"`), oldest first.

Requires `gh` authenticated with `repo` (issues + contents) scope. In GitHub
Actions GITHUB_TOKEN / GITHUB_REPOSITORY are picked up automatically.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CRITERIA_PATH = REPO_ROOT / ".github" / "post-boot-criteria.yml"
E2E_WORKFLOWS = ("test-plain-install.yml", "test-luks-install.yml")
TAP_RE = re.compile(r"^(?P<status>ok|not ok) - (?P<id>\S+)\s*$", re.MULTILINE)


def load_criteria(path: Path = CRITERIA_PATH) -> dict:
    return yaml.safe_load(path.read_text())


def run_gh(args: list[str], repo: str, *, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    cmd = ["gh", "--repo", repo, *args]
    return subprocess.run(cmd, input=input_bytes, capture_output=True)


def list_runs(repo: str, *, limit: int = 100) -> list[dict]:
    proc = run_gh(
        ["run", "list", "--branch", "main", "--json",
         "number,createdAt,conclusion,path", "--limit", str(limit)],
        repo,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh run list failed: {proc.stderr.decode(errors='replace')}")
    runs = json.loads(proc.stdout or "[]")
    return [r for r in runs if any(r.get("path", "").endswith(w) for w in E2E_WORKFLOWS)]


def fetch_run_log(repo: str, run_number: int) -> str | None:
    """Return the combined run log text, or None if it cannot be fetched."""
    proc = run_gh([f"runs/{run_number}/logs"], repo)
    if proc.returncode != 0 or not proc.stdout:
        return None
    data = proc.stdout
    if data.startswith(b"\x1f\x8b"):  # gzip
        try:
            data = gzip.decompress(data)
        except gzip.BadGzipFile:
            return None
    return data.decode("utf-8", "replace")


def parse_tap(log: str | None) -> dict[str, str]:
    if not log:
        return {}
    # Last occurrence per id wins: a re-run of the step in one run keeps the
    # most recent verdict.
    return {m.group("id"): m.group("status") for m in TAP_RE.finditer(log)}


def gather(checks: list[str], runs: list[dict], repo: str) -> dict[str, list[tuple[str, str]]]:
    results: dict[str, list[tuple[str, str]]] = {c: [] for c in checks}
    for run in runs:
        tap = parse_tap(fetch_run_log(repo, run["number"]))
        for cid in checks:
            if cid in tap:
                results[cid].append((run["createdAt"], tap[cid]))
    for cid in results:
        results[cid].sort(key=lambda item: item[0])
    return results


def gather_offline(checks: list[str], fixture: dict) -> dict[str, list[tuple[str, str]]]:
    """Offline gather from a {check_id: ["ok", "not ok", ...]} fixture.

    The statuses are chronological (oldest first) so the trailing-consecutive
    streak in evaluate() reads them newest-last, matching the live path.
    """
    results: dict[str, list[tuple[str, str]]] = {}
    for cid in checks:
        results[cid] = [(None, s) for s in fixture.get(cid, [])]
    return results


@dataclass
class Bar:
    required_consecutive_passes: int = 3
    maximum_failure_rate: float = 0.10
    lookback_runs: int = 20

    @classmethod
    def from_config(cls, cfg: dict) -> "Bar":
        g = cfg.get("graduation", {})
        return cls(
            required_consecutive_passes=int(g.get("required_consecutive_passes", 3)),
            maximum_failure_rate=float(g.get("maximum_failure_rate", 0.10)),
            lookback_runs=int(g.get("lookback_runs", 20)),
        )


def evaluate(seq: list[str], bar: Bar) -> tuple[bool, int, int, str]:
    """Return (qualified, failures, total, detail) for one check's history."""
    window = seq[-bar.lookback_runs:] if len(seq) > bar.lookback_runs else seq
    total = len(window)
    if total == 0:
        return False, 0, 0, "no E2E run exercised this check"
    failures = sum(1 for s in window if s != "ok")
    consecutive = 0
    for s in reversed(window):
        if s == "ok":
            consecutive += 1
        else:
            break
    rate = failures / total
    reasons: list[str] = []
    if consecutive < bar.required_consecutive_passes:
        reasons.append(
            f"{consecutive} consecutive passes "
            f"(need {bar.required_consecutive_passes})"
        )
    if rate > bar.maximum_failure_rate:
        reasons.append(f"{rate:.0%} failures (max {bar.maximum_failure_rate:.0%})")
    qualified = not reasons
    detail = f"{failures}/{total} failures" if qualified else "; ".join(reasons)
    return qualified, failures, total, detail


def build_body(qualified: list[tuple[str, int, int, str]], bar: Bar) -> str:
    checks = "\n".join(
        f"- `{cid}` — {detail} over the last {total} runs that exercised it; "
        f"meets the graduation bar."
        for cid, failures, total, detail in qualified
    )
    return f"""## Summary

The following post-boot assertion(s) in `scripts/verify-post-install.sh` have
passed a measured green bar over the last {bar.lookback_runs} scheduled E2E
runs on `main` and are ready to graduate from observability-only to strict
(add their id to `STRICT_IDS`). A strict regression then fails the E2E lane
instead of only warning.

---

{checks}

### How to graduate

1. Add the id(s) above to `STRICT_IDS` in the `luks-verify-qemu` and
   `plain-verify-qemu` justfile recipes (or export it in the E2E workflows).
2. Open a PR; CI now fails a regression in that check.
3. Close this issue once merged.

This recommendation is automated by scripts/graduation-check.py
(.github/workflows/graduation-check.yml). Enforcement stays a human decision.
"""


def find_existing_issue(repo: str, title: str) -> int | None:
    proc = run_gh(
        ["issue", "list", "--state", "all", "--json", "number,title",
         "--search", f"In:title:{title!r}"],
        repo,
    )
    if proc.returncode != 0:
        return None
    for item in json.loads(proc.stdout or "[]"):
        if item.get("title") == title:
            return int(item["number"])
    return None


def upsert_issue(repo: str, title: str, body: str, dry_run: bool) -> int | None:
    if dry_run:
        print(f"[dry-run] would open/refresh issue: {title}")
        print(body)
        return None
    number = find_existing_issue(repo, title)
    args = ["issue", "update", str(number), "--repo", repo, "--title", title]
    if number is None:
        args = ["issue", "create", "--repo", repo, "--title", title]
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        fh.write(body)
        body_file = fh.name
    try:
        proc = run_gh(args + ["--body-file", body_file], repo)
    finally:
        Path(body_file).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh issue failed: {proc.stderr.decode(errors='replace')}")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print instead of opening issues")
    parser.add_argument("--json", action="store_true", help="emit the pass-rate report as JSON")
    parser.add_argument("--repo", default=None, help="OWNER/REPO (default: $GITHUB_REPOSITORY)")
    parser.add_argument(
        "--runs", type=Path, default=None,
        help="offline run-history fixture {check_id: [\"ok\", ...]} instead of gh",
    )
    args = parser.parse_args(argv)

    cfg = load_criteria()
    checks = cfg["checks"]
    bar = Bar.from_config(cfg)

    if args.runs:
        fixture = json.loads(args.runs.read_text())
        results = gather_offline(checks, fixture)
    else:
        repo = args.repo or os.environ.get("GITHUB_REPOSITORY")
        if not repo:
            parser.error("no repository: pass --runs or set GITHUB_REPOSITORY")
        runs = list_runs(repo)
        results = gather(checks, runs, repo)

    qualified = []
    report = {}
    for cid in checks:
        seq = [s for _, s in results[cid]]
        ok, failures, total, detail = evaluate(seq, bar)
        report[cid] = {"runs": total, "failures": failures, "qualified": ok, "detail": detail}
        if ok:
            qualified.append((cid, failures, total, detail))

    if args.json:
        print(json.dumps({"bar": bar.__dict__, "checks": report}, indent=2))
        return 0

    if not qualified:
        print("No post-boot assertion has graduated yet. Nothing to report.")
    elif not args.runs:
        # Offline mode measures and reports only; opening an issue needs a
        # repo and gh. The --json report above already surfaced the gradation.
        title = "Post-boot assertion ready to graduate to strict"
        upsert_issue(repo, title, build_body(qualified, bar), args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
