import os
import socket
from urllib.parse import urlparse

import httpx


def main() -> int:
    api_key = os.environ.get("ROQ_API_KEY")
    if not api_key:
        raise SystemExit(
            "ROQ_API_KEY is not set. Run: export ROQ_API_KEY='your-key'\n"
            "Optional: export ROQ_BASE_URL='https://api.roq.com/openai/v1'"
        )

    base_url = os.environ.get("ROQ_BASE_URL", "https://api.roq.com/openai/v1")
    parsed = urlparse(base_url)
    host = parsed.hostname or "<unknown-host>"

    print(f"Checking ROQ connectivity for: {base_url}")
    print(f"Host: {host}")

    try:
        socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        print("DNS resolution failed.")
        print(f"Details: Unable to resolve the ROQ hostname '{host}'. Check your network/DNS and the ROQ base URL.")
        return 1

    url = base_url.rstrip("/") + "/models"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=20.0,
        )
    except Exception as exc:
        print("Request failed.")
        print(f"Details: {exc}")
        return 1

    print(f"HTTP status: {response.status_code}")
    if response.status_code in (200, 201, 202):
        print("Connectivity check passed: the ROQ endpoint is reachable and responded successfully.")
        return 0

    body = response.text.strip()
    if body:
        preview = body[:250].replace("\n", " ")
        print(f"Response preview: {preview}")
    print("Connectivity check failed: the endpoint responded, but not successfully.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
