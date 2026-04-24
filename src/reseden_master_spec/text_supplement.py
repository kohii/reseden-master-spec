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
_CIRCLED_DIGITS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
# 項目名がセル内で折返されて途中切れしている可能性がある末尾
_TRUNCATED_NAME_SUFFIXES = ("グルー", "コ", "ロ", "プ", "判", "者", "ー", "グ")
# 継続行の先頭に現れた「項目名の続き断片 + 2スペース以上 + description 本体」パターン
_CONTINUATION_WITH_FRAG_RE = re.compile(r"^(?P<frag>\S{1,8})\s{2,}(?P<rest>\S.*)$")
# 断片として許容する文字（漢字/ひらがな/カタカナ + 記号少々）
_NAME_FRAG_CHARSET_RE = re.compile(r"^[぀-ヿ一-鿿・／／]+$")


def _zh(s: str) -> str:
    return s.translate(_ZEN2HAN)


def _count_labels(name: str) -> int:
    """"施設基準①～⑩" や "背反１～１０" 等から繰返し回数 (10) を返す。無ければ 0。"""
    m = re.search(r"([①-⑳])\s*[～〜~\-]\s*([①-⑳])", name)
    if m:
        return _CIRCLED_DIGITS.index(m.group(2)) - _CIRCLED_DIGITS.index(m.group(1)) + 1
    # 数字ラベル (1〜10 / １～１０)
    m = re.search(r"([0-9０-９]+)\s*[～〜~\-]\s*([0-9０-９]+)", name)
    if m:
        a, b = int(_zh(m.group(1))), int(_zh(m.group(2)))
        if a < b:
            return b - a + 1
    return 0


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
            # 項目名列の残余（例: "プ区分         行為の..."）を検出し、
            # 先頭断片を name へ戻す。current.name が途中切れの suffix で終わる場合に限る。
            if current.get("name", "").endswith(_TRUNCATED_NAME_SUFFIXES):
                frag_m = _CONTINUATION_WITH_FRAG_RE.match(stripped)
                if frag_m and _NAME_FRAG_CHARSET_RE.match(frag_m.group("frag")):
                    current["name"] = current["name"] + frag_m.group("frag")
                    rest = frag_m.group("rest").strip()
                    if rest:
                        current["content"] = (
                            current["content"] + "\n" + rest
                        ).strip()
                    continue
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


# サブ項目行（seq無し） - "  項目名 モード バイト 形式 内容"
_SUB_ROW_RE = re.compile(
    r"^\s+(?P<name>\S.*?)\s+(?P<mode>" + "|".join(_MODE_TOKENS) + r")\s+"
    r"(?P<bytes>\([0-9０-９]+\)|[0-9０-９]+)\s+"
    r"(?P<fmt>" + "|".join(_FORMAT_TOKENS) + r")"
    r"(?:\s+(?P<content>.*))?$"
)


_PAGE_FOOTER_RE = re.compile(r"^\s*-\s*[0-9０-９]+\s*-\s*$")
_ANY_MODE_TOKEN_RE = re.compile("|".join(_MODE_TOKENS))


def _is_page_boilerplate(stripped: str) -> bool:
    """pdftotext 出力に混入するページフッタ・テーブルヘッダ行かを判定する。

    サブ項目行（例: "項番 数字 3 固定 項番を設定する。"）は除外しない。
    モードトークン（数字/英数/漢字/カナ）を含まず、かつ
    「項番 … 項 目 名」や「ﾓｰﾄﾞ ﾊﾞｲﾄ 形式」のようにヘッダ用語が揃っている行だけを
    ボイラープレートとみなす。
    """
    if _PAGE_FOOTER_RE.match(stripped):
        return True
    has_mode_token = bool(_ANY_MODE_TOKEN_RE.search(stripped))
    if has_mode_token:
        return False
    # モードトークンを含まない行のうち、以下のヘッダ断片はスキップ
    if "項番" in stripped and "項 目 名" in stripped:
        return True
    if stripped.strip() in ("形 式", "形    式"):
        return True
    if "ﾓｰﾄﾞ" in stripped and ("ﾊﾞｲﾄ" in stripped or "形式" in stripped):
        return True
    if stripped.strip() in ("ﾓｰﾄﾞ", "ﾊﾞｲﾄ", "ﾊﾞｲﾄ 形式"):
        return True
    return False


