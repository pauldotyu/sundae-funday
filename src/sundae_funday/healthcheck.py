"""Simple HTTP healthcheck for containers."""

import os
import urllib.error
import urllib.request


def main() -> int:
    port = int(os.getenv("PORT", "8301"))
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=5):
            return 0
    except urllib.error.URLError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
