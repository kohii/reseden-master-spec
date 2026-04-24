# reseden-master-spec

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

## ディレクトリ構成

```
reseden-master-spec/
├── src/reseden_master_spec/
│   ├── extract.py         # PDF → JSON 変換エンジン
│   ├── cli.py             # CLI エントリポイント（reseden コマンド）
│   └── text_supplement.py # pdftotext ベースのハイブリッド補完
├── data/
│   ├── raw/               # ダウンロード済みPDF (キャッシュ、非配布)
│   └── <YYYYMMDD>/        # 抽出結果（バージョン別）
│       ├── manifest.json
│       ├── <master>.json
│       └── sections.debug.json
├── pyproject.toml
└── uv.lock
```

## 使い方（配布版）

抽出済みJSONの一覧・検索だけしたい利用者向け。ローカルに clone 不要。
事前に [uv](https://docs.astral.sh/uv/getting-started/installation/) が必要。

### インストール

```bash
# グローバルに reseden コマンドを入れる（main 追従）
uv tool install 'git+https://github.com/kohii/reseden-master-spec'

# 初回のみ、シェルに PATH を通す
uv tool update-shell
```

特定バージョンに固定したい場合は URL 末尾に `@vX.Y.Z` を付ける。

### よく使うコマンド

```bash
reseden info                      # 同梱データ版・マスター一覧・主要コマンドガイド
reseden masters                   # 全マスター（masterId と fieldCount）
reseden fields iyakuhin --summary # seq/name/mode/maxBytes/hasCodes の簡略版
reseden field iyakuhin 14         # 項番14 の詳細（codes 含む）
reseden code iyakuhin 14 3        # 項番14 のコード値 "3" を引く
reseden search 後発品 --limit 10   # キーワード検索（どこでヒットしたか + snippet 付）
reseden schema                    # 出力 JSON のスキーマ概要
reseden verify                    # 抽出結果の健全性チェック（exit 0=OK / 1=error）
```

各サブコマンドの詳細と使用例は `reseden <cmd> --help`。

### 更新・削除

```bash
# 最新に追従
uv tool upgrade reseden-master-spec

# 現行を破棄して同じ URL で再インストール（ピン留めの付け外しに使う）
uv tool install --force 'git+https://github.com/kohii/reseden-master-spec'

# アンインストール
uv tool uninstall reseden-master-spec
```

### 単発実行（uvx）

インストールせず一度だけ試したいとき:

```bash
uvx --from 'git+https://github.com/kohii/reseden-master-spec' reseden info
```

### 補足

- 同梱される PDF 版は `reseden info` または `manifest.json` で確認できる。
- `--out-dir` 省略時のデータ解決順: `(1) CWD/data` に版があればそれ → `(2) パッケージ同梱 data/`
- `fetch`（PDFダウンロード→再抽出）は Poppler 等のビルド時依存が必要なので配布版での利用は想定していない。開発者向け参照。

## 開発者向け

### セットアップ

[uv](https://docs.astral.sh/uv/) と [Poppler](https://poppler.freedesktop.org/)（`pdftotext` 同梱）が必要。
pdfplumber で取りこぼした行を `pdftotext -layout` で補完するため。

```bash
# macOS の場合
brew install poppler

uv sync
```

### PDF取得＋抽出

```bash
uv run reseden fetch \
  https://www.ssk.or.jp/seikyushiharai/tensuhyo/kihonmasta/index.files/master_1_20260331.pdf
```

- PDFファイル名の末尾 `YYYYMMDD` からバージョンを推定して `data/<version>/` に出力する。
- 既にダウンロード済みの場合はキャッシュ (`data/raw/`) を使う。再取得したい場合は `--force`。

### ローカルで CLI を叩く

```bash
uv run reseden versions
uv run reseden masters --version 20260331
uv run reseden fields iyakuhin
uv run reseden field iyakuhin 14
uv run reseden code iyakuhin 14 3
uv run reseden search 後発品
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
| `houmon_kango` | 訪問看護療養費マスター (基本テーブル) |
| `houmon_kango_イ` 〜 `_オ` | 訪問看護療養費マスターのサブテーブル |

## バージョン管理方針

- PDFファイル名の `YYYYMMDD`（例: `master_1_20260331.pdf` → `20260331`）をバージョンキーに採用。
- `data/<version>/` 以下をgit管理することで、改定の度にPRで差分をレビューできる。
- 生PDF (`data/raw/`) はキャッシュ扱いなのでリポジトリ管理の要否は運用で判断する
  （ファイルサイズは約1MB）。

## 既知の制限

PDFテーブル構造の性質上、以下のケースでは抽出結果が不完全になる可能性がある。
いずれも JSON を読めばコーディングエージェントが気づける程度の破綻で済むよう、
`description` には生テキストを残している。

- **2行に跨る項目名の末尾切れ**（例: `検査等実施判断グルー` → 正しくは `…グループ`）。
  `pdftotext` 補完行でレイアウト由来の途中切れが残る。`verify` が `may be truncated mid-word`
  warning として検出する。
- **補完 field の `description` レイアウトノイズ**: pdftotext 由来の行は元PDFの
  空白レイアウト由来の残余（同一行内に隣接する別列の文字列など）がまれに含まれる。
  情報は保持されているため検索には耐えるが、そのまま表示に使う場合は後処理が必要。
- **別紙（補助コード表）** は本PDFに含まれず、別配布のため対象外。

### 品質チェック

`reseden verify` で抽出結果を機械検査する。
終了コードが `0` なら OK、`1` なら何らかの error を検出。

```bash
# 最新版を検査
uv run reseden verify

# 前版との差分（新規/削除マスター、fieldCount 変動）
uv run reseden verify --baseline 20260331
```

検出内容:
- seq 飛び（missing seq）— 絶対数 20 以上 or 全体の 20% 以上で error
- 項目名の空/改行/途中切れ疑い
- 認識できない mode 値
- コード名が 200 文字超（説明文の混入疑い）

### 再抽出（PDFを変更せず JSON だけ作り直す）

```bash
uv run python -m reseden_master_spec.extract data/raw/master_1_20260331.pdf data/20260331
```

### 抽出ロジックの改善ポイント

- `extract.expand_parent_child()` が親行＋子行のサブ項目展開を担っている。
  サブ項目の分割（改行数ベース）で失敗するケースはここを調整する。
- `extract.parse_codes()` はコード値（`N：名称`）の抽出。名称の折り返し判定で誤動作しがち。
- `extract._recover_ranged_fields()` は `10〜99` のような範囲ヘッダを検出して展開する。
  1孤立子行に全サブが乗らないケースは `text_supplement.find_range_subdefinitions()` の経路で救済している。
- `text_supplement` は `pdftotext -layout` で pdfplumber の取りこぼしを補完する
  ハイブリッド層。pdfplumber → pdftotext 補完 → テンプレート fallback の 3 段構え。

### 新しいバージョンの公開

1. 変更をコミット（PDF改定なら `uv run reseden fetch <URL>` で `data/<YYYYMMDD>/` を更新してコミット、ツール改修ならコードを修正してコミット）。
2. `uv run reseden verify` が `ok: true` で通ることを確認。
3. `pyproject.toml` と `src/reseden_master_spec/__init__.py` の `version` / `__version__` を semver で bump してコミット。
4. `git tag vX.Y.Z && git push --tags` でタグを打つ。
5. 利用者は `uvx --from 'git+https://github.com/kohii/reseden-master-spec@vX.Y.Z' reseden masters` で参照可能。
