from __future__ import annotations

import unittest

from src.collect_pubmed import classify, extract_records
from src.publish import validate_document
from src.summarize_ja import guard_summary
from src.common import NOT_REPORTED, SUMMARY_FIELDS


class PipelineTests(unittest.TestCase):
    def test_pubmed_publication_type_is_extracted_from_xml(self) -> None:
        xml = """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID><Article>
        <ArticleTitle>Trial</ArticleTitle><Abstract><AbstractText>N=100 patients.</AbstractText></Abstract>
        <PublicationTypeList><PublicationType>Randomized Controlled Trial</PublicationType></PublicationTypeList>
        </Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""
        record = extract_records(xml)[0]
        self.assertEqual(record["publication_types"], ["Randomized Controlled Trial"])

    def test_company_is_derived_from_asset_configuration(self) -> None:
        pack = {"entities": {"focal_company": {"name": "Boehringer Ingelheim"}, "assets": [
            {"name": "empagliflozin", "brand": ["Jardiance"], "company": "Boehringer Ingelheim"}
        ]}, "sources": {"japan_relevance_terms": ["Japan"]}}
        record = {"title": "Jardiance in Japan", "abstract": ""}
        classify(record, pack)
        self.assertTrue(record["bi_related"])
        self.assertTrue(record["japan_relevant"])

    def test_missing_abstract_forces_all_fields_to_not_reported(self) -> None:
        guarded = guard_summary({field: "invented" for field in SUMMARY_FIELDS}, "")
        self.assertEqual(set(guarded.values()), {NOT_REPORTED})

    def test_numbers_not_in_abstract_are_removed(self) -> None:
        result = {field: "text" for field in SUMMARY_FIELDS}
        result["endpoint_results_ja"] = "HR 0.75"
        result["sample_size_ja"] = "N=999"
        guarded = guard_summary(result, "A randomized trial enrolled 100 participants.")
        self.assertEqual(guarded["endpoint_results_ja"], NOT_REPORTED)
        self.assertEqual(guarded["sample_size_ja"], NOT_REPORTED)

    def test_partial_summary_fails_quality_gate(self) -> None:
        row = {"pmid": "1", "title": "x", "url": "https://example.test", "content_sha256": "a", "publication_types": [], "summary_ja": "x"}
        self.assertIn("summary fields must be all present or all absent", validate_document(row))


if __name__ == "__main__":
    unittest.main()