def find_range_subdefinitions(
    pdf_path: Path, start: int, end: int, rng: dict[str, Any]
) -> list[dict[str, Any]]:
    """range header 直後から、1 repetition 分のサブ項目定義を抽出する。

    rng は find_seq_ranges が返す 1エントリ。同じサブ名が再登場した時点で
    2 周目とみなして打ち切る。range 範囲外の seq 行（例: "100 変更年月日"）
    に到達しても打ち切る。
    """
    lines = pdftotext_pages(pdf_path, start, end)
    start_seq = rng["start"]
    end_seq = rng["end"]
    header_seen = False
    subs: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    pending_description_lines: list[str] = []

    def attach_description():
        if subs and pending_description_lines:
            joined = "\n".join(pending_description_lines).strip()
            if joined:
                prev = subs[-1].get("content", "")
                subs[-1]["content"] = (prev + "\n" + joined).strip() if prev else joined
        pending_description_lines.clear()

    for line in lines:
        if not header_seen:
            m = _RANGE_HEADER_RE.match(line)
            if m and int(_zh(m.group("seq_start"))) == start_seq:
                header_seen = True
                # range header 行の末尾（description）に乗った 1個目のサブを拾う
                tail = line[m.end():].strip()
                mm = _MODE_AT_RE.match(tail)
                if mm:
                    name = mm.group("name").strip()
                    subs.append(
                        {
                            "name": name,
                            "mode": mm.group("mode"),
                            "bytes": _zh(mm.group("bytes")),
                            "fmt": mm.group("fmt"),
                            "content": (mm.group("content") or "").strip(),
                        }
                    )
                    seen_names.add(name)
            continue

        stripped = line.strip()
        if not stripped:
            # 空行: 現在の sub の description 区切り
            attach_description()
            continue
        # PDF ページフッタ・ヘッダは説明文に混入させない
        if _is_page_boilerplate(stripped):
            continue
        # range 範囲外の独立 seq 行（例: "100 変更年月日"）で打ち切り
        inner_seq_m = re.match(r"^\s*([0-9０-９]+)\s+\S", line)
        if inner_seq_m:
            candidate = int(_zh(inner_seq_m.group(1)))
            if candidate > end_seq:
                attach_description()
                break
            # 範囲内 seq の行は想定しない（range 本体は seq を持たないサブだけ）
            # だが誤拾いを避けるため attach して進める
            attach_description()
            continue
        # 範囲終端マーカー行 ("９９" 単独) はスキップ
        if _RANGE_END_RE.match(line):
            continue
        # "～" で始まる行: 後ろに実体があればサブ定義として拾う
        if _RANGE_TILDE_RE.match(line):
            rest = re.sub(r"^\s*[～〜~]\s*", "", line.rstrip())
            if rest.strip():
                m_tilde = _MODE_AT_RE.match(rest)
                if m_tilde:
                    name = m_tilde.group("name").strip()
                    if name not in seen_names:
                        attach_description()
                        subs.append(
                            {
                                "name": name,
                                "mode": m_tilde.group("mode"),
                                "bytes": _zh(m_tilde.group("bytes")),
                                "fmt": m_tilde.group("fmt"),
                                "content": (m_tilde.group("content") or "").strip(),
                            }
                        )
                        seen_names.add(name)
                    continue
            continue

        m_sub = _SUB_ROW_RE.match(line)
        if m_sub:
            attach_description()
            name = m_sub.group("name").strip()
            if name in seen_names:
                # 2 周目に入ったので打ち切り
                break
            subs.append(
                {
                    "name": name,
                    "mode": m_sub.group("mode"),
                    "bytes": _zh(m_sub.group("bytes")),
                    "fmt": m_sub.group("fmt"),
                    "content": (m_sub.group("content") or "").strip(),
                }
            )
            seen_names.add(name)
            continue
        # mode/bytes/fmt のない行は親ラベル行 or 説明文。
        # 短く 15 文字以下なら親ラベルとみなしスキップ、それ以外は直前 sub の description に連結。
        if len(stripped) <= 15 and not re.search(r"\d", stripped):
            # 親ラベル行 (告示番号 / 診療行為名称 等): sub として登録しない
            continue
        pending_description_lines.append(stripped)

    attach_description()
    return subs


