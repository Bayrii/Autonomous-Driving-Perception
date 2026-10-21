"""Download finished recordings from the deployed recorder to this PC.

    python tools/download_recordings.py https://<your-app>.onrender.com            # all finished recordings
    python tools/download_recordings.py https://<your-app>.onrender.com --id <session_id> --out recordings

The token is read from the APP_ACCESS_TOKEN environment variable or asked for interactively. It is sent
in the Authorization header, never in the URL. ZIPs are streamed to disk, so large recordings are fine.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def request(url: str, token: str, timeout: float = 120):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    return urllib.request.urlopen(req, timeout=timeout)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base_url", help="https address of the deployed app")
    ap.add_argument("--id", action="append", help="session id (repeatable); default: all finished recordings")
    ap.add_argument("--out", default="recordings", help="output folder (default: recordings/)")
    a = ap.parse_args(argv)
    base = a.base_url.rstrip("/")
    token = os.environ.get("APP_ACCESS_TOKEN") or getpass.getpass("Access token: ")
    try:
        with request(f"{base}/api/sessions", token, timeout=90) as r:   # 90 s: free servers wake up slowly
            sessions = json.load(r)["sessions"]
    except urllib.error.HTTPError as e:
        print(f"Listing failed: HTTP {e.code} {e.read().decode(errors='replace')[:200]}", file=sys.stderr)
        return 1
    wanted = [s for s in sessions if s["downloadable"] and (not a.id or s["session_id"] in a.id)]
    if not wanted:
        print("No finished recordings to download.")
        return 0
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for s in wanted:
        dest = out / f"recording-{s['session_id']}.zip"
        if dest.exists():
            print(f"skip {dest} (already downloaded)")
            continue
        tmp = dest.with_suffix(".part")
        with request(f"{base}/api/sessions/{s['session_id']}/download", token, timeout=600) as r, open(tmp, "wb") as fh:
            while chunk := r.read(1 << 20):
                fh.write(chunk)
        tmp.replace(dest)
        print(f"{dest}  ({dest.stat().st_size / 1e6:.1f} MB, {s['frames']} frames, {s['imu_samples']} IMU samples)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
