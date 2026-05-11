"""別紙PDF（`master_2_YYYYMMDD.pdf`）からコード表を抽出する。

このPDFには、

  - 「施設基準コード一覧」 (p.31-70, p.126-127 等の連続表)
  - 「名寄せコード一覧」   (p.71-80 の名寄せ先→元グルーピング表)

の2つの構造化テーブルが含まれている。他の別紙ページは説明文・補助図解で
構造化対象外。

出力:
    <output_dir>/shisetsu_kijun.json   {codeTableId, kind=codeNamePairs, codes:[{code,name}]}
    <output_dir>/nayose.json           {codeTableId, kind=nayoseGroups, groups:[...]}

実装メモ:
    pdfplumber.extract_tables() はマージセルの内容を「マージ範囲内のどれかの列」
    に置く挙動がある。ページにより配置が変わる（例: 施設基準では p.31 のコード
    が col=0 だが p.126 では col=1）。
    そのため、ヘッダ位置を信用せず、データ行で実際に値が現れる列の出現頻度から
    論理カラム位置を推定する。
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pdfplumber

from . import manifest_io

EXTRACTOR_VERSION = "0.1.0"

# 全角数字→半角（コード正規化用）
_DIGIT_TRANS = str.maketrans("０１２３４５６７８９", "0123456789")
_WS_RE = re.compile(r"\s+")


def _flat(s: str | None) -> str:
    if s is None:
        return ""
    return _WS_RE.sub("", s)


def _cell_text(s: str | None) -> str:
    """セル内の空白（改行・スペース等）を全て削除する。

    PDF表組み由来の改行は単語の途中に挟まることが多く（例: 'リハビリテーショ\\nン料'）、
    日本語テキストでは空白を残すと逆に読みづらくなる。
    """
    if s is None:
        return ""
    return _WS_RE.sub("", s)


def _normalize_code(s: str | None) -> str:
    if s is None:
        return ""
    return unicodedata.normalize("NFKC", _flat(s)).translate(_DIGIT_TRANS)


# ---------------------------------------------------------------------------
# ページ分類
# ---------------------------------------------------------------------------


@dataclass
class PageKind:
    page_no: int  # 1-indexed
    kind: str  # 'shisetsu' | 'nayose' | 'other'


# 施設基準系のページ判定:
#  - 表ヘッダ '施設基準コード 施設基準' がページ先頭行に来る (本文中盤のページ)
#  - タイトル '施設基準コード一覧' / '施設基準コード表' がページ冒頭付近にある (見出しページ)
_SHISETSU_HEADER_RE = re.compile(r"^施設基準コード\s+施設基準")
_SHISETSU_TITLE_RE = re.compile(r"施設基準コード(?:一覧|表)")
_NAYOSE_BODY_HEAD_RE = re.compile(r"^名寄せ先\s+名寄せ元")
_NAYOSE_TITLE_RE = re.compile(r"名寄せコード一覧")
# 「別紙N－M」「別紙N」のラベル。N(M)は全角/半角どちらも可。
_APPENDIX_LABEL_RE = re.compile(r"別紙[\d０-９]+(?:[－-][\d０-９]+)?")


def _normalize_appendix_label(label: str) -> str:
    """別紙ラベル中の数字を半角化して比較を容易にする。"""
    return label.translate(_DIGIT_TRANS).replace("－", "-")


def _detect_appendix_label(text: str) -> str:
    """ページ冒頭付近にある『別紙Ｎ－Ｍ』ラベルを返す。なければ空文字。"""
    m = _APPENDIX_LABEL_RE.search(text[:300])
    return _normalize_appendix_label(m.group(0)) if m else ""


# 別紙ラベルごとの施設基準コード系列の codeTable id 振り分け。
#   別紙７－N (p.30-70): 医科・歯科系          → shisetsu_kijun
#   別紙９－N (p.116):   調剤系                → shisetsu_kijun_chouzai
#   別紙１０-N (p.125):  訪問看護療養費系      → shisetsu_kijun_houmon_kango
_SHISETSU_KIND_BY_APPENDIX_PREFIX: list[tuple[str, str]] = [
    ("別紙10", "shisetsu_houmon_kango"),
    ("別紙9", "shisetsu_chouzai"),
]


def _shisetsu_kind_for(appendix_label: str) -> str:
    for prefix, kind in _SHISETSU_KIND_BY_APPENDIX_PREFIX:
        if appendix_label.startswith(prefix):
            return kind
    return "shisetsu"


def classify_pages(pdf: pdfplumber.PDF) -> list[PageKind]:
    """各ページを kind に分類する。

    kind は以下のいずれか:
      - 'shisetsu'              : 別紙７－８ 医科・歯科系 施設基準コード一覧
      - 'shisetsu_chouzai'      : 別紙９－４ 調剤系 施設基準コード表
      - 'shisetsu_houmon_kango' : 別紙１０－５ 訪問看護療養費系 施設基準コード一覧
      - 'nayose'                : 別紙７－８ 名寄せコード一覧
      - 'other'                 : 構造化対象外

    ページ冒頭の『別紙Ｎ－Ｍ』ラベルで業種系列を切り替える。ラベルが無い続きページは
    直前ラベルを維持する。
    """
    out: list[PageKind] = []
    cur_label = ""
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        label = _detect_appendix_label(text)
        if label:
            cur_label = label
        first = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
        head = text[:400]
        if _NAYOSE_BODY_HEAD_RE.match(first) or _NAYOSE_TITLE_RE.search(head):
            kind = "nayose"
        elif _SHISETSU_HEADER_RE.match(first) or _SHISETSU_TITLE_RE.search(head):
            kind = _shisetsu_kind_for(cur_label)
        else:
            kind = "other"
        out.append(PageKind(page_no=i + 1, kind=kind))
    return out


def _group_consecutive(kinds: list[PageKind], target: str) -> list[tuple[int, int]]:
    """同じ kind が連続するページを (start, end) 区間に畳む"""
    ranges: list[tuple[int, int]] = []
    cur_start: int | None = None
    prev: int | None = None
    for pk in kinds:
        if pk.kind == target:
            if cur_start is None:
                cur_start = pk.page_no
            prev = pk.page_no
        else:
            if cur_start is not None and prev is not None:
                ranges.append((cur_start, prev))
                cur_start = None
                prev = None
    if cur_start is not None and prev is not None:
        ranges.append((cur_start, prev))
    return ranges


# ---------------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------------


_HEADER_TOKENS = {
    "施設基準コード",
    "施設基準",
    "施設基準名",
    "名寄せ先",
    "名寄せ元",
    "備考",
    "コード",
    "名称",
}


def _is_header_row(row: list[str | None]) -> bool:
    """ヘッダ専用キーワードだけで構成される行か（データ列推定から除外する）"""
    has_any = False
    for cell in row:
        s = _flat(cell)
        if not s:
            continue
        has_any = True
        if s not in _HEADER_TOKENS:
            return False
    return has_any


def _data_rows(table: list[list[str | None]]) -> list[list[str | None]]:
    return [r for r in table if not _is_header_row(r)]


def _detect_logical_columns(rows: list[list[str | None]], n: int) -> list[int] | None:
    """データ行で値が現れる列の出現回数を数え、上位 n 個を index 昇順で返す"""
    counts: dict[int, int] = {}
    for r in rows:
        for i, c in enumerate(r):
            if _flat(c):
                counts[i] = counts.get(i, 0) + 1
    if len(counts) < n:
        return None
    top = sorted(counts.keys(), key=lambda i: (-counts[i], i))[:n]
    return sorted(top)


# ---------------------------------------------------------------------------
# 施設基準コード一覧
# ---------------------------------------------------------------------------


def _row_to_code_name(row: list[str | None]) -> tuple[str, str] | None:
    """1 データ行から (code, name) を動的に抽出する。

    行ごとに列配置がずれるテーブル（例: p.125 で 902 と 923-926 で code/name が
    異なる列に入る）にも追随できるよう、固定列ではなく行内のセル並びから
    『最初に出現する数字セル = code, その右の最初の非空セル = name』として読む。
    """
    code = ""
    code_idx = -1
    for i, cell in enumerate(row):
        norm = _normalize_code(cell)
        if norm and norm.isdigit():
            code = norm
            code_idx = i
            break
    if code_idx < 0:
        return None
    name = ""
    for j in range(code_idx + 1, len(row)):
        text = _cell_text(row[j])
        if text:
            name = text
            break
    if not name:
        return None
    return code, name


def extract_shisetsu_kijun(
    pdf: pdfplumber.PDF, page_ranges: list[tuple[int, int]]
) -> tuple[list[dict[str, str]], list[dict[str, int]]]:
    """施設基準コード一覧の (code, name) ペアを連結して返す。

    複数ページにまたがり、ページごとに列配置が微妙にずれる可能性に対応する。
    """
    codes: list[dict[str, str]] = []
    seen: set[str] = set()
    used_ranges: list[dict[str, int]] = [{"start": s, "end": e} for s, e in page_ranges]
    for start, end in page_ranges:
        for page_no in range(start, end + 1):
            page = pdf.pages[page_no - 1]
            for table in page.extract_tables() or []:
                if not table:
                    continue
                for row in _data_rows(table):
                    pair = _row_to_code_name(row)
                    if pair is None:
                        continue
                    code, name = pair
                    if code in seen:
                        continue
                    seen.add(code)
                    codes.append({"code": code, "name": name})
    return codes, used_ranges


# ---------------------------------------------------------------------------
# 名寄せコード一覧
# ---------------------------------------------------------------------------


_TARGET_CODE_X_RANGE = (55.0, 105.0)  # 名寄せ先コード列のX範囲（経験則）
_TARGET_NAME_X_RANGE = (100.0, 205.0)  # 名寄せ先名称列
_SOURCE_CODE_X_RANGE = (205.0, 260.0)
_SOURCE_NAME_X_RANGE = (255.0, 420.0)
_NOTE_X_RANGE = (415.0, 600.0)
# 名寄せ先コード列を構成する縦罫線間の水平罫線。pdfplumber が rect 化したものから
# 列幅 ≈ 42 のものを target_code セル境界として利用する。
_TARGET_CELL_LINE_X0_RANGE = (55.0, 60.0)
_TARGET_CELL_LINE_X1_RANGE = (95.0, 100.0)
# 名寄せ元コード列の水平罫線。x0≈206, x1≈258 で各 source 行の境界を表す。
_SOURCE_CELL_LINE_X0_RANGE = (205.0, 210.0)
_SOURCE_CELL_LINE_X1_RANGE = (256.0, 260.0)


def _word_in_range(word: dict, x_range: tuple[float, float]) -> bool:
    return x_range[0] <= word["x0"] <= x_range[1]


def _word_in_y_range(word: dict, y_top: float, y_bot: float) -> bool:
    return y_top <= word["top"] <= y_bot


def _is_target_code_token(text: str) -> bool:
    """target_code は 3-4桁の数字。実例は 849 と 8001〜8030 周辺。"""
    return text.isdigit() and 3 <= len(text) <= 4


def _is_source_code_token(text: str) -> bool:
    return text.isdigit() and 1 <= len(text) <= 5


def _collect_text_in_box(
    words: list[dict], x_range: tuple[float, float], y_top: float, y_bot: float
) -> str:
    """与えられた x/y 範囲に含まれる words のテキストを連結（改行・空白除去）。"""
    parts = [
        w["text"]
        for w in sorted(words, key=lambda w: (w["top"], w["x0"]))
        if _word_in_range(w, x_range) and _word_in_y_range(w, y_top, y_bot)
    ]
    return _flat("".join(parts))


@dataclass
class _NayoseRegion:
    """1つの名寄せグループの Y 領域とメタ情報"""

    y_top: float
    y_bot: float
    code: str
    target_name: str
    note: str
    sources: list[dict[str, str]]


def _find_body_start_y(words: list[dict]) -> float:
    """ヘッダ行 ('コード'/'名称') の最下端 Y を返す。

    ページ冒頭（y < 130）かつ target_code/source_code 列の x 位置に絞り、
    本文中に出現する '名寄せコード）' のような単語に引きずられないようにする。
    """
    y = 0.0
    for w in words:
        if w["top"] >= 130:
            continue
        if _flat(w["text"]) not in ("名称", "コード"):
            continue
        # 'コード' ヘッダは x0 ≈ 62 (target側) / 211 (source側) に出現する
        if not (55 <= w["x0"] <= 80 or 130 <= w["x0"] <= 170 or 205 <= w["x0"] <= 230 or 340 <= w["x0"] <= 380):
            continue
        y = max(y, w["bottom"])
    return y + 2.0 if y > 0 else 60.0


def _target_cell_boundaries(page: pdfplumber.page.Page, body_start_y: float) -> list[float]:
    """名寄せ先コード列の水平罫線 Y 座標を昇順で返す。

    これらの罫線は target_code 列のセル境界そのもの。隣接する2本の Y で挟まれる
    範囲がひとつの merged cell（= ひとつの名寄せグループ）になる。
    pdfplumber.find_tables() は merged cell を取りこぼすことがあるため、
    rect 由来の生罫線から直接境界を組み立てる。
    """
    ys: set[float] = set()
    for r in page.rects:
        if r["height"] >= 2:
            continue
        if not (_TARGET_CELL_LINE_X0_RANGE[0] <= r["x0"] <= _TARGET_CELL_LINE_X0_RANGE[1]):
            continue
        if not (_TARGET_CELL_LINE_X1_RANGE[0] <= r["x1"] <= _TARGET_CELL_LINE_X1_RANGE[1]):
            continue
        ys.add(round(r["top"], 1))
    # 本文開始 Y 以降の罫線だけ採用（ヘッダ罫線は除外）。
    return sorted(y for y in ys if y >= body_start_y - 1.0)


def _collect_regions_from_boundaries(
    page: pdfplumber.page.Page,
    boundaries: list[float],
    words: list[dict],
    page_height: float,
) -> list[_NayoseRegion]:
    """target_code 列の水平罫線で区切られた各 Y 帯を region 化する。

    各帯の中で target_code word を探す。存在しない帯（= 前ページからの継続）は
    region 化しない。
    """
    if not boundaries:
        return []
    segments: list[tuple[float, float]] = []
    for i, y in enumerate(boundaries):
        y_top = y
        y_bot = boundaries[i + 1] if i + 1 < len(boundaries) else page_height
        if y_bot - y_top < 5.0:
            continue  # ヘッダ等の薄い帯は無視
        segments.append((y_top, y_bot))

    regions: list[_NayoseRegion] = []
    for y_top, y_bot in segments:
        target_words = [
            w
            for w in words
            if _word_in_range(w, _TARGET_CODE_X_RANGE)
            and _is_target_code_token(w["text"])
            and y_top <= w["top"] < y_bot
        ]
        if not target_words:
            continue  # 継続グループ（target_code なし）
        target_words.sort(key=lambda w: w["top"])
        code = _normalize_code(target_words[0]["text"])
        target_name = _collect_text_in_box(words, _TARGET_NAME_X_RANGE, y_top, y_bot)
        note = _collect_text_in_box(words, _NOTE_X_RANGE, y_top, y_bot)
        regions.append(
            _NayoseRegion(
                y_top=y_top,
                y_bot=y_bot,
                code=code,
                target_name=target_name,
                note=note,
                sources=[],
            )
        )
    return regions


def _source_row_boundaries(page: pdfplumber.page.Page, body_start_y: float) -> list[float]:
    """名寄せ元コード列の水平罫線 Y を昇順で返す。各 source 行を一意に区切る。"""
    ys: set[float] = set()
    for r in page.rects:
        if r["height"] >= 2:
            continue
        if not (_SOURCE_CELL_LINE_X0_RANGE[0] <= r["x0"] <= _SOURCE_CELL_LINE_X0_RANGE[1]):
            continue
        if not (_SOURCE_CELL_LINE_X1_RANGE[0] <= r["x1"] <= _SOURCE_CELL_LINE_X1_RANGE[1]):
            continue
        ys.add(round(r["top"], 1))
    return sorted(y for y in ys if y >= body_start_y - 1.0)


def _attach_sources_to_regions(
    regions: list[_NayoseRegion],
    page: pdfplumber.page.Page,
    words: list[dict],
    body_start_y: float,
    previous_group: dict[str, Any] | None,
) -> None:
    """source 列の水平罫線で1行ずつ区切り、含む region (または継続グループ) に紐付ける。"""
    src_boundaries = _source_row_boundaries(page, body_start_y)
    for i in range(len(src_boundaries) - 1):
        y_top = src_boundaries[i]
        y_bot = src_boundaries[i + 1]
        if y_bot - y_top < 5.0 or y_bot - y_top > 40.0:
            continue  # ヘッダ罫線間 / 想定外の高さは無視
        src_code = _collect_text_in_box(words, _SOURCE_CODE_X_RANGE, y_top, y_bot)
        if not src_code or not _is_source_code_token(src_code):
            continue
        src_name = _collect_text_in_box(words, _SOURCE_NAME_X_RANGE, y_top, y_bot)
        entry = {"code": _normalize_code(src_code), "name": src_name}
        # 含まれる region を探す（Y中心で判定）
        y_mid = (y_top + y_bot) / 2
        target_region = next(
            (r for r in regions if r.y_top <= y_mid < r.y_bot), None
        )
        if target_region is not None:
            target_region.sources.append(entry)
        elif previous_group is not None:
            previous_group["sources"].append(entry)


def extract_nayose(
    pdf: pdfplumber.PDF, page_range: tuple[int, int]
) -> tuple[list[dict[str, Any]], list[dict[str, int]]]:
    """名寄せコード一覧を groups 形式で返す。

    pdfplumber.extract_tables() は target_code 列の merged cell を取りこぼすことが
    多い（例: 8014 / 8015 は cell として認識されない）。

    そこで、page.rects から target_code 列の水平罫線 Y 座標を集め、
    それらで区切られた Y 帯 = 名寄せ先グループ として処理する:

    1. 罫線 Y で Y 帯を切り出す
    2. 各 Y 帯に target_code word があればその帯を 1 グループとする
    3. target_code word が無い Y 帯は前ページからの継続テキスト (target_name / 備考)
       とソース行とみなし、直前グループに合流させる
    4. source 行は find_tables() の sub-row (height ≤ 35) から x 位置で抽出
    """
    start, end = page_range
    all_groups: list[dict[str, Any]] = []

    for page_no in range(start, end + 1):
        page = pdf.pages[page_no - 1]
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        body_start_y = _find_body_start_y(words)

        boundaries = _target_cell_boundaries(page, body_start_y)
        if not boundaries:
            continue
        regions = _collect_regions_from_boundaries(page, boundaries, words, page.height)

        previous_group = all_groups[-1] if all_groups else None

        # 前ページから続く先頭の継続帯（target_code word のない先頭セグメント）から
        # target_name / note を直前グループに合流する
        if previous_group is not None and boundaries:
            first_region_y_top = regions[0].y_top if regions else page.height
            if first_region_y_top > boundaries[0] + 1.0:
                cont_top = boundaries[0]
                cont_bot = first_region_y_top
                extra_name = _collect_text_in_box(
                    words, _TARGET_NAME_X_RANGE, cont_top, cont_bot
                )
                extra_note = _collect_text_in_box(
                    words, _NOTE_X_RANGE, cont_top, cont_bot
                )
                if extra_name and extra_name not in previous_group["targetName"]:
                    previous_group["targetName"] = (
                        previous_group["targetName"] + extra_name
                    )
                if extra_note and extra_note not in previous_group["note"]:
                    previous_group["note"] = previous_group["note"] + extra_note

        _attach_sources_to_regions(regions, page, words, body_start_y, previous_group)

        for r in regions:
            all_groups.append(
                {
                    "targetCode": r.code,
                    "targetName": r.target_name,
                    "sources": r.sources,
                    "note": r.note,
                }
            )

    return all_groups, [{"start": start, "end": end}]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class CodeTableArtifact:
    code_table_id: str
    code_table_name: str
    kind: str  # 'codeNamePairs' | 'nayoseGroups'
    pages: list[dict[str, int]]
    payload: dict[str, Any]  # JSONに保存する本体


_SHISETSU_TABLE_SPECS: list[tuple[str, str, str]] = [
    # (kind, code_table_id, code_table_name)
    ("shisetsu", "shisetsu_kijun", "施設基準コード一覧（医科・歯科）"),
    ("shisetsu_chouzai", "shisetsu_kijun_chouzai", "施設基準コード表（調剤）"),
    (
        "shisetsu_houmon_kango",
        "shisetsu_kijun_houmon_kango",
        "施設基準コード一覧（訪問看護療養費）",
    ),
]


def extract_all(pdf: pdfplumber.PDF) -> list[CodeTableArtifact]:
    """別紙PDFから抽出できる code tables を全て返す"""
    kinds = classify_pages(pdf)
    artifacts: list[CodeTableArtifact] = []

    for kind, table_id, table_name in _SHISETSU_TABLE_SPECS:
        ranges = _group_consecutive(kinds, kind)
        if not ranges:
            continue
        codes, used = extract_shisetsu_kijun(pdf, ranges)
        artifacts.append(
            CodeTableArtifact(
                code_table_id=table_id,
                code_table_name=table_name,
                kind="codeNamePairs",
                pages=used,
                payload={"codes": codes, "rowCount": len(codes)},
            )
        )

    nayose_ranges = _group_consecutive(kinds, "nayose")
    if nayose_ranges:
        all_groups: list[dict[str, Any]] = []
        used_ranges: list[dict[str, int]] = []
        for r in nayose_ranges:
            groups, used = extract_nayose(pdf, r)
            all_groups.extend(groups)
            used_ranges.extend(used)
        artifacts.append(
            CodeTableArtifact(
                code_table_id="nayose",
                code_table_name="名寄せコード一覧",
                kind="nayoseGroups",
                pages=used_ranges,
                payload={"groups": all_groups, "groupCount": len(all_groups)},
            )
        )

    return artifacts


def _infer_version_from_path(pdf_path: Path) -> str:
    m = re.search(r"(\d{8})", pdf_path.stem)
    return m.group(1) if m else "unknown"


def main(argv: list[str] | None = None) -> int:
    """別紙PDFを抽出して `out_dir/<codetable>.json` と manifest を更新する。

    使い方:
        python -m reseden_master_spec.extract_appendix <pdf> <out_dir> [version] [source_url]
    """
    args = list(argv) if argv is not None else sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        return 1
    pdf_path = Path(args[0]).resolve()
    out_dir = Path(args[1]).resolve()
    version_override = args[2] if len(args) >= 3 else None
    source_url = args[3] if len(args) >= 4 else None
    out_dir.mkdir(parents=True, exist_ok=True)

    version = version_override or _infer_version_from_path(pdf_path)
    print(f"[extract_appendix] pdf={pdf_path.name} version={version}", file=sys.stderr)

    manifest = manifest_io.read_manifest(out_dir)
    if manifest is None:
        manifest = manifest_io.init_manifest(
            version=version,
            extractor_version=EXTRACTOR_VERSION,
            dependencies={
                "pdfplumber": getattr(pdfplumber, "__version__", "unknown"),
            },
        )
    else:
        manifest_io.ensure_shape(manifest)
        manifest["version"] = version
        # extractorVersion はこの実行で書いた値を優先
        manifest["extractorVersion"] = EXTRACTOR_VERSION
    # 既存の codeTables 由来ファイルを掃除する用に保存
    prev_files = {ct.get("file") for ct in manifest.get("codeTables", []) if ct.get("file")}
    manifest["codeTables"] = []

    with pdfplumber.open(pdf_path) as pdf:
        artifacts = extract_all(pdf)

    new_files: set[str] = set()
    for art in artifacts:
        out_file = out_dir / f"{art.code_table_id}.json"
        body: dict[str, Any] = {
            "codeTableId": art.code_table_id,
            "codeTableName": art.code_table_name,
            "kind": art.kind,
            "version": version,
            "sourcePdf": pdf_path.name,
            "pages": art.pages,
            **art.payload,
        }
        out_file.write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        new_files.add(out_file.name)
        # manifest entry
        if art.kind == "codeNamePairs":
            row_count = art.payload.get("rowCount", len(art.payload.get("codes", [])))
        else:
            row_count = art.payload.get("groupCount", len(art.payload.get("groups", [])))
        manifest["codeTables"].append(
            {
                "codeTableId": art.code_table_id,
                "codeTableName": art.code_table_name,
                "kind": art.kind,
                "pages": art.pages,
                "file": out_file.name,
                "rowCount": row_count,
                "sourcePdf": pdf_path.name,
            }
        )
        print(
            f"[extract_appendix] {art.code_table_id} kind={art.kind} rows={row_count}",
            file=sys.stderr,
        )

    # 不要になった旧 codeTable ファイルを削除
    for stale in prev_files - new_files:
        stale_path = out_dir / stale
        if stale_path.exists():
            stale_path.unlink()
            print(f"[extract_appendix] removed stale {stale}", file=sys.stderr)

    manifest_io.upsert_source(
        manifest,
        kind="appendix",
        source_pdf=pdf_path.name,
        source_url=source_url,
        source_sha256=manifest_io.sha256_of(pdf_path),
        source_version=_infer_version_from_path(pdf_path),
    )
    manifest_io.write_manifest(out_dir, manifest)
    print(
        f"[done] wrote {len(manifest['codeTables'])} code tables to {out_dir}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
