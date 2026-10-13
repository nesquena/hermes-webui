"""Isolated entry point for provider status network requests."""

from __future__ import annotations

import json
import sys

from api.network_isolation import install_test_network_block

install_test_network_block()

from api.provider_status import _fetch_once


def main() -> None:
    rows, ttl, freshness_deadline = _fetch_once(sys.argv[1])
    print(json.dumps({
        "rows": rows,
        "ttl": ttl,
        "freshness_deadline": freshness_deadline.isoformat() if freshness_deadline else None,
    }))


if __name__ == "__main__":
    main()
