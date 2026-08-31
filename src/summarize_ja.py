from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from .common import DATA_DIR, NOT_REPORTED, SUMMARY_FIELDS, atomic_write_json, now_utc, read_json

CORPUS_PATH = DATA_DIR / "corpus.json"
USAGE_PATH = DATA_DIR / "ai-usage.json"
MODEL = "@cf/qwen/qwen3.8-27b"
SUMMARY_DAILY_NEURON_BUDGET = 4000.0
INPUT_NEURONS_PER_TOKEN = 0.45 / 0.011 / 1000
OUTPUT_NEURONS_PER_TOKEN = 3.20 / 0.011 / 1000

PROMPT = """あなたは臨床データ管理・臨床開発の視点を持つ医学文献アナリストです。
与えられた論文情報だけを根拠に、指定された7フィールドのJSONオブジェクトだけを返してください。
抄録から読み取れない項目は、推測せず必ず文字列「抄録に記載なし」にしてください。
endpoint_results_jaには抄録に実際に書かれた生の数値だけを使い、補完・換算・推定を禁止します。

フィールド:
summary_ja: 2〜3文の要約
key_takeaway_ja: 1文の要点
why_matters_ja: Boehringer IngelheimのCardio-Renal-Metabolic領域から見た意義
study_type_ja: 試験デザイン
endpoints_ja: 主要・副次エンドポイント
endpoint_results_ja: エンドポイントの結果
sample_size_ja: 症例数

Title: {title}
Journal: {journal}
Publication Type: {publication_types}
Abstract: {abstract}
"""


def extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Qwen response did not contain a JSON object")
    value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Qwen response must be a JSON object")
    return value


def numeric_tokens(value: str) -> set[str]:
    return {token.replace(",", "") for token in re.findall(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?", value or "")}


def guard_summary(result: dict[str, Any], abstract: str) -> dict[str, str]:
    if not abstract.strip():
        return {field: NOT_REPORTED for field in SUMMARY_FIELDS}
    guarded: dict[str, str] = {}
    for field in SUMMARY_FIELDS:
        value = str(result.get(field) or "").strip()
        guarded[field] = value if value else NOT_REPORTED
    source_numbers = numeric_tokens(abstract)
    for field in ("endpoint_results_ja", "sample_size_ja"):
        claimed = numeric_tokens(guarded[field])
        if claimed - source_numbers:
            guarded[field] = NOT_REPORTED
    return guarded


def usage_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def current_usage() -> dict[str, Any]:
    payload = read_json(USAGE_PATH, {})
    if payload.get("date_utc") != usage_day():
        return {"date_utc": usage_day(), "estimated_neurons": 0.0, "calls": 0}
    return payload


def estimate_neurons(usage: dict[str, Any], prompt: str, output: str) -> float:
    prompt_tokens = usage.get("prompt_tokens") or max(1, len(prompt) // 4)
    output_tokens = usage.get("completion_tokens") or max(1, len(output) // 3)
    return float(prompt_tokens) * INPUT_NEURONS_PER_TOKEN + float(output_tokens) * OUTPUT_NEURONS_PER_TOKEN


def call_qwen(prompt: str) -> tuple[dict[str, Any], float]:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not account_id or not token:
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN are required")
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{MODEL}"
    body = {
        "messages": [
            {"role": "system", "content": "Return valid JSON only. Do not use markdown."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 1200,
    }
    delay = 2.0
    last_error: Exception | None = None
    for _ in range(5):
        try:
            response = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=120)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
                delay = min(delay * 2, 30)
                continue
            if response.status_code in {500, 502, 503, 504}:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            response.raise_for_status()
            payload = response.json()
            result = payload.get("result", payload)
            output = result.get("response") or result.get("result", {}).get("response")
            if not isinstance(output, str) or not output.strip():
                raise ValueError("Cloudflare Qwen returned an empty response")
            return extract_json(output), estimate_neurons(result.get("usage") or {}, prompt, output)
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError("Cloudflare Qwen request failed after retries") from last_error


def summarize(limit: int = 0) -> dict[str, Any]:
    corpus = read_json(CORPUS_PATH, {"documents": []})
    pending = [row for row in corpus.get("documents", []) if not all(row.get(field) for field in SUMMARY_FIELDS)]
    if limit:
        pending = pending[:limit]
    usage = current_usage()
    completed = 0
    failures: list[dict[str, str]] = []
    for row in pending:
        if float(usage["estimated_neurons"]) >= SUMMARY_DAILY_NEURON_BUDGET:
            break
        prompt = PROMPT.format(
            title=row.get("title") or "",
            journal=row.get("journal") or "",
            publication_types=", ".join(row.get("publication_types") or []),
            abstract=row.get("abstract") or "[no abstract available]",
        )
        try:
            raw, neurons = call_qwen(prompt)
            row.update(guard_summary(raw, row.get("abstract") or ""))
            row["summary_model"] = MODEL
            row["summary_generated_at_utc"] = now_utc()
            usage["estimated_neurons"] = round(float(usage["estimated_neurons"]) + neurons, 2)
            usage["calls"] = int(usage["calls"]) + 1
            completed += 1
            atomic_write_json(CORPUS_PATH, corpus)
            atomic_write_json(USAGE_PATH, usage)
        except Exception as exc:  # failure stays pending; no placeholder is persisted
            failures.append({"pmid": row.get("pmid", ""), "error": f"{type(exc).__name__}: {exc}"})
    return {
        "pending_at_start": len(pending),
        "completed": completed,
        "failed": len(failures),
        "remaining": sum(1 for row in corpus.get("documents", []) if not all(row.get(field) for field in SUMMARY_FIELDS)),
        "usage": usage,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    result = summarize(args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

