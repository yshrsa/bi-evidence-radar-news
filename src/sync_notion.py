from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

import requests

from .common import DATA_DIR, SUMMARY_FIELDS, read_json

NOTION_VERSION = "2022-06-28"


def rich_text(value: str, limit: int = 1900) -> list[dict[str, Any]]:
    text = value[:limit]
    return [{"type": "text", "text": {"content": text}}] if text else []


def properties(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "Title": {"title": rich_text(row.get("title") or "")},
        "PMID": {"rich_text": rich_text(str(row.get("pmid") or ""))},
        "URL": {"url": row.get("url") or None},
        "Publication Date": {"date": {"start": row["pub_date"]} if row.get("pub_date") else None},
        "Journal": {"rich_text": rich_text(row.get("journal") or "")},
        "Publication Types": {"multi_select": [{"name": value[:100]} for value in (row.get("publication_types") or [])[:10]]},
        "Assets": {"multi_select": [{"name": value[:100]} for value in (row.get("matched_assets") or [])]},
        "BI Related": {"checkbox": bool(row.get("bi_related"))},
        "Japan Relevant": {"checkbox": bool(row.get("japan_relevant"))},
        "Summary JA": {"rich_text": rich_text(row.get("summary_ja") or "")},
        "Key Takeaway JA": {"rich_text": rich_text(row.get("key_takeaway_ja") or "")},
        "Why Matters JA": {"rich_text": rich_text(row.get("why_matters_ja") or "")},
        "Study Type JA": {"rich_text": rich_text(row.get("study_type_ja") or "")},
        "Endpoints JA": {"rich_text": rich_text(row.get("endpoints_ja") or "")},
        "Endpoint Results JA": {"rich_text": rich_text(row.get("endpoint_results_ja") or "")},
        "Sample Size JA": {"rich_text": rich_text(row.get("sample_size_ja") or "")},
        "Content SHA256": {"rich_text": rich_text(row.get("content_sha256") or "")},
    }


class NotionClient:
    def __init__(self, token: str, database_id: str) -> None:
        self.database_id = database_id
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        delay = 1.0
        last_error: Exception | None = None
        for _ in range(5):
            try:
                response = requests.request(method, url, headers=self.headers, timeout=60, **kwargs)
                if response.status_code == 429:
                    retry = response.headers.get("Retry-After")
                    time.sleep(float(retry) if retry and retry.isdigit() else delay)
                    delay = min(delay * 2, 20)
                    continue
                if response.status_code in {500, 502, 503, 504}:
                    time.sleep(delay)
                    delay = min(delay * 2, 20)
                    continue
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(delay)
        raise RuntimeError("Notion request failed after retries") from last_error

    def find(self, pmid: str) -> dict[str, Any] | None:
        response = self.request("POST", f"https://api.notion.com/v1/databases/{self.database_id}/query", json={
            "filter": {"property": "PMID", "rich_text": {"equals": pmid}}, "page_size": 1
        }).json()
        results = response.get("results") or []
        return results[0] if results else None

    def upsert(self, row: dict[str, Any]) -> str:
        existing = self.find(str(row["pmid"]))
        if existing:
            self.request("PATCH", f"https://api.notion.com/v1/pages/{existing['id']}", json={"properties": properties(row)})
            return "updated"
        self.request("POST", "https://api.notion.com/v1/pages", json={
            "parent": {"database_id": self.database_id}, "properties": properties(row)
        })
        return "created"


def sync(limit: int = 0) -> dict[str, int]:
    token = os.environ.get("NOTION_API_KEY", "").strip()
    database_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not token or not database_id:
        raise RuntimeError("NOTION_API_KEY and NOTION_DATABASE_ID are required")
    documents = [row for row in read_json(DATA_DIR / "corpus.json", {"documents": []})["documents"] if all(row.get(field) for field in SUMMARY_FIELDS)]
    if limit:
        documents = documents[:limit]
    client = NotionClient(token, database_id)
    counts = {"created": 0, "updated": 0}
    for row in documents:
        counts[client.upsert(row)] += 1
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    print(json.dumps(sync(args.limit), ensure_ascii=False))