# 範囲ヘッダ行: "100 年齢加算①～④  ５２  当該..." のようなパターン
# サブ項目名＋モード＋バイト等が欠けていることが多いので、
# 「項番 + 項目名 + (maxBytes) + (内容先頭)」のゆるいマッチに留める。
# ラベル表現: "①～⑩" / "１～１０" / "1〜10" などを許容。
_LABEL_NUM = r"(?:[①-⑳]|[0-9０-９]{1,2})"
_RANGE_HEADER_RE = re.compile(
    r"^\s*(?P<seq_start>[0-9０-９]+)\s+(?P<name>.+?"
    + _LABEL_NUM
    + r"\s*[～〜~\-]\s*"
    + _LABEL_NUM
    + r")"
    r"(?:\s+(?P<max_bytes>[0-9０-９]+))?"
)
# 行頭が "～" の行 (単独、または "～ <説明文>" の形)
_RANGE_TILDE_RE = re.compile(r"^\s*[～〜~](\s|$)")
_RANGE_END_RE = re.compile(r"^\s*([0-9０-９]+)\s*$")


def find_seq_ranges(pdf_path: Path, start: int, end: int) -> list[dict[str, Any]]:
    """pdftotextから範囲表記ヘッダを検出し、(start_seq, end_seq, name, max_bytes) を返す。

    例:
        100 年齢加算①～④       ５２        当該診療行為...
         ～
        111
    → {"start": 100, "end": 111, "name": "年齢加算①～④", "max_bytes": "52", "description": "..."}
    """
    lines = pdftotext_pages(pdf_path, start, end)
    results: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        m = _RANGE_HEADER_RE.match(line)
        if m:
            # 続く行で 〜 と 終端項番 を探す
            end_seq = None
            description_lines: list[str] = []
            # ヘッダ行の内容先頭部分（maxBytesの後）を description に取り込む
            tail = line[m.end() :].strip()
            if tail:
                description_lines.append(tail)
            j = i + 1
            saw_tilde = False
            while j < len(lines) and j < i + 60:
                nxt = lines[j].rstrip()
                if _RANGE_TILDE_RE.match(nxt):
                    saw_tilde = True
                    # "～" 以降の説明文を description に取り込む
                    rest = re.sub(r"^\s*[～〜~]\s*", "", nxt)
                    if rest.strip():
                        description_lines.append(rest.strip())
                    j += 1
                    continue
                m2 = _RANGE_END_RE.match(nxt)
                if m2:
                    candidate = int(_zh(m2.group(1)))
                    # "〜" を見たあとなら終端、さもなくば直近 (i+5以内) に限定
                    if saw_tilde:
                        end_seq = candidate
                        j += 1
                        break
                    if j <= i + 5 and candidate > int(_zh(m.group("seq_start"))):
                        end_seq = candidate
                        j += 1
                        break
                # 他の項番行 (例: "45 施設基準コード") に当たったら範囲推定。
                # ヘッダの ①〜⑩ ラベル数から期待される end_seq と比較し、
                # その項番自体 or 直前を終端とする判定を行う。
                inner_seq_m = re.match(r"^\s*([0-9０-９]+)\s+\S", nxt)
                if inner_seq_m and saw_tilde:
                    candidate = int(_zh(inner_seq_m.group(1)))
                    start_seq_val = int(_zh(m.group("seq_start")))
                    label_count = _count_labels(m.group("name"))
                    if label_count > 0:
                        expected_end = start_seq_val + label_count - 1
                        # 1. その項番がラベル終端と一致 → 終端自体が範囲末尾
                        if candidate == expected_end:
                            end_seq = candidate
                            break
                        # 2. その項番が期待末尾を超える → ヘッダに続く項目は独立
                        #    範囲終端 = candidate - 1
                        if candidate > expected_end:
                            end_seq = candidate - 1
                            break
                        # 3. それ以外: ヘッダ末尾推定値を採用
                        end_seq = expected_end
                        break
                    else:
                        end_seq = candidate - 1
                    break
                if nxt.strip():
                    description_lines.append(nxt.strip())
                j += 1

            if end_seq is not None:
                start_seq = int(_zh(m.group("seq_start")))
                mb = m.group("max_bytes")
                results.append(
                    {
                        "start": start_seq,
                        "end": end_seq,
                        "name": m.group("name").strip(),
                        "max_bytes": _zh(mb) if mb else "",
                        "description": "\n".join(description_lines).strip(),
                    }
                )
            i = j
            continue
        i += 1
    return results
