"""PDFからマスター仕様のテーブルを抽出し、JSONに変換する。

使い方:
    python -m reseden_master_spec.extract <pdf_path> <output_dir>

出力:
    <output_dir>/
      manifest.json              # バージョン情報・マスター一覧
      <master_id>.json           # 各マスターのフィールド定義
      sections.debug.json        # マスター境界・ページ範囲デバッグ情報
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pdfplumber

from . import text_supplement

EXTRACTOR_VERSION = "0.1.0"

# 全角数字→半角
ZEN2HAN_DIGIT = str.maketrans("０１２３４５６７８９", "0123456789")
# カタカナ「ﾓｰﾄﾞ」パターン等、ヘッダ検出用に正規化
WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class MasterSection:
    """マスター1つ分のセクション（ページ範囲）"""

    master_id: str  # 例: "iyakuhin"
    master_name: str  # 例: "医薬品マスター"
    sub_name: str | None  # 歯科診療行為の「基本テーブル」等
    number_label: str  # "（１）", "ア"など目次記号
    start_page: int  # 1-indexed
    end_page: int | None = None  # 1-indexed, inclusive (未確定なら None)


# 目次に表れるマスター見出し表記: 例 "（１） 傷病名マスター"
# 「（N）」＋非空白＋「マスター」までを非欲張りで拾う（後続の注記「（旧...）」等は無視）
MASTER_HEADING_RE = re.compile(
    r"^[（(]\s*([０-９0-9]+)\s*[）)]\s*([^\s（(]+?マスター)"
)
# 歯科のサブテーブル見出し: 例 "ア 基本テーブル"
SUBTABLE_HEADING_RE = re.compile(r"^([アイウエオカキクケコサシスセソタチツテト])\s+(.+?テーブル.*?)$")


# マスター日本語名 → ファイル名用の識別子
MASTER_ID_MAP: dict[str, str] = {
    "傷病名マスター": "shoubyomei",
    "修飾語マスター": "shushokugo",
    "歯式マスター": "shishiki",
    "医薬品マスター": "iyakuhin",
    "特定器材マスター": "tokutei_kizai",
    "コメントマスター": "comment",
    "医科診療行為マスター": "ika_shinryoukoui",
    "歯科診療行為マスター": "shika_shinryoukoui",
    "調剤行為マスター": "chouzai_koui",
    "訪問看護療養費マスター": "houmon_kango",
}

# サブテーブル（「イ 〜テーブル」等の見出しで区切られる）を持つマスター
_SUBTABLE_ENABLED_MASTERS = frozenset(
    {
        "歯科診療行為マスター",
        "訪問看護療養費マスター",
    }
)


def normalize_str(s: str | None) -> str:
    if s is None:
        return ""
    # NFKC で半角カナ(ﾊﾞｲﾄ)↔全角(バイト) などを統一し、空白は全削除
    return WHITESPACE_RE.sub("", unicodedata.normalize("NFKC", s))


def header_signature(row: list[str | None]) -> str:
    """ヘッダー行をキーワード抽出してシグネチャ化"""
    return "|".join(normalize_str(c) for c in row)


def is_header_row(row: list[str | None]) -> bool:
    sig = header_signature(row)
    return "項番" in sig and "項目名" in sig and "内容" in sig


def is_sub_header_row(row: list[str | None]) -> bool:
    sig = header_signature(row)
    return "ﾓｰﾄﾞ" in sig or "モード" in sig


# ---------------------------------------------------------------------------
# セクション検出
# ---------------------------------------------------------------------------


def detect_sections(pdf: pdfplumber.PDF) -> list[MasterSection]:
    """各ページの先頭テキストから、マスターのセクション（開始ページ）を検出する。"""
    sections: list[MasterSection] = []
    current_master: MasterSection | None = None
    for page_idx, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            m = MASTER_HEADING_RE.match(line)
            if m:
                number_label = f"（{m.group(1)}）"
                master_name = m.group(2).strip()
                # 「目次」ページは先頭にある。項番1つにつき複数回見つけるが、
                # 各マスターの開始ページは「項番」「項目名」を含む表が存在するページ。
                master_id = MASTER_ID_MAP.get(master_name)
                if master_id is None:
                    continue
                current_master = MasterSection(
                    master_id=master_id,
                    master_name=master_name,
                    sub_name=None,
                    number_label=number_label,
                    start_page=page_idx + 1,
                )
                sections.append(current_master)
                break  # このページの他行はスキップ（先頭のマスター見出しのみ拾う）
            sm = SUBTABLE_HEADING_RE.match(line)
            if sm and current_master and current_master.master_name in _SUBTABLE_ENABLED_MASTERS:
                # サブテーブル開始。親マスター配下のサブテーブルとして追加。
                # 先頭「ア 基本テーブル」は MASTER_HEADING と同じページに現れ、
                # そのページは MASTER_HEADING 検出で break するため自然に無視される
                # （＝ `ア` は親マスター本体として扱われる）。
                sub_id = f"{current_master.master_id}_{sm.group(1)}"
                sections.append(
                    MasterSection(
                        master_id=sub_id,
                        master_name=current_master.master_name,
                        sub_name=sm.group(2).strip(),
                        number_label=sm.group(1),
                        start_page=page_idx + 1,
                    )
                )
                break
    # end_pageを後から埋める: 次セクションの開始ページ-1。最後は pdf.pages最終に設定しない
    # （実際は「別紙」のページが続くため、マスターのテーブルが表現されるページだけ有効）
    for i, sec in enumerate(sections):
        next_start = sections[i + 1].start_page if i + 1 < len(sections) else None
        if next_start is not None:
            sec.end_page = next_start - 1
        else:
            # 最終マスターは「別紙」開始前まで。別紙は「別紙」文字列で開始。
            last_page = _find_bessi_start(pdf, sec.start_page)
            sec.end_page = last_page - 1 if last_page else len(pdf.pages)
    # 目次ページ（最初の2-3ページ）に該当する「擬似セクション」を除外する：
    # セクションが同一master_idで重複した場合、後ろに現れたものを有効とする
    # しかし目次は複数のマスターが1ページに全部並んでいるので「break」で最初の1件だけ拾っている。
    # 目次ページ側ではヘッダ行(項番/項目名/内容)がないので、後続処理でそのページを無視する流れでOK。
    # ただし start_page が目次ページを指している場合、end_page が次の目次行指定値 -1 となり範囲が不正になる。
    # -> 目次ページのセクションはいずれも表テーブルを含まないので、extract時に無視する。
    return sections


def _find_bessi_start(pdf: pdfplumber.PDF, after_page: int) -> int | None:
    """after_page以降で、`別紙` で始まるページを探す"""
    for page_idx in range(after_page, len(pdf.pages)):
        text = pdf.pages[page_idx].extract_text() or ""
        first_line = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
        if first_line.startswith("別紙"):
            return page_idx + 1
    return None


# ---------------------------------------------------------------------------
# テーブル抽出
# ---------------------------------------------------------------------------


@dataclass
class ColumnSchema:
    """ページ内テーブルの列マッピング"""

    seq: int = -1
    name: int = -1
    sub_name: int | None = None
    mode: int = -1
    max_bytes: int = -1
    item_format: int = -1
    content: int = -1
    flag_columns: list[tuple[int, str]] = field(default_factory=list)


def resolve_columns(header_row: list[str | None], sub_header_row: list[str | None]) -> ColumnSchema:
    """ヘッダ2行を基に各列の意味を判別する。"""
    norm = [normalize_str(c) for c in header_row]
    norm2 = [normalize_str(c) for c in sub_header_row]
    schema = ColumnSchema()
    for i, cell in enumerate(norm):
        if cell == "項番":
            schema.seq = i
        elif cell == "項目名":
            schema.name = i
        elif cell == "内容":
            schema.content = i
        elif cell and cell != "形式":
            # "共通区分" や "※注1" のようなフラグ列
            schema.flag_columns.append((i, cell))
    for i, cell in enumerate(norm2):
        # NFKC正規化済み。"ﾓｰﾄﾞ" → "モード", "ﾊﾞｲﾄ" → "バイト"
        if cell == "モード":
            schema.mode = i
        elif cell == "最大バイト":
            schema.max_bytes = i
        elif cell == "項目形式":
            schema.item_format = i
    # sub_name 列: name と mode の間に空でない列がある場合
    if schema.mode > schema.name + 1:
        # name+1 〜 mode-1 にサブ項目名列がある可能性
        schema.sub_name = schema.name + 1
    return schema


def extract_table_rows(page: pdfplumber.page.Page) -> list[dict[str, Any]]:
    """1ページから項目行を抽出する。"""
    tables = page.extract_tables()
    records: list[dict[str, Any]] = []
    for table in tables:
        if len(table) < 3:
            continue
        # ヘッダ行を検出
        header_idx = None
        sub_header_idx = None
        for i, row in enumerate(table[:3]):
            if is_header_row(row):
                header_idx = i
            elif header_idx is not None and sub_header_idx is None and is_sub_header_row(row):
                sub_header_idx = i
                break
        if header_idx is None:
            continue
        # sub_headerがない場合(稀)はheader_idx+1をsub扱いに
        if sub_header_idx is None:
            sub_header_idx = header_idx + 1
        schema = resolve_columns(table[header_idx], table[sub_header_idx])
        for row in table[sub_header_idx + 1 :]:
            rec = _row_to_record(row, schema)
            if rec:
                records.append(rec)
    return records


def _get(row: list[str | None], idx: int) -> str:
    if idx < 0 or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _row_to_record(row: list[str | None], schema: ColumnSchema) -> dict[str, Any] | None:
    seq_raw = _get(row, schema.seq)
    name = _get(row, schema.name)
    sub_name = _get(row, schema.sub_name) if schema.sub_name is not None else ""
    mode = _get(row, schema.mode)
    max_bytes = _get(row, schema.max_bytes)
    item_format = _get(row, schema.item_format)
    content = _get(row, schema.content)
    flags = {label: _get(row, idx) for idx, label in schema.flag_columns}
    # 完全に空の行を無視
    if not (seq_raw or name or sub_name or mode or max_bytes or item_format or content):
        return None
    return {
        "seq_raw": seq_raw,
        "name": name,
        "sub_name": sub_name,
        "mode_raw": mode,
        "max_bytes_raw": max_bytes,
        "item_format_raw": item_format,
        "content": content,
        "flags": flags,
    }


def _split_lines(s: str) -> list[str]:
    """文字列を改行で分割して空要素を除く"""
    if not s:
        return []
    return [line.strip() for line in s.split("\n") if line.strip()]


def _normalize_mode_lines(lines: list[str]) -> list[str]:
    """mode列の "英数"+"カナ" のように分割された行を "英数カナ" に統合する"""
    result: list[str] = []
    for line in lines:
        if line == "カナ" and result:
            result[-1] = result[-1] + "カナ"
        else:
            result.append(line)
    return result


# 丸数字 "①〜⑩" "⑪〜⑳"
_CIRCLED_DIGITS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _resolve_seq_range(seq_lines: list[str]) -> tuple[int, int] | None:
    """["72","～","81"] や ["10","〜","17"] を (72, 81) に解釈。失敗時None。"""
    flat = " ".join(seq_lines)
    # 改行/空白を除いた "N～M" の形を検出
    m = re.search(r"(\d+)\s*[～〜~\-]\s*(\d+)", to_half_digits(flat))
    if not m:
        return None
    start, end = int(m.group(1)), int(m.group(2))
    if 0 < start <= end:
        return (start, end)
    return None


def _extract_group_labels(parent_name: str, expected_count: int) -> list[str]:
    """親nameに "①〜⑩" "１～１０" のような繰返しラベル表現があれば、
    expected_count 分のラベルリストを返す。なければ空リスト。"""
    if not parent_name:
        return []
    # 丸数字
    m = re.search(r"([①-⑳])\s*[～〜~\-]\s*([①-⑳])", parent_name)
    if m:
        start_idx = _CIRCLED_DIGITS.index(m.group(1))
        end_idx = _CIRCLED_DIGITS.index(m.group(2))
        if end_idx - start_idx + 1 == expected_count:
            return list(_CIRCLED_DIGITS[start_idx : end_idx + 1])
        return []
    # 数字ラベル (1〜10 / １～１０)
    m = re.search(r"([0-9０-９]+)\s*[～〜~\-]\s*([0-9０-９]+)", parent_name)
    if m:
        a = int(to_half_digits(m.group(1)))
        b = int(to_half_digits(m.group(2)))
        if a < b and b - a + 1 == expected_count:
            return [str(i) for i in range(a, b + 1)]
    return []


def _strip_group_label_notation(name: str) -> str:
    """親name中の "①〜⑩" や "１～１０" 表記を除去する。"""
    name = re.sub(r"[①-⑳]\s*[～〜~\-]\s*[①-⑳]", "", name)
    name = re.sub(r"[0-9０-９]+\s*[～〜~\-]\s*[0-9０-９]+", "", name)
    return name.strip()


def _pick_or_last(lst: list[str], i: int) -> str:
    if not lst:
        return ""
    return lst[i] if i < len(lst) else lst[-1]


def expand_parent_child(raw_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """親行＋子行をマージし、サブ項目を展開したフラットなレコード列にする。

    PDF表の規則性:
    - 親行: seq列のみ埋まる / nameがメイン項目名（ただし子行の情報が混入している場合あり）
    - 子行: seqは空、sub_name列(index name+1)に複数のサブ項目名が改行区切りで入る。
      mode/max_bytes/item_format もそれぞれ改行区切り。
    """
    merged: list[dict[str, Any]] = []
    pending_parent: dict[str, Any] | None = None

    def flush_parent():
        nonlocal pending_parent
        if pending_parent is not None:
            # サブ項目なしの親。name列が複数行なら折返しとみなして結合する
            # (「薬価基準収載医薬品コ\nード」のような2行に渡る項目名の復元)
            p = dict(pending_parent)
            if p["name"] and "\n" in p["name"]:
                p["name"] = "".join(p["name"].split("\n"))
            merged.append(p)
            pending_parent = None

    def expand_self_contained(rec: dict[str, Any]) -> None:
        """親行なしで seq/sub_name/mode が改行積みされている行を展開してmergedへ追加。

        例: ページ跨ぎで前ページの親項目の子行だけが今ページ先頭に来るケースや、
        seq_raw="8\\n9" のように1行に複数項目が詰まっているケース。
        """
        sub_names = _split_lines(rec["sub_name"]) or _split_lines(rec["name"])
        modes = _normalize_mode_lines(_split_lines(rec["mode_raw"]))
        bytes_list = _split_lines(rec["max_bytes_raw"])
        formats = _split_lines(rec["item_format_raw"])
        seqs = _split_lines(rec["seq_raw"])
        n = max(len(sub_names), len(modes), len(bytes_list), len(formats), len(seqs))
        if n <= 1:
            merged.append(rec)
            return

        for i in range(n):
            merged.append(
                {
                    "seq_raw": _pick_or_last(seqs, i),
                    "name": "",
                    "sub_name": _pick_or_last(sub_names, i),
                    "mode_raw": _pick_or_last(modes, i),
                    "max_bytes_raw": _pick_or_last(bytes_list, i),
                    "item_format_raw": _pick_or_last(formats, i),
                    "content": rec["content"] if i == 0 else "",
                    "flags": rec.get("flags") or {},
                }
            )

    for rec in raw_records:
        seq_raw = rec["seq_raw"]
        sub_name = rec["sub_name"]

        is_child_row = not seq_raw.strip() and bool(sub_name.strip())
        # seqもsub_nameも改行含みなら自己完結型複合行とみなす
        is_multi_row = (
            "\n" in seq_raw
            and ("\n" in sub_name or "\n" in rec["mode_raw"])
        )

        if is_child_row:
            # 親とペアリング
            if pending_parent is None:
                # 親なしの孤立子行 → そのまま（前のページからの続きなど）
                merged.append(rec)
                continue
            # サブ項目展開
            sub_names = _split_lines(sub_name)
            modes = _normalize_mode_lines(_split_lines(rec["mode_raw"]))
            bytes_list = _split_lines(rec["max_bytes_raw"])
            formats = _split_lines(rec["item_format_raw"])
            # 子行の列が空のときは親行から継承
            parent_modes = _normalize_mode_lines(_split_lines(pending_parent["mode_raw"]))
            parent_bytes = _split_lines(pending_parent["max_bytes_raw"])
            parent_formats = _split_lines(pending_parent["item_format_raw"])
            if not modes and parent_modes:
                modes = parent_modes
            if not bytes_list and parent_bytes:
                bytes_list = parent_bytes
            if not formats and parent_formats:
                formats = parent_formats
            # 親行から seq リストを取得。親行にseqが "4\n5\n6\n7" のように積まれていたらそのまま
            parent_seqs_raw = _split_lines(pending_parent["seq_raw"])
            parent_name = (pending_parent["name"] or "").split("\n")[0].strip()

            child_content = rec["content"]
            parent_content = pending_parent["content"]
            combined_content = "\n".join(
                c for c in (parent_content, child_content) if c
            ).strip()

            k = max(len(sub_names), len(modes), len(bytes_list), len(formats))

            seq_range = _resolve_seq_range(parent_seqs_raw)
            if seq_range is not None and k > 0:
                start, end = seq_range
                n_total = end - start + 1
                # N×M 展開: n_total が k の倍数なら繰返し構造とみなす
                if n_total % k == 0 and k > 0:
                    repeat = n_total // k
                    labels = _extract_group_labels(pending_parent["name"], repeat)
                    base_name = (
                        _strip_group_label_notation(pending_parent["name"].split("\n")[0])
                        if pending_parent["name"]
                        else ""
                    )
                    for r in range(repeat):
                        label = labels[r] if labels else ""
                        group_name = (base_name + label) if base_name else (parent_name + label)
                        for s in range(k):
                            seq_val = start + r * k + s
                            merged.append(
                                {
                                    "seq_raw": str(seq_val),
                                    "name": group_name,
                                    "sub_name": _pick_or_last(sub_names, s),
                                    "mode_raw": _pick_or_last(modes, s),
                                    "max_bytes_raw": _pick_or_last(bytes_list, s),
                                    "item_format_raw": _pick_or_last(formats, s),
                                    "content": combined_content if (r == 0 and s == 0) else "",
                                    "flags": rec.get("flags") or {},
                                }
                            )
                    pending_parent = None
                    continue

            seqs_for_subs: list[str]
            if len(parent_seqs_raw) == k:
                seqs_for_subs = parent_seqs_raw
            elif len(parent_seqs_raw) == 1:
                # 親seqが1つしかない（例: 項番3「医薬品コード」のサブが区分・番号）
                seqs_for_subs = [parent_seqs_raw[0]] * k
            else:
                if parent_seqs_raw:
                    seqs_for_subs = [parent_seqs_raw[0]] * k
                else:
                    seqs_for_subs = [""] * k

            for i in range(k):
                merged.append(
                    {
                        "seq_raw": seqs_for_subs[i] if i < len(seqs_for_subs) else "",
                        "name": parent_name,
                        "sub_name": _pick_or_last(sub_names, i),
                        "mode_raw": _pick_or_last(modes, i),
                        "max_bytes_raw": _pick_or_last(bytes_list, i),
                        "item_format_raw": _pick_or_last(formats, i),
                        "content": combined_content if i == 0 else "",
                        "flags": rec.get("flags") or {},
                    }
                )
            pending_parent = None
        elif is_multi_row:
            # 自己完結型複合行 (seq列にもsub_name列にも改行が積まれている)
            flush_parent()
            expand_self_contained(rec)
        else:
            # 通常の親行 or 独立項目行
            flush_parent()
            pending_parent = rec

    flush_parent()
    return merged


# ---------------------------------------------------------------------------
# 正規化 (raw records → フィールド定義)
# ---------------------------------------------------------------------------


MODE_MAP = {
    "数字": "numeric",
    "英数": "alphanumeric",
    "英数カナ": "alphanumeric",
    "漢字": "text",
    "カナ": "text",
    "": "",
}


def to_half_digits(s: str) -> str:
    return s.translate(ZEN2HAN_DIGIT)


def parse_seq(seq_raw: str) -> int | None:
    s = to_half_digits(seq_raw).strip()
    if not s:
        return None
    # "10", "10～17" など。先頭の数字のみ
    m = re.match(r"^(\d+)", s)
    return int(m.group(1)) if m else None


def parse_max_bytes(raw: str) -> dict[str, Any]:
    s = to_half_digits(raw).strip()
    if not s:
        return {}
    # "(2)" のような丸括弧付きはサブ項目
    m = re.match(r"^[（(](\d+)[）)]$", s)
    if m:
        return {"max_bytes": int(m.group(1)), "sub": True}
    m = re.match(r"^(\d+)$", s)
    if m:
        return {"max_bytes": int(m.group(1))}
    return {"max_bytes_raw": s}


CODE_LINE_RE = re.compile(r"^([0-9０-９A-Za-zＡ-Ｚａ-ｚ]{1,3})[:：]\s*(.+?)\s*$")
# 説明文の始まりを示す接頭語（コード名称の折返しとは区別して終端する）
_NON_CODE_PREFIX_RE = re.compile(
    r"^("
    r"[「『（(＜<※]"
    r"|なお|また|ただし|ただ|その|さらに|そして|つまり|したがって|これらの|ただ|注\s|注[0-9０-９]"
    r"|項番|漢字[:：]|半角|全角|該当|対象|コメント|点数|金額|詳細"
    r"|例\s|例[0-9０-９]|未使用|使用しない|設定|算定|以下の|以上の|任意"
    r"|参照|参考|備考|出力|入力|記載|記録|返戻"
    r")"
)
# コード名称の最大長（これを超える連結は説明文の混入とみなして打ち切る）
_CODE_NAME_MAX_LEN = 80


def normalize_code(raw: str) -> str:
    """コード値を NFKC 正規化し前後空白を除去する。抽出・検索で共通のキーとして使う。"""
    return unicodedata.normalize("NFKC", raw).strip()


def parse_codes(content: str) -> list[dict[str, str]]:
    """内容欄から「N:名称」形式のコードを抽出する。

    - 各行単位で判定。
    - コード名称の折返し (名称が"。"等で終わらない場合の続き) のみ連結する。
    - 空行、または「なお」「「」等で始まる行はコードの説明文とみなして終端する。
    """
    if not content:
        return []
    lines = content.split("\n")
    codes: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    def finish():
        nonlocal current
        if current is not None:
            current["name"] = current["name"].strip()
            codes.append(current)
            current = None

    for raw_line in lines:
        line = raw_line.strip()
        m = CODE_LINE_RE.match(line)
        if m:
            finish()
            code_raw, name = m.group(1), m.group(2)
            current = {"code": normalize_code(code_raw), "name": name.strip()}
            continue
        if current is None:
            continue
        if not line:
            finish()
            continue
        # 直前のコード名称が文として完結している → 説明文に入ったので終端
        if current["name"].rstrip().endswith(("。", "．")):
            finish()
            continue
        # 説明文らしい接頭語で始まる → 終端
        if _NON_CODE_PREFIX_RE.match(line):
            finish()
            continue
        # 既に名称が十分長いのにまだ追加しようとしている → 説明文混入とみなして終端
        if len(current["name"]) >= _CODE_NAME_MAX_LEN:
            finish()
            continue
        current["name"] = current["name"] + line

    finish()
    # 2個以上のコードが抽出できた場合のみ採用（説明文中の偶然のコロン一致を排除）
    return codes if len(codes) >= 2 else []


def normalize_records(raw_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """rawレコードを medi-xplorer スキーマ互換の形式に変換する。"""
    # 親行＋子行を先に統合
    expanded = expand_parent_child(raw_records)
    # 連続する同じ項番を持つ行（サブ項目）を親にまとめる
    merged: list[dict[str, Any]] = []
    for rec in expanded:
        seq = parse_seq(rec["seq_raw"])
        if seq is None and merged:
            # 項番なし行: 前のレコードに contentを連結（多ページに跨る項目の続き）
            last = merged[-1]
            last["content"] = (last["content"] + "\n" + rec["content"]).strip()
            continue
        merged.append({**rec, "seq": seq})

    fields_out: list[dict[str, Any]] = []
    for rec in merged:
        seq = rec["seq"]
        if seq is None:
            continue
        name = rec["name"]
        sub_name = rec["sub_name"]
        full_name = name
        if sub_name:
            full_name = f"{name}/{sub_name}" if name else sub_name
        content = rec["content"]
        mb_info = parse_max_bytes(rec["max_bytes_raw"])
        # mode の改行（英数\nカナ 等）を結合してから enum lookup
        mode_normalized = "".join(_normalize_mode_lines(_split_lines(rec["mode_raw"]))) if rec["mode_raw"] else ""
        mode = MODE_MAP.get(mode_normalized, mode_normalized) or None
        codes = parse_codes(content)
        entry: dict[str, Any] = {"seq": seq, "name": full_name}
        if sub_name and name:
            entry["shortName"] = sub_name
        if mode:
            entry["mode"] = mode
        if "max_bytes" in mb_info:
            entry["maxBytes"] = mb_info["max_bytes"]
        if rec["item_format_raw"]:
            entry["itemFormat"] = rec["item_format_raw"]
        if content:
            entry["description"] = content
        if codes:
            entry["codes"] = codes
        flags = {k: v for k, v in (rec.get("flags") or {}).items() if v}
        if flags:
            entry["flags"] = flags
        fields_out.append(entry)

    # 同じseqが複数回（サブ項目+親）入るケースを処理:
    # 既に "full_name" でスラッシュ結合しているのでそのままでOK
    return fields_out


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------


def _detect_missing_seqs(fields: list[dict[str, Any]]) -> set[int]:
    """連続する項番列の中で欠落している seq を返す。"""
    seqs = sorted({f["seq"] for f in fields})
    if not seqs:
        return set()
    return set(range(seqs[0], seqs[-1] + 1)) - set(seqs)


def _detect_defective_seqs(fields: list[dict[str, Any]]) -> set[int]:
    """補完したい seq 集合: 欠落 seq + name が空の seq"""
    defective = _detect_missing_seqs(fields)
    for f in fields:
        if not f.get("name"):
            defective.add(f["seq"])
    return defective


def _find_orphan_children(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """孤立子行（seq空・sub_name複数） を返す"""
    return [
        rec
        for rec in raw
        if not rec["seq_raw"].strip() and rec["sub_name"].strip()
    ]


def _recover_ranged_fields(
    raw: list[dict[str, Any]],
    ranges: list[dict[str, Any]],
    defective_seqs: set[int],
    existing_fields: list[dict[str, Any]] | None = None,
    pdf_path: Path | None = None,
    section_start: int | None = None,
    section_end: int | None = None,
) -> list[dict[str, Any]]:
    """範囲ヘッダ + 孤立子行 から N×M 展開された field entries を生成する。

    existing_fields が与えられた場合、孤立子が見つからないときのフォールバックとして
    範囲内に存在する既存フィールド (たとえば終端付近のみ拾われたもの) をテンプレート
    として利用する。
    """
    produced: list[dict[str, Any]] = []
    existing_by_seq: dict[int, dict[str, Any]] = {
        f["seq"]: f for f in (existing_fields or [])
    }
    for rng in ranges:
        span_seqs = set(range(rng["start"], rng["end"] + 1))
        # span のうち defective が過半数を占める場合に展開対象とする
        defective_in_span = span_seqs & defective_seqs
        if len(defective_in_span) < max(1, len(span_seqs) // 2):
            continue
        # 範囲ヘッダから ①〜④ などのラベルを拾い、繰返し回数を決定
        n_total = rng["end"] - rng["start"] + 1
        children = _find_orphan_children(raw)
        best_child = None
        for child in children:
            sub_count = len(_split_lines(child["sub_name"]))
            if sub_count > 0 and n_total % sub_count == 0:
                # repeat 回数とラベル数が一致すれば採用
                repeat = n_total // sub_count
                lbs = _extract_group_labels(rng["name"], repeat)
                if lbs:
                    best_child = (child, sub_count, repeat, lbs)
                    break
        if best_child is None:
            # フォールバック1: pdftotext から range 内の 1 cycle 分サブ定義を拾う
            if pdf_path is not None and section_start and section_end:
                subs_text = text_supplement.find_range_subdefinitions(
                    pdf_path, section_start, section_end, rng
                )
                if subs_text and len(subs_text) >= 2 and n_total % len(subs_text) == 0:
                    k_text = len(subs_text)
                    repeat_text = n_total // k_text
                    labels_text = _extract_group_labels(rng["name"], repeat_text)
                    if labels_text:
                        base_name = _strip_group_label_notation(rng["name"])
                        for r in range(repeat_text):
                            label = labels_text[r]
                            group_name = base_name + label
                            for s, sub in enumerate(subs_text):
                                seq_val = rng["start"] + r * k_text + s
                                mode = MODE_MAP.get(sub["mode"], sub["mode"]) or None
                                mb = parse_max_bytes(sub["bytes"])
                                entry: dict[str, Any] = {
                                    "seq": seq_val,
                                    "name": f"{group_name}/{sub['name']}",
                                    "shortName": sub["name"],
                                }
                                if mode:
                                    entry["mode"] = mode
                                if "max_bytes" in mb:
                                    entry["maxBytes"] = mb["max_bytes"]
                                if sub["fmt"]:
                                    entry["itemFormat"] = sub["fmt"]
                                if r == 0 and sub.get("content"):
                                    entry["description"] = sub["content"]
                                entry["_source"] = "range+text-subs"
                                produced.append(entry)
                        continue

            # フォールバック2: 既存フィールドを template にして子行無しで展開
            template_seq = next(
                (s for s in span_seqs if s in existing_by_seq), None
            )
            if template_seq is None:
                continue
            template = existing_by_seq[template_seq]
            labels = _extract_group_labels(rng["name"], n_total)
            if not labels:
                continue
            repeat = n_total
            k = 1
            # template を各 seq に複製する
            base_name = _strip_group_label_notation(rng["name"])
            template_short = template.get("shortName") or template.get("name") or ""
            for r in range(repeat):
                label = labels[r]
                group_name = base_name + label
                seq_val = rng["start"] + r
                entry: dict[str, Any] = {
                    "seq": seq_val,
                    "name": f"{group_name}/{template_short}",
                    "shortName": template_short,
                }
                for key in ("mode", "maxBytes", "itemFormat"):
                    if key in template:
                        entry[key] = template[key]
                if r == 0 and rng.get("description"):
                    entry["description"] = rng["description"]
                entry["_source"] = "range+template"
                produced.append(entry)
            continue
        child, k, repeat, labels = best_child
        sub_names = _split_lines(child["sub_name"])
        modes = _normalize_mode_lines(_split_lines(child["mode_raw"]))
        bytes_list = _split_lines(child["max_bytes_raw"])
        formats = _split_lines(child["item_format_raw"])
        base_name = _strip_group_label_notation(rng["name"])
        content = child["content"] or rng.get("description", "")
        for r in range(repeat):
            label = labels[r] if r < len(labels) else ""
            group_name = base_name + label
            for s in range(k):
                seq_val = rng["start"] + r * k + s
                entry: dict[str, Any] = {
                    "seq": seq_val,
                    "name": f"{group_name}/{_pick_or_last(sub_names, s)}"
                    if sub_names
                    else group_name,
                    "shortName": _pick_or_last(sub_names, s) or None,
                }
                if entry["shortName"] is None:
                    entry.pop("shortName")
                mode_lookup = _pick_or_last(modes, s)
                mode = MODE_MAP.get(mode_lookup, mode_lookup) or None
                if mode:
                    entry["mode"] = mode
                mb = parse_max_bytes(_pick_or_last(bytes_list, s))
                if "max_bytes" in mb:
                    entry["maxBytes"] = mb["max_bytes"]
                fmt = _pick_or_last(formats, s)
                if fmt:
                    entry["itemFormat"] = fmt
                if r == 0 and s == 0 and content:
                    entry["description"] = content
                entry["_source"] = "range+text"
                produced.append(entry)
    return produced


def _to_field_entry(rec: dict[str, Any]) -> dict[str, Any]:
    """text_supplement由来のrawレコードを medi-xplorer互換 field entry に変換する。"""
    mode = MODE_MAP.get(rec["mode_raw"], rec["mode_raw"]) or None
    mb_info = parse_max_bytes(rec["max_bytes_raw"])
    content = rec.get("content", "")
    codes = parse_codes(content)
    entry: dict[str, Any] = {"seq": rec["seq"], "name": rec["name"]}
    if mode:
        entry["mode"] = mode
    if "max_bytes" in mb_info:
        entry["maxBytes"] = mb_info["max_bytes"]
    if rec.get("item_format_raw"):
        entry["itemFormat"] = rec["item_format_raw"]
    if content:
        entry["description"] = content
    if codes:
        entry["codes"] = codes
    entry["_source"] = "text"
    return entry


def extract_master(
    pdf: pdfplumber.PDF,
    section: MasterSection,
    pdf_path: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """セクション範囲内のテーブルを抽出し、raw / 正規化結果 / ソース別件数 を返す。

    pdf_path が与えられた場合、pdfplumber で取りこぼした seq をpdftotext出力から補完する。
    戻り値の3つ目は {"pdfplumber": N, "range+text": M, ...} の集計（debug用）。
    """
    raw: list[dict[str, Any]] = []
    if section.end_page is None:
        return raw, [], {}
    for page_idx in range(section.start_page - 1, section.end_page):
        page = pdf.pages[page_idx]
        records = extract_table_rows(page)
        for rec in records:
            rec["_page"] = page_idx + 1
            raw.append(rec)
    normalized = normalize_records(raw)

    # 欠落 / name空 の seq を pdftotext で補完
    if pdf_path is not None and normalized:
        defective = _detect_defective_seqs(normalized)
        if defective:
            # まず、範囲表記ヘッダ + 孤立子行 のマージで N×M 展開を試す
            ranges = text_supplement.find_seq_ranges(
                pdf_path, section.start_page, section.end_page
            )
            range_recovered = _recover_ranged_fields(
                raw,
                ranges,
                defective,
                existing_fields=normalized,
                pdf_path=pdf_path,
                section_start=section.start_page,
                section_end=section.end_page,
            )
            if range_recovered:
                recovered_seqs = {f["seq"] for f in range_recovered}
                # 置換対象の既存 field (同 seq) を除去
                normalized = [f for f in normalized if f["seq"] not in recovered_seqs]
                normalized.extend(range_recovered)
                defective -= recovered_seqs

            supplemental = text_supplement.supplement_from_text(
                pdf_path, section.start_page, section.end_page, defective
            )
            if supplemental:
                supp_seqs = {r["seq"] for r in supplemental}
                # 補完できた seq の name空フィールドを除去して置換
                normalized = [
                    f for f in normalized if not (f["seq"] in supp_seqs and not f.get("name"))
                ]
                for rec in supplemental:
                    normalized.append(_to_field_entry(rec))
        # seq 順にソート (同seq内は pdfplumber由来を先に)
        normalized.sort(key=lambda f: (f["seq"], f.get("_source") is not None))

    # _source は内部デバッグ情報。公開JSONからは除去し件数だけdebug側に回す。
    source_counts: dict[str, int] = {}
    for f in normalized:
        src = f.pop("_source", "pdfplumber")
        source_counts[src] = source_counts.get(src, 0) + 1

    return raw, normalized, source_counts


def infer_version_from_path(pdf_path: Path) -> str:
    """PDFファイル名から版（YYYYMMDD）を推測する。"""
    m = re.search(r"(\d{8})", pdf_path.stem)
    if m:
        return m.group(1)
    return "unknown"


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pdftotext_version() -> str:
    """pdftotext のバージョン文字列を返す。無ければ空文字。"""
    exe = shutil.which("pdftotext")
    if not exe:
        return ""
    try:
        # pdftotext -v は stderr に出力する
        result = subprocess.run(
            [exe, "-v"], capture_output=True, text=True, check=False
        )
        out = (result.stderr or result.stdout or "").strip()
        first = out.splitlines()[0] if out else ""
        return first
    except Exception:
        return ""


def ensure_pdftotext_available() -> None:
    """pdftotext が見つからない場合に分かりやすいエラーで終了する。"""
    if not shutil.which("pdftotext"):
        sys.exit(
            "error: `pdftotext` not found on PATH. Install Poppler (e.g. `brew install poppler`)."
        )


def main(argv: list[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        return 1
    pdf_path = Path(args[0]).resolve()
    out_dir = Path(args[1]).resolve()
    version_override = args[2] if len(args) >= 3 else None
    source_url = args[3] if len(args) >= 4 else None
    out_dir.mkdir(parents=True, exist_ok=True)

    ensure_pdftotext_available()

    version = version_override or infer_version_from_path(pdf_path)
    print(f"[extract] pdf={pdf_path.name} version={version}", file=sys.stderr)

    manifest: dict[str, Any] = {
        "version": version,
        "sourcePdf": pdf_path.name,
        "sourceUrl": source_url,
        "sourceSha256": _sha256_of(pdf_path),
        "extractorVersion": EXTRACTOR_VERSION,
        "extractedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dependencies": {
            "pdfplumber": getattr(pdfplumber, "__version__", "unknown"),
            "pdftotext": _pdftotext_version(),
        },
        "masters": [],
    }
    sections_debug: list[dict[str, Any]] = []

    with pdfplumber.open(pdf_path) as pdf:
        sections = detect_sections(pdf)

        # 目次ページのセクション（テーブル非含有）を除外: start_pageの先頭行が
        # 該当マスター名と一致し、かつヘッダ行を持つかチェック
        valid_sections: list[MasterSection] = []
        for sec in sections:
            page = pdf.pages[sec.start_page - 1]
            tables = page.extract_tables()
            has_field_table = any(
                any(is_header_row(r) for r in t[:3]) for t in tables
            )
            if has_field_table:
                valid_sections.append(sec)
            else:
                print(
                    f"[skip] section '{sec.master_name}' at p.{sec.start_page} has no field table",
                    file=sys.stderr,
                )
        # valid_sectionsのend_pageを再計算
        for i, sec in enumerate(valid_sections):
            if i + 1 < len(valid_sections):
                sec.end_page = valid_sections[i + 1].start_page - 1
            else:
                last_page = _find_bessi_start(pdf, sec.start_page)
                sec.end_page = last_page - 1 if last_page else len(pdf.pages)

        for sec in valid_sections:
            print(
                f"[extract] {sec.master_id} p.{sec.start_page}-{sec.end_page} ({sec.master_name}"
                + (f" / {sec.sub_name}" if sec.sub_name else "")
                + ")",
                file=sys.stderr,
            )
            raw, normalized, source_counts = extract_master(pdf, sec, pdf_path)
            master_file = out_dir / f"{sec.master_id}.json"
            master_data: dict[str, Any] = {
                "masterId": sec.master_id,
                "masterName": sec.master_name,
                "subName": sec.sub_name,
                "version": version,
                "pages": {"start": sec.start_page, "end": sec.end_page},
                "fields": normalized,
            }
            master_file.write_text(
                json.dumps(master_data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest["masters"].append(
                {
                    "masterId": sec.master_id,
                    "masterName": sec.master_name,
                    "subName": sec.sub_name,
                    "pages": {"start": sec.start_page, "end": sec.end_page},
                    "file": master_file.name,
                    "fieldCount": len(normalized),
                }
            )
            sections_debug.append(
                {
                    "masterId": sec.master_id,
                    "masterName": sec.master_name,
                    "subName": sec.sub_name,
                    "pages": {"start": sec.start_page, "end": sec.end_page},
                    "rawRecordCount": len(raw),
                    "fieldCount": len(normalized),
                    "sourceCounts": source_counts,
                }
            )

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "sections.debug.json").write_text(
        json.dumps(sections_debug, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[done] wrote {len(manifest['masters'])} master files to {out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
