#!/usr/bin/env python3
"""Download the latest `corpus` artifact published by the build workflow.

The build publishes corpus.jsonl (+ .sha256 sidecar) as a 90-day workflow
artifact named `corpus` on every run that changes the feeds. Artifacts are
private to the repository, so this needs a token with `actions: read`
(GITHUB_TOKEN inside Actions, a fine-grained PAT elsewhere). If the `gh`
CLI is installed and authenticated, it is used transparently; otherwise
the script talks to the REST API directly.

Usage:
    python scripts/download_corpus.py --out-dir ./corpus-downloads
    python scripts/download_corpus.py --repo OWNER/REPO --run-id 12345

After downloading, verify + ingest:
    python scripts/ingest_corpus.py --db corpus.db --corpus corpus-downloads/corpus.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import httpx


def _headers(token: str | None) -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def latest_artifact(repo: str, token: str | None, run_id: int | None) -> tuple[dict, int]:
    api = "https://api.github.com"
    with httpx.Client(base_url=api, headers=_headers(token), timeout=30.0, follow_redirects=True) as c:
        if run_id is None:
            r = c.get(f"/repos/{repo}/actions/runs", params={"status": "success", "per_page": 10})
            r.raise_for_status()
            runs = [
                x
                for x in r.json().get("workflow_runs", [])
                if x.get("head_branch") == "main"
            ]
            if not runs:
                raise SystemExit("No successful runs found on main.")
            run_id = runs[0]["id"]
        r = c.get(f"/repos/{repo}/actions/runs/{run_id}/artifacts")
        r.raise_for_status()
        for art in r.json().get("artifacts", []):
            if art.get("name") == "corpus" and not art.get("expired"):
                return art, run_id
    raise SystemExit(
        f"No non-expired 'corpus' artifact on run {run_id}. "
        "Try an earlier successful run with --run-id."
    )


def download(repo: str, token: str | None, out_dir: Path, run_id: int | None) -> Path:
    art, run_id = latest_artifact(repo, token, run_id)
    print(f"Downloading artifact '{art['name']}' (id {art['id']}, {art['size_in_bytes']} B) from run {run_id}")

    gh = os.environ.get("GH_PATH", "gh")
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"corpus-{run_id}.zip"

    token_env = {"GH_TOKEN": token} if token else None
    if os.environ.get("USE_GH", "1") != "0" and _gh_available(gh):
        import subprocess

        subprocess.run(
            [gh, "api", f"/repos/{repo}/actions/artifacts/{art['id']}/zip"],
            check=True,
            stdout=open(zip_path, "wb"),
            env={**os.environ, **(token_env or {})},
        )
    else:
        if not token:
            raise SystemExit("Artifact downloads require GH_TOKEN/GITHUB_TOKEN when gh is unavailable.")
        with httpx.Client(headers=_headers(token), timeout=120.0, follow_redirects=True) as c:
            with c.stream("GET", f"https://api.github.com/repos/{repo}/actions/artifacts/{art['id']}/zip") as r:
                r.raise_for_status()
                with open(zip_path, "wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)

    wanted = ("corpus.jsonl", "corpus.jsonl.sha256")
    extracted: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(zip_path.read_bytes())) as z:
        for name in z.namelist():
            base = Path(name).name
            if base in wanted:
                extracted[base] = z.read(name)
    if "corpus.jsonl" not in extracted:
        raise SystemExit(f"corpus.jsonl missing from artifact zip ({sorted(extracted)})")

    body = extracted["corpus.jsonl"]
    if "corpus.jsonl.sha256" in extracted:
        expected = extracted["corpus.jsonl.sha256"].decode().split()[0]
        actual = hashlib.sha256(body).hexdigest()
        if expected != actual:
            raise SystemExit(f"SHA-256 MISMATCH: expected {expected}, got {actual} — refusing to write.")
        print(f"SHA-256 verified: {actual[:12]}…")

    final = out_dir / "corpus.jsonl"
    final.write_bytes(body)
    if "corpus.jsonl.sha256" in extracted:
        (out_dir / "corpus.jsonl.sha256").write_bytes(extracted["corpus.jsonl.sha256"])
    n_rows = sum(1 for line in body.decode("utf-8").splitlines() if line.strip())
    print(f"Wrote {n_rows} rows to {final}")
    return final


def _gh_available(gh: str) -> bool:
    import shutil
    import subprocess

    if shutil.which(gh) is None:
        return False
    try:
        subprocess.run([gh, "auth", "status"], capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "bedardandy/maine-government-feeds"))
    ap.add_argument("--out-dir", type=Path, default=Path("corpus-downloads"))
    ap.add_argument("--run-id", type=int, default=None, help="Specific workflow run (default: newest success on main)")
    ap.add_argument("--token", default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
    args = ap.parse_args()
    download(args.repo, args.token, args.out_dir, args.run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
