# kihon-master-spec

診療報酬制度の「レセプト電算処理システム マスターファイル仕様書」PDFを、
コーディングエージェント（LLM）や周辺ツールが扱いやすい構造化JSONに変換するツール。

PDF（[社会保険診療報酬支払基金 公開資料][ssk]）はページ表形式で配布されており、
そのままではコードからの参照やLLMへの参照入力が難しい。本ツールは、

- PDF URL を指定するだけで **ダウンロード → 表抽出 → JSON化** を自動化する
- 改定（版）毎にディレクトリを分けて **バージョン管理** できる
- CLI から **マスター一覧・項目定義・コード値検索** ができる

ことで、仕様書を一次情報として扱いやすくする。

[ssk]: https://www.ssk.or.jp/seikyushiharai/tensuhyo/kihonmasta/

## 出力スキーマ

1マスター = 1JSONファイル。10種以上のマスター（歯科診療行為のサブテーブルを含めると18〜19）が
それぞれ `data/<version>/<master_id>.json` に出力される。

```json
{
  "masterId": "iyakuhin",
  "masterName": "医薬品マスター",
  "subName": null,
  "version": "20260331",
  "pages": { "start": 20, "end": 23 },
  "fields": [
    {
      "seq": 14,
      "name": "麻薬・毒薬・覚醒剤原料等",
      "mode": "numeric",
      "maxBytes": 1,
      "itemFormat": "固定",
      "description": "...",
      "codes": [
        { "code": "0", "name": "「１」から「５」以外の医薬品" },
        { "code": "1", "name": "麻薬" },
        { "code": "2", "name": "毒薬" },
        { "code": "3", "name": "覚醒剤原料" },
        { "code": "5", "name": "向精神薬" }
      ]
    }
  ]
}
```

スキーマは [`kohii/medi-xplorer`][mx] の `src/features/*/master-fields.ts` と
互換になるように設計している（`alias`・`columnWidth` などの独自拡張は手動で重ねる前提）。

[mx]: https://github.com/kohii/medi-xplorer

## ディレクトリ構成

```
kihon-master-spec/
├── scripts/
│   ├── extract.py      # PDF → JSON 変換エンジン
│   └── cli.py          # CLI エントリポイント
├── data/
│   ├── raw/            # ダウンロード済みPDF (キャッシュ)
│   └── <YYYYMMDD>/     # 抽出結果（バージョン別）
│       ├── manifest.json
│       ├── <master>.json
│       └── sections.debug.json
├── pyproject.toml
└── uv.lock
```

## セットアップ

[uv](https://docs.astral.sh/uv/) を使う。

```bash
uv sync
```

## 使い方

### PDF取得＋抽出

```bash
uv run scripts/cli.py fetch \
  https://www.ssk.or.jp/seikyushiharai/tensuhyo/kihonmasta/index.files/master_1_20260331.pdf
```

- PDFファイル名の末尾 `YYYYMMDD` からバージョンを推定して `data/<version>/` に出力する。
- 既にダウンロード済みの場合はキャッシュ (`data/raw/`) を使う。再取得したい場合は `--force`。

### 一覧・検索

```bash
# 抽出済みのバージョン一覧
uv run scripts/cli.py versions

# あるバージョンに含まれるマスター一覧
uv run scripts/cli.py masters --version 20260331

# 医薬品マスターの全項目
uv run scripts/cli.py fields iyakuhin

# 項番14 (麻薬・毒薬・覚醒剤原料等) の詳細
uv run scripts/cli.py field iyakuhin 14

# 項番14のコード "3" が何を指すかを引く
uv run scripts/cli.py code iyakuhin 14 3

# キーワード検索（全マスター or --master-id で絞り込み）
uv run scripts/cli.py search 後発品
```

`--version` を省略すると **最新** （ディレクトリ名ソートで最後）のバージョンが使われる。

## 抽出マスター一覧

| master_id | マスター名 |
| --- | --- |
| `shoubyomei` | 傷病名マスター（旧傷病名管理ファイル） |
| `shushokugo` | 修飾語マスター |
| `shishiki` | 歯式マスター |
| `iyakuhin` | 医薬品マスター |
| `tokutei_kizai` | 特定器材マスター |
| `comment` | コメントマスター |
| `ika_shinryoukoui` | 医科診療行為マスター |
| `shika_shinryoukoui` | 歯科診療行為マスター (基本テーブル) |
| `shika_shinryoukoui_イ` 〜 `_コ` | 歯科診療行為マスターのサブテーブル |
| `chouzai_koui` | 調剤行為マスター |
| `houmon_kango` | 訪問看護療養費マスター |

## バージョン管理方針

- PDFファイル名の `YYYYMMDD`（例: `master_1_20260331.pdf` → `20260331`）をバージョンキーに採用。
- `data/<version>/` 以下をgit管理することで、改定の度にPRで差分をレビューできる。
- 生PDF (`data/raw/`) はキャッシュ扱いなのでリポジトリ管理の要否は運用で判断する
  （ファイルサイズは約1MB）。

## 既知の制限

PDFテーブル構造の性質上、以下のケースでは抽出結果が不完全になる可能性がある。
いずれも JSON を読めばコーディングエージェントが気づける程度の破綻で済むよう、
`description` には生テキストを残している。

- **項番の範囲表記 (例: 「10〜17」)** を展開できず、先頭の `10` のみが抽出される。
  1項番あたり複数のサブ項目を持つ構造（レセプト編集情報①〜④ × カラム位置/桁数）は
  部分抽出にとどまる。
- **pdfplumberが認識できないセル** （親項目名が sub_name列の外に配置されている等）
  は抽出漏れする。例: コメントマスターの項番6, 7（「コメント文」の 漢字有効桁数・漢字名称）。
- **別紙（補助コード表）** はまだ抽出対象外。
- `alias` など [`medi-xplorer`][mx] で使う独自メタデータは含まれないので、手動マージが必要。
- 欠落・不整合は `data/<version>/sections.debug.json` と `manifest.json` の `fieldCount` を
  過去バージョンと比較して検出できる。

抽出結果の検証は `data/<version>/sections.debug.json`（セクション検出ログ）を参照。
不具合を見つけた場合は、対応する `manifest.json` のページ範囲からPDFを当たって原因を特定する。

## 開発者向け

### 再抽出（PDFを変更せず JSON だけ作り直す）

```bash
uv run scripts/extract.py data/raw/master_1_20260331.pdf data/20260331
```

### 抽出ロジックの改善ポイント

- `scripts/extract.py` の `expand_parent_child()` が親行＋子行のサブ項目展開を担っている。
  サブ項目の分割（改行数ベース）で失敗するケースはここを調整する。
- `parse_codes()` はコード値（`N：名称`）の抽出。名称の折り返し判定で誤動作しがち。
