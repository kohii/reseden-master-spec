"""pdfplumberで取りこぼした項目を pdftotext -layout ベースで補完する。

pdfplumberは表の罫線やセル構造を解釈するため、親項目がグループセル化されて
いるケース（例: コメントマスター項番6,7の「コメント文」→「漢字有効桁数/漢字名称」）
で行が丸ごと欠落することがある。

このモジュールは補助的に pdftotext -layout の出力を行単位で解析し、
「項番 + 項目名 + モード + バイト + 項目形式 + 内容」を拾って records を返す。
主抽出 (pdfplumber) の結果で欠けた seq のみを埋めるように呼び出し側で統合する。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

_MODE_TOKENS = ("数字", "英数カナ", "英数", "漢字", "カナ")
_FORMAT_TOKENS = ("固定", "可変")

# 項番 (全角/半角数字) ＋ インデント ＋ 項目名 ＋ モード ＋ バイト ＋ 項目形式 ＋ 内容
# モード・バイトが出てこないサブ項目行などは、最初の wave でマッチできないので別扱い。
_ROW_RE = re.compile(
    r"^\s*(?P<seq>[0-9０-９]{1,3})\s+"
    r"(?P<rest>.+?)$"
)

# 項目名とその後のトークンを分解
_MODE_AT_RE = re.compile(
    r"(?P<name>.+?)\s+(?P<mode>" + "|".join(_MODE_TOKENS) + r")\s+"
    r"(?P<bytes>\([0-9０-９]+\)|[0-9０-９]+)\s+"
    r"(?P<fmt>" + "|".join(_FORMAT_TOKENS) + r")"
    r"(?:\s+(?P<content>.*))?$"
)

_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")


def _zh(s: str) -> str:
    return s.translate(_ZEN2HAN)


def pdftotext_pages(pdf_path: Path, start: int, end: int) -> list[str]:
    """指定ページ範囲（1-indexed, inclusive）のテキスト layout を行単位で返す。"""
    result = subprocess.run(
        [
            "pdftotext",
            "-layout",
            "-f",
            str(start),
            "-l",
            str(end),
            str(pdf_path),
            "-",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.splitlines()


def parse_text_rows(lines: list[str]) -> list[dict[str, Any]]:
    """pdftotext layout の行群から、項目行を抽出する。"""
    records: list[dict[str, Any]] = []
    current_group: str = ""  # "コメント文" のような親グループ名（最後に見た非項番行の項目名候補）
    current: dict[str, Any] | None = None  # 連続する description 行を連結する対象

    for raw in lines:
        # ヘッダ・ページ番号等は行頭パターンで除外しないが、ヒューリスティックに抜く
        line = raw.rstrip()
        if not line:
            # 空行: 追跡中レコードの切れ目と見なす
            current = None
            continue
        if "形    式" in line or "項 目 名" in line or "項番" in line and "項 目 名" in line:
            current = None
            continue
        if line.strip().startswith("-") and line.strip().endswith("-"):
            # フッタ "- 24 -"
            current = None
            continue

        m = _ROW_RE.match(line)
        if m:
            seq = int(_zh(m.group("seq")))
            rest = m.group("rest").strip()
            inner = _MODE_AT_RE.match(rest)
            if inner:
                name = inner.group("name").strip()
                mode = inner.group("mode")
                bytes_raw = _zh(inner.group("bytes"))
                fmt = inner.group("fmt")
                content = (inner.group("content") or "").strip()
                rec = {
                    "seq": seq,
                    "name": name,
                    "group": current_group,
                    "mode_raw": mode,
                    "max_bytes_raw": bytes_raw,
                    "item_format_raw": fmt,
                    "content": content,
                }
                records.append(rec)
                current = rec
                continue
            # モード未検出 = 親項目の行（「コメント文」みたいな単独ヘッダではなく、
            # 項番がある親行。サブ行でモードが来る可能性）
            # 記録は一旦せずに次行に委ねる
            current = None
            continue

        # 項番なし行
        stripped = line.strip()
        # サブグループ親の候補: 短く、モード・バイト・数字で構成されていない
        if (
            not any(tok in stripped for tok in _MODE_TOKENS + _FORMAT_TOKENS)
            and not re.search(r"\d", stripped)
            and len(stripped) <= 20
            and current is None  # 前にレコード追記中ではない
        ):
            current_group = stripped
            continue
        # description 折返し: current に連結
        if current is not None:
            current["content"] = (current["content"] + "\n" + stripped).strip()

    return records


def supplement_from_text(
    pdf_path: Path, start: int, end: int, missing_seqs: set[int]
) -> list[dict[str, Any]]:
    """指定ページ範囲で missing_seqs に該当する項番のテキスト抽出結果を返す。"""
    if not missing_seqs:
        return []
    lines = pdftotext_pages(pdf_path, start, end)
    records = parse_text_rows(lines)
    return [r for r in records if r["seq"] in missing_seqs]
