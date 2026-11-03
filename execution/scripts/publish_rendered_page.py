#!/usr/bin/env python3
"""Publish through the configured /p alias workflow. Sensitive input uses stdin."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lib.capabilities.rendered_page_links import PublicationError, Publisher  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--configuration",
        required=True,
        help="owning deployment_configuration entity id",
    )
    args = parser.parse_args()
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise PublicationError("invalid_publication_request")
        result = Publisher().publish(args.configuration, request)
    except PublicationError as exc:
        result = {"alias_status": "blocked", "error_code": str(exc)}
        if exc.conformance_warnings is not None:
            result["conformance_warnings"] = exc.conformance_warnings
        print(json.dumps(result))
        return 1
    except Exception:
        print(
            json.dumps(
                {"alias_status": "blocked", "error_code": "publication_request_failed"}
            )
        )
        return 1
    print(json.dumps(result))
    return 0 if result["alias_status"] in {"verified", "not_applicable"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
