from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests
import yaml

from .common import DATA_DIR, ROOT, atomic_write_json, now_utc, read_json

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
CORPUS_PATH = DATA_DIR / "corpus.json"


def request_with_backoff(url: str, params: dict[str, str], attempts: int = 6) -> requests.Response:
    delay = 1.0
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            response = requests.get(url, params=params, timeout=60)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
                delay = min(delay * 2, 30)
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError(f"PubMed request failed after {attempts} attempts") from last_error


def extract_records(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    records: list[dict[str, Any]] = []
    for item in root.findall(".//PubmedArticle"):
        medline = item.find(".//MedlineCitation")
        article = item.find(".//Article")
        if medline is None or article is None:
            continue
        pmid = (medline.findtext("PMID") or "").strip()
        if not pmid:
            continue
        title_element = article.find("ArticleTitle")
        title = "".join(title_element.itertext()).strip() if title_element is not None else ""
        abstract_parts: list[str] = []
        for node in article.findall(".//Abstract/AbstractText"):
            text = "".join(node.itertext()).strip()
            label = node.attrib.get("Label") or node.attrib.get("NlmCategory")
            if text:
                abstract_parts.append(f"{label}: {text}" if label else text)
        authors: list[str] = []
        for author in article.findall(".//AuthorList/Author")[:8]:
            name = f"{author.findtext('LastName') or ''} {author.findtext('ForeName') or ''}".strip()
            if name:
                authors.append(name)
        journal = (article.findtext(".//Journal/Title") or article.findtext(".//Journal/ISOAbbreviation") or "").strip()
        pub_date = ""
        date_node = article.find(".//ArticleDate")
        if date_node is not None and date_node.findtext("Year"):
            pub_date = f"{date_node.findtext('Year')}-{(date_node.findtext('Month') or '01').zfill(2)}-{(date_node.findtext('Day') or '01').zfill(2)}"
        if not pub_date:
            issue_date = article.find(".//Journal/JournalIssue/PubDate")
            if issue_date is not None and issue_date.findtext("Year"):
                month = issue_date.findtext("Month") or "01"
                pub_date = f"{issue_date.findtext('Year')}-{month.zfill(2) if month.isdigit() else '01'}-01"
        doi = ""
        for identifier in item.findall(".//PubmedData/ArticleIdList/ArticleId"):
            if identifier.attrib.get("IdType") == "doi":
                doi = (identifier.text or "").strip()
        publication_types = [
            (node.text or "").strip()
            for node in article.findall(".//PublicationTypeList/PublicationType")
            if (node.text or "").strip()
        ]
        mesh_terms = [
            (node.text or "").strip()
            for node in medline.findall(".//MeshHeadingList/MeshHeading/DescriptorName")
            if (node.text or "").strip()
        ]
        records.append({
            "pmid": pmid,
            "title": title,
            "abstract": "\n".join(abstract_parts),
            "journal": journal,
            "authors": ", ".join(authors),
            "pub_date": pub_date,
            "doi": doi,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "publication_types": publication_types,
            "mesh_terms": mesh_terms,
        })
    return records


def classify(record: dict[str, Any], pack: dict[str, Any]) -> None:
    searchable = f"{record['title']}\n{record['abstract']}".lower()
    assets: list[str] = []
    companies: list[str] = []
    for asset in pack["entities"]["assets"]:
        terms = [asset["name"], *asset.get("brand", [])]
        if any(term.lower() in searchable for term in terms):
            assets.append(asset["name"])
            companies.append(asset["company"])
    record["matched_assets"] = sorted(set(assets))
    record["matched_companies"] = sorted(set(companies))
    focal = pack["entities"]["focal_company"]["name"]
    record["bi_related"] = focal in companies
    record["japan_relevant"] = any(
        term.lower() in searchable for term in pack["sources"].get("japan_relevance_terms", [])
    )


def content_hash(record: dict[str, Any]) -> str:
    content = json.dumps(
        {key: record.get(key) for key in ("title", "abstract", "journal", "pub_date", "publication_types")},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def collect(max_results: int | None = None) -> dict[str, int]:
    pack = yaml.safe_load((ROOT / "pack.yaml").read_text(encoding="utf-8"))
    settings = pack["sources"]["pubmed"]
    limit = max_results or int(settings["max_per_run"])
    api_key = os.environ.get("NCBI_API_KEY", "")
    search_params = {"db": "pubmed", "term": settings["query"], "retmax": str(limit), "sort": "date", "retmode": "json"}
    if api_key:
        search_params["api_key"] = api_key
    pmids = request_with_backoff(ESEARCH_URL, search_params).json().get("esearchresult", {}).get("idlist", [])
    fetch_params = {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"}
    if api_key:
        fetch_params["api_key"] = api_key
    fetched = extract_records(request_with_backoff(EFETCH_URL, fetch_params).text) if pmids else []
    corpus = read_json(CORPUS_PATH, {"schema_version": "1.0", "documents": []})
    existing = {row["pmid"]: row for row in corpus.get("documents", [])}
    new_count = updated_count = 0
    timestamp = now_utc()
    for record in fetched:
        classify(record, pack)
        digest = content_hash(record)
        previous = existing.get(record["pmid"])
        if previous and previous.get("content_sha256") == digest:
            continue
        if previous:
            for key in tuple(previous):
                if key.endswith("_ja") or key.startswith("summary_"):
                    previous.pop(key, None)
            previous.update(record)
            previous["content_sha256"] = digest
            previous["updated_at_utc"] = timestamp
            updated_count += 1
        else:
            record.update({"content_sha256": digest, "fetched_at_utc": timestamp, "updated_at_utc": timestamp})
            existing[record["pmid"]] = record
            new_count += 1
    documents = sorted(existing.values(), key=lambda row: (row.get("pub_date", ""), row["pmid"]), reverse=True)
    payload = {"schema_version": "1.0", "generated_at_utc": timestamp, "documents": documents}
    atomic_write_json(CORPUS_PATH, payload)
    return {"found": len(pmids), "new": new_count, "updated": updated_count, "total": len(documents)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-results", type=int)
    args = parser.parse_args()
    print(json.dumps(collect(args.max_results), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

