from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import DATA_DIR, SITE_DATA_DIR, SUMMARY_FIELDS, atomic_write_json, now_utc, read_json

CORPUS_PATH = DATA_DIR / "corpus.json"


def validate_document(row: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ("pmid", "title", "url", "content_sha256"):
        if not str(row.get(field) or "").strip():
            errors.append(f"{field} is blank")
    if not isinstance(row.get("publication_types"), list):
        errors.append("publication_types must be a list")
    present = [bool(str(row.get(field) or "").strip()) for field in SUMMARY_FIELDS]
    if any(present) and not all(present):
        errors.append("summary fields must be all present or all absent")
    if any("[要約エラー]" in str(row.get(field) or "") for field in SUMMARY_FIELDS):
        errors.append("error placeholder must not be published")
    return errors


def publish() -> dict[str, Any]:
    corpus = read_json(CORPUS_PATH, {"documents": []})
    documents = corpus.get("documents", [])
    seen: set[str] = set()
    problems: list[dict[str, Any]] = []
    for row in documents:
        pmid = str(row.get("pmid") or "")
        if pmid in seen:
            problems.append({"pmid": pmid, "errors": ["duplicate PMID"]})
        seen.add(pmid)
        errors = validate_document(row)
        if errors:
            problems.append({"pmid": pmid, "errors": errors})
    summarized = [row for row in documents if all(str(row.get(field) or "").strip() for field in SUMMARY_FIELDS)]
    latest = sorted(summarized, key=lambda row: (row.get("pub_date", ""), row.get("pmid", "")), reverse=True)[:150]
    timestamp = now_utc()
    status = {
        "schema_version": "1.0",
        "checked_at_utc": timestamp,
        "overall_status": "healthy" if not problems and len(summarized) == len(documents) else "degraded",
        "counts": {
            "documents": len(documents),
            "summarized": len(summarized),
            "pending": len(documents) - len(summarized),
            "publishable": len(latest),
            "validation_problems": len(problems),
        },
        "problems": problems[:20],
    }
    if problems:
        atomic_write_json(DATA_DIR / "quality-status.json", status)
        raise ValueError(f"quality gate failed for {len(problems)} document(s)")
    feed = {"schema_version": "1.0", "generated_at_utc": timestamp, "items": latest}
    atomic_write_json(DATA_DIR / "news-latest.json", feed)
    atomic_write_json(DATA_DIR / "quality-status.json", status)
    SITE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("news-latest.json", "quality-status.json"):
        shutil.copy2(DATA_DIR / name, SITE_DATA_DIR / name)
    return status


if __name__ == "__main__":
    print(json.dumps(publish(), ensure_ascii=False, indent=2))

