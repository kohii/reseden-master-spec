---
name: reseden-master-spec
description: 診療報酬制度「レセプト電算処理システム 基本マスターファイル仕様書」の構造化 JSON を引く reseden CLI の使い方。医薬品・医科/歯科診療行為・特定器材・コメント・傷病名などレセ電マスターの項目定義やコード値を確認したい時に使用する。
---

# reseden-master-spec

診療報酬制度のレセ電マスター仕様書 PDF を構造化 JSON 化したものを、
`reseden` CLI から引くためのガイド。

## 使い方の原則

1. 最初に `reseden info` を叩いて **同梱データの版・マスター ID 一覧・主要コマンドのヒント** を確認する。
2. JSON の構造が知りたければ `reseden schema` で master / field / code / manifest のスキーマ概要が得られる。
3. **masterId / seq / code を憶測で書かない**。版によって増減するので、必要になった時点で `masters` → `fields --summary` → `field` と段階的に絞り込む。

## 主要コマンド

- `reseden masters` — 全マスター一覧（masterId / masterName / fieldCount / pages）
- `reseden fields <masterId> --summary` — そのマスターの項番一覧（seq / name / mode / maxBytes / hasCodes）
- `reseden field <masterId> <seq>` — 1 項目の詳細（codes 含む）
- `reseden code <masterId> <seq> <code>` — コード値の名称を逆引き
- `reseden search <keyword> [--master-id M] [--limit N]` — 全マスター横断のキーワード検索（name / shortName / description / code.name）
- `reseden verify` — 抽出結果の健全性チェック（exit 0 = OK / 1 = error）

各サブコマンドの詳細・使用例は `reseden <cmd> --help`。

## 語彙

- **seq** … 仕様書でいう「項番」。1 始まり。サブ項目は同じ seq で複数レコードになる場合がある。
- **mode** … `numeric` / `alphanumeric` / `text` / `date` / null のいずれか（項目の文字種）。
- **code** … 列挙値のキー。NFKC 正規化済みで比較されるので、全角 / 半角のどちらで渡しても一致する。
- **version** … PDF の改定日付（`YYYYMMDD`）。`--version` 省略時は最新が使われる。

## 出力と慣習

- すべてのサブコマンドは **UTF-8 JSON を stdout に返す**（`jq` で加工可能）。
- ヒント・警告は stderr に書かれるので、パイプしても混ざらない。
- 失敗時は非 0 終了コード + stderr に `error: ...` と次に叩くべきコマンド候補。

## 典型的な引き方

- 「医薬品マスターにどんな項目があるか見たい」:
  1. `reseden masters` で `iyakuhin` があることを確認
  2. `reseden fields iyakuhin --summary` で項番一覧
  3. 気になる seq を `reseden field iyakuhin <seq>` で詳細化
- 「あるコード値が何を意味するか」:
  `reseden code <masterId> <seq> <code>`。コード一覧が分からなければ先に `reseden field` でその項目を見る。
- 「キーワードで関連項目を探したい」:
  `reseden search <keyword> --limit 10`。`where` フィールドでどの属性（name/shortName/description/code）でヒットしたかが分かる。

## 既知の制限

- 2 行にまたがる項目名の末尾が切れることがある（例: `検査等実施判断グルー` → 本来は `…グループ`）。`reseden verify` が `may be truncated mid-word` warning として検出する。
- `description` は PDF からの生テキストを保持しているので、レイアウト由来の余剰文字が混ざる場合がある（検索には耐えるが、そのまま表示に使う場合は要後処理）。
- 別紙（補助コード表）は本仕様書 PDF に含まれず対象外。
