from __future__ import annotations

import argparse
import json

from .collect_pubmed import collect
from .publish import publish
from .summarize_ja import summarize


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-results", type=int)
    parser.add_argument("--summary-limit", type=int, default=12)
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--skip-summarize", action="store_true")
    args = parser.parse_args()
    result = {}
    if not args.skip_collect:
        result["collect"] = collect(args.max_results)
    if not args.skip_summarize:
        result["summarize"] = summarize(args.summary_limit)
    result["quality"] = publish()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

