---
name: reseden-master-spec
description: 診療報酬制度「レセプト電算処理システム 基本マスターファイル仕様書」と別紙（施設基準コード一覧・名寄せコード一覧）を構造化 JSON で扱う reseden CLI の使い方。医薬品・医科/歯科診療行為・特定器材・コメント・傷病名等のマスター項目定義、および施設基準コードの逆引きに使用する。
---

# reseden-master-spec

診療報酬制度の以下2系統の PDF を構造化 JSON 化したものを、`reseden` CLI から引くガイド。

- **master** … 基本マスターファイル仕様書 (`master_0_*.pdf`) 由来。`fields[]` を持つ項目仕様。
- **codeTable** … 別紙 (`master_2_*.pdf`) 由来の補助コード辞書。
  - `kind=codeNamePairs`: 単純な (code, name) フラットリスト（例: 施設基準コード一覧）
  - `kind=nayoseGroups`: 名寄せ先 → 名寄せ元複数 のグループ構造（例: 名寄せコード一覧）

## 使い方の原則

1. 最初に `reseden info` を叩いて **同梱データの版・master/codeTable の ID 一覧・主要コマンドのヒント** を確認する。
2. JSON の構造が知りたければ `reseden schema` で master / codeTable / manifest のスキーマ概要が得られる。
3. **masterId / codeTableId / seq / code を憶測で書かない**。版によって増減するので、必要になった時点で
   `masters` / `codetables` → `fields --summary` / `codetable --limit N` → `field` / `codetable <id> <code>` と段階的に絞り込む。

## 主要コマンド

### master 系

- `reseden masters` — 全マスター一覧
- `reseden fields <masterId> --summary` — そのマスターの項番一覧
- `reseden field <masterId> <seq>` — 1 項目の詳細（codes 含む）
- `reseden code <masterId> <seq> <code>` — マスターの列挙コード値の名称を逆引き

### codeTable 系

- `reseden codetables` — 全 codeTable 一覧
- `reseden codetable <codeTableId> [--limit N]` — codeTable の全件 or 先頭 N 件
- `reseden codetable <codeTableId> <code>` — コード逆引き
  - `kind=codeNamePairs` ならコード → 名称
  - `kind=nayoseGroups` なら、コードが target でも source でも、属するグループを返す

### 横断

- `reseden search <keyword> [--scope master|codetable|all] [--master-id M] [--limit N]`
  — master と codeTable をまたぐキーワード検索（hit ごとに `type: master|codeTable`）
- `reseden verify` — 抽出結果の健全性チェック（exit 0 = OK / 1 = error）

各サブコマンドの詳細・使用例は `reseden <cmd> --help`。

## 語彙

- **seq** … master 仕様書でいう「項番」。1 始まり。サブ項目は同じ seq で複数レコードになる場合がある。
- **mode** … `numeric` / `alphanumeric` / `text` / `date` / null（項目の文字種）。
- **code** … 列挙値のキー。NFKC 正規化済みで比較されるので、全角 / 半角のどちらで渡しても一致する。
- **version** … 適用日（`YYYYMMDD`）。`--version` 省略時は最新が使われる。
- **kind** (codeTable のみ) … `codeNamePairs` か `nayoseGroups`。

## 出力と慣習

- すべてのサブコマンドは **UTF-8 JSON を stdout に返す**（`jq` で加工可能）。
- ヒント・警告は stderr に書かれるので、パイプしても混ざらない。
- 失敗時は非 0 終了コード + stderr に `error: ...` と次に叩くべきコマンド候補。

## 典型的な引き方

- 「医薬品マスターにどんな項目があるか見たい」:
  1. `reseden masters` で `iyakuhin` があることを確認
  2. `reseden fields iyakuhin --summary` で項番一覧
  3. 気になる seq を `reseden field iyakuhin <seq>` で詳細化
- 「マスター内のあるコード値が何を意味するか」:
  `reseden code <masterId> <seq> <code>`。コード一覧が不明なら先に `reseden field` でその項目を見る。
- 「施設基準コードから名称を引きたい」:
  業種ごとに codeTable が分かれている (`shisetsu_kijun` = 医科・歯科 / `shisetsu_kijun_chouzai` = 調剤 / `shisetsu_kijun_houmon_kango` = 訪問看護療養費)。
  どこにあるか分からない時は `reseden search <code> --scope codetable` で全 codeTable 横断検索。
- 「ある施設基準コードが名寄せ対象か知りたい」:
  `reseden codetable nayose <code>`。`match: source` が返れば名寄せ元、`match: target` なら名寄せ先。
- 「キーワードで関連項目を探したい」:
  `reseden search <keyword> --limit 10`。hit の `type` で master / codeTable のどちらでヒットしたかが分かる。

## 既知の制限

- 2 行にまたがる項目名の末尾が切れることがある（例: `検査等実施判断グルー` → 本来は `…グループ`）。`reseden verify` が `may be truncated mid-word` warning として検出する。
- master 側の `description` は PDF からの生テキストを保持しているので、レイアウト由来の余剰文字が混ざる場合がある（検索には耐えるが、そのまま表示に使う場合は要後処理）。
- 別紙の名寄せコード `note` 列も PDF レイアウト由来で、改行を空白除去した形で格納されている。
