#!/usr/bin/env python3
"""
Upload an image file to GitHub's CDN and print the resulting URL.

GitHub's web UI uses an internal S3-backed upload policy endpoint when users
paste images into issue/PR comments.  This script replicates that flow so CI
can embed screenshots directly in PR comments rather than linking to artifacts.

Usage:
    python3 upload_screenshot.py <owner/repo> <image_path> <display_name>

Exits 0 and prints the CDN URL on success.
Exits 1 on failure (caller should fall back to artifact link).

Requires:
    GITHUB_TOKEN env var with repo write scope.
"""

import json
import os
import sys
import urllib.error
import urllib.request


def upload(repo: str, image_path: str, display_name: str) -> str:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set")

    with open(image_path, "rb") as f:
        data = f.read()
    size = len(data)

    # ── Step 1: request an upload policy ──────────────────────────────────────
    policy_url = f"https://github.com/{repo}/upload/policy"
    policy_body = json.dumps(
        {"file_name": display_name, "content_type": "image/png", "size": size}
    ).encode()

    req = urllib.request.Request(
        policy_url,
        data=policy_body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        policy = json.loads(resp.read())

    upload_url: str = policy["upload_url"]
    form: dict = policy["form"]
    asset_href: str = policy["asset"]["href"]

    # ── Step 2: multipart/form-data POST to S3 ────────────────────────────────
    boundary = "CIUploadBoundary"
    crlf = b"\r\n"
    parts = b""
    for key, value in form.items():
        parts += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
    parts += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{display_name}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + data + crlf + f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        upload_url,
        data=parts,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        # S3 may return 204 No Content on success, which urlopen treats as ok,
        # or other 2xx.  Any 4xx/5xx is a real failure.
        if e.code >= 400:
            raise

    return asset_href


def main() -> None:
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <owner/repo> <image_path> <display_name>",
              file=sys.stderr)
        sys.exit(1)

    repo, image_path, display_name = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        url = upload(repo, image_path, display_name)
        print(url)
    except Exception as e:
        print(f"[upload_screenshot] ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
