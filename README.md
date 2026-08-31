# BI Evidence Radar — Cloud News

Cardio-Renal-Metabolic領域のPubMed文献を毎日収集し、Cloudflare Workers AI上の
Qwen 3.8 27Bで7項目の日本語情報を生成してGitHub Pagesへ公開します。

## データ契約

PubMed XMLの`PublicationTypeList`とMeSHを直接取得します。RCT等のエビデンス階層を
LLMに分類させません。Qwenが生成する項目は次の7つです。

1. `summary_ja`
2. `key_takeaway_ja`
3. `why_matters_ja`
4. `study_type_ja`
5. `endpoints_ja`
6. `endpoint_results_ja`
7. `sample_size_ja`

抄録から分からない項目は`抄録に記載なし`とし、結果・症例数に現れた数値が抄録に
存在しない場合はPython側でも同文字列へ戻します。7項目が揃わない文献は公開しません。

## 無料枠と障害時動作

- OpenAI API、Vectorize、有料フォールバックは使いません。
- 要約はUTC 1日4,000 Neuronsまで、既定で最大12件/runです。
- 429は`Retry-After`、5xxは指数バックオフで再試行します。
- 失敗時はエラー文を保存せず未処理のまま翌日へ繰り越します。
- 公開前の品質ゲートに失敗した場合、前回の正常な静的データを維持します。

## ローカル検証

```powershell
python -m unittest discover -s tests -v
python -m src.publish
```

日次更新にはGitHub Secretsとして`CLOUDFLARE_ACCOUNT_ID`と
`CLOUDFLARE_API_TOKEN`が必要です。Notion同期を有効にする場合は
`NOTION_API_KEY`と`NOTION_DATABASE_ID`も設定します。

