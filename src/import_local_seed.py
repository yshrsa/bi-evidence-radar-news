from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from .collect_pubmed import classify, content_hash
from .common import DATA_DIR, NOT_REPORTED, SUMMARY_FIELDS, atomic_write_json, now_utc


def decode_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return value
    try:
        decoded = json.loads(value or "[]")
        return decoded if isinstance(decoded, list) else []
    except json.JSONDecodeError:
        return []


def import_seed(source: Path) -> int:
    uri = f"file:{source.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
        required = {"pmid", "title", "abstract", "url", *SUMMARY_FIELDS}
        missing = required - columns
        if missing:
            raise ValueError("source database is missing columns: " + ", ".join(sorted(missing)))
        rows = connection.execute("SELECT * FROM documents ORDER BY pub_date DESC, pmid DESC").fetchall()
    finally:
        connection.close()
    pack = yaml.safe_load((Path(__file__).resolve().parents[1] / "pack.yaml").read_text(encoding="utf-8"))
    timestamp = now_utc()
    documents: list[dict[str, Any]] = []
    for raw in rows:
        record = {
            "pmid": str(raw["pmid"]),
            "title": raw["title"] or "",
            "abstract": raw["abstract"] or "",
            "journal": raw["journal"] or "",
            "authors": raw["authors"] or "",
            "pub_date": raw["pub_date"] or "",
            "doi": raw["doi"] or "",
            "url": raw["url"] or f"https://pubmed.ncbi.nlm.nih.gov/{raw['pmid']}/",
            "publication_types": decode_list(raw["publication_types"]),
            "mesh_terms": decode_list(raw["mesh_terms"]),
            "fetched_at_utc": raw["fetched_at_utc"] or timestamp,
            "updated_at_utc": raw["updated_at_utc"] or timestamp,
        }
        classify(record, pack)
        record["content_sha256"] = content_hash(record)
        imported_summary = {field: str(raw[field] or "").strip() for field in SUMMARY_FIELDS}
        # The older local corpus has three-field summaries on many rows.  The cloud
        # feed publishes only complete seven-field records; partial rows remain a
        # clean pending item rather than leaking an ambiguous mixed-version record.
        if not record["abstract"].strip():
            record.update({field: NOT_REPORTED for field in SUMMARY_FIELDS})
            record["summary_model"] = "deterministic/missing-abstract"
            record["summary_generated_at_utc"] = timestamp
        elif all(imported_summary.values()):
            record.update(imported_summary)
            record["summary_model"] = "local/qwen3.8:27b"
            record["summary_generated_at_utc"] = raw["summary_ja_generated_at_utc"] or timestamp
        else:
            record.update({field: "" for field in SUMMARY_FIELDS})
        documents.append(record)
    atomic_write_json(DATA_DIR / "corpus.json", {"schema_version": "1.0", "generated_at_utc": timestamp, "documents": documents})
    return len(documents)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    print(json.dumps({"imported": import_seed(args.source)}, ensure_ascii=False))
