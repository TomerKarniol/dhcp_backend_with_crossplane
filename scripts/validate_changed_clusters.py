#!/usr/bin/env python3
"""Detect which cluster files changed and validate each one with its full merge chain.

Repository structure expected:
    sites/
      {site}/
        config.yaml                           # site-level overrides (optional)
        mce/
          {mce}/
            config.yaml                       # MCE-level overrides (optional)
            hosted-cluster/
              {cluster}.yaml                  # cluster-specific values (required)

Merge chain for every cluster (left to right, last value wins):
    1. sites/{site}/config.yaml               (if file exists)
    2. sites/{site}/mce/{mce}/config.yaml     (if file exists)
    3. sites/{site}/mce/{mce}/hosted-cluster/{cluster}.yaml

Smart change detection — validates only what is affected:
    sites/{s}/config.yaml           → all clusters in site {s}
    sites/{s}/mce/{m}/config.yaml   → all clusters in MCE {m}
    hosted-cluster/{c}.yaml         → just cluster {c}

Usage:
    # Auto-detect changed files from git (normal CI usage)
    python scripts/validate_changed_clusters.py

    # Validate a specific cluster file directly
    python scripts/validate_changed_clusters.py sites/site-a/mce/mce-1/hosted-cluster/cluster-1.yaml

    # Validate every cluster regardless of what changed
    python scripts/validate_changed_clusters.py --all

GitLab CI environment variables used automatically:
    CI_MERGE_REQUEST_DIFF_BASE_SHA   MR pipelines
    CI_COMMIT_BEFORE_SHA             push pipelines
    CI_COMMIT_SHA                    current commit
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = REPO_ROOT / "scripts" / "validate_dhcp_values.py"
SITES_DIR = REPO_ROOT / "sites"


# ---------------------------------------------------------------------------
# Cluster discovery
# ---------------------------------------------------------------------------

def _find_all_clusters() -> list[Path]:
    return sorted(SITES_DIR.glob("*/mce/*/hosted-cluster/*.yaml"))


def _find_clusters_in_site(site: str) -> list[Path]:
    return sorted((SITES_DIR / site).glob("mce/*/hosted-cluster/*.yaml"))


def _find_clusters_in_mce(site: str, mce: str) -> list[Path]:
    return sorted((SITES_DIR / site / "mce" / mce).glob("hosted-cluster/*.yaml"))


def _build_merge_chain(cluster_path: Path) -> list[Path]:
    """Return the ordered list of values files to merge for this cluster.

    Only includes files that actually exist — site and MCE config layers are
    optional and silently skipped when absent.
    """
    try:
        rel = cluster_path.relative_to(REPO_ROOT)
    except ValueError:
        rel = cluster_path

    parts = rel.parts
    # Expected: ('sites', '<site>', 'mce', '<mce>', 'hosted-cluster', '<cluster>.yaml')
    if (
        len(parts) != 6
        or parts[0] != "sites"
        or parts[2] != "mce"
        or parts[4] != "hosted-cluster"
    ):
        print(f"WARNING: unexpected path structure: {rel} — skipping", file=sys.stderr)
        return []

    site, mce = parts[1], parts[3]

    candidates = [
        SITES_DIR / site / "config.yaml",
        SITES_DIR / site / "mce" / mce / "config.yaml",
        REPO_ROOT / rel,
    ]
    return [f for f in candidates if f.exists()]


# ---------------------------------------------------------------------------
# Git change detection
# ---------------------------------------------------------------------------

def _run_git(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return [line for line in result.stdout.splitlines() if line] if result.returncode == 0 else []


def _get_changed_yaml_files() -> list[str] | None:
    """Return YAML paths changed vs the push/MR base, or None if unknown."""
    null_sha = "0" * 40

    base = os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA", "")
    if base and base != null_sha:
        files = _run_git("diff", "--name-only", base, "HEAD")
        return [f for f in files if f.endswith(".yaml")]

    before = os.environ.get("CI_COMMIT_BEFORE_SHA", "")
    after = os.environ.get("CI_COMMIT_SHA", "HEAD")
    if before and before != null_sha:
        files = _run_git("diff", "--name-only", before, after)
        return [f for f in files if f.endswith(".yaml")]

    return None  # can't determine — caller should validate all


def _determine_clusters_to_validate(changed_files: list[str]) -> list[Path]:
    """Map a list of changed file paths to the cluster files that need validation."""
    clusters: set[Path] = set()

    for f in changed_files:
        parts = Path(f).parts

        if parts[0] != "sites":
            continue

        # sites/{site}/config.yaml → all clusters in that site
        if len(parts) == 3 and parts[2] == "config.yaml":
            clusters.update(_find_clusters_in_site(parts[1]))

        # sites/{site}/mce/{mce}/config.yaml → all clusters in that MCE
        elif len(parts) == 5 and parts[2] == "mce" and parts[4] == "config.yaml":
            clusters.update(_find_clusters_in_mce(parts[1], parts[3]))

        # sites/{site}/mce/{mce}/hosted-cluster/{cluster}.yaml → just this cluster
        elif (
            len(parts) == 6
            and parts[2] == "mce"
            and parts[4] == "hosted-cluster"
            and parts[5].endswith(".yaml")
        ):
            abs_path = REPO_ROOT / Path(f)
            if abs_path.exists():
                clusters.add(abs_path)

    return sorted(clusters)


# ---------------------------------------------------------------------------
# Validation runner
# ---------------------------------------------------------------------------

def _validate_cluster(cluster: Path) -> bool:
    """Call validate_dhcp_values.py with the full merge chain for this cluster."""
    chain = _build_merge_chain(cluster)
    if not chain:
        return True

    try:
        label = cluster.relative_to(REPO_ROOT)
    except ValueError:
        label = cluster

    print(f"\n--- {label} ---")
    for f in chain:
        try:
            print(f"  + {f.relative_to(REPO_ROOT)}")
        except ValueError:
            print(f"  + {f}")

    result = subprocess.run(
        [sys.executable, str(VALIDATOR), *[str(f) for f in chain]],
        cwd=REPO_ROOT,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = sys.argv[1:]

    if "--all" in args:
        clusters = _find_all_clusters()
        if not clusters:
            print("No cluster files found under sites/")
            sys.exit(0)
        print(f"Validating all {len(clusters)} cluster(s).")

    elif args:
        clusters = []
        for a in args:
            p = Path(a)
            if not p.is_absolute():
                p = REPO_ROOT / p
            if not p.exists():
                print(f"ERROR: file not found: {a}", file=sys.stderr)
                sys.exit(1)
            clusters.append(p)

    else:
        changed = _get_changed_yaml_files()
        if changed is None:
            print("Cannot determine changed files — validating all clusters.")
            clusters = _find_all_clusters()
        else:
            clusters = _determine_clusters_to_validate(changed)
            if not clusters:
                print("No cluster files affected — nothing to validate.")
                sys.exit(0)
            print(f"{len(clusters)} cluster(s) affected by this change.")

    passed, failed = [], []
    for cluster in clusters:
        if _validate_cluster(cluster):
            passed.append(cluster)
        else:
            failed.append(cluster)

    print(f"\n{'=' * 60}")
    print(f"Results: {len(passed)} passed  {len(failed)} failed")

    if failed:
        print("\nFailed:")
        for c in failed:
            try:
                print(f"  FAIL  {c.relative_to(REPO_ROOT)}")
            except ValueError:
                print(f"  FAIL  {c}")

    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
