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


_SHISETSU_HEADER_RE = re.compile(r"^施設基準コード\s+施設基準")
_NAYOSE_BODY_HEAD_RE = re.compile(r"^名寄せ先\s+名寄せ元")
_NAYOSE_TITLE_RE = re.compile(r"名寄せコード一覧")


def classify_pages(pdf: pdfplumber.PDF) -> list[PageKind]:
    out: list[PageKind] = []
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        first = next((ln.strip() for ln in text.split("\n") if ln.strip()), "")
        kind = "other"
        if _SHISETSU_HEADER_RE.match(first):
            kind = "shisetsu"
        elif _NAYOSE_BODY_HEAD_RE.match(first) or _NAYOSE_TITLE_RE.search(text[:300]):
            kind = "nayose"
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
                data = _data_rows(table)
                cols = _detect_logical_columns(data, 2)
                if cols is None:
                    continue
                code_col, name_col = cols
                for row in data:
                    code_raw = row[code_col] if code_col < len(row) else None
                    name_raw = row[name_col] if name_col < len(row) else None
                    code = _normalize_code(code_raw)
                    name = _cell_text(name_raw)
                    if not code or not code.isdigit() or not name:
                        continue
                    if code in seen:
                        continue
                    seen.add(code)
                    codes.append({"code": code, "name": name})
    return codes, used_ranges


# ---------------------------------------------------------------------------
# 名寄せコード一覧
# ---------------------------------------------------------------------------


@dataclass
class NayoseCols:
    target_code: int
    target_name: int
    source_code: int
    source_name: int
    note: int | None  # 備考列がない（最終ページなど）場合は None


def _detect_nayose_columns(table: list[list[str | None]]) -> NayoseCols | None:
    """データ行から名寄せ表の論理5列を推定する。

    名寄せ表は ターゲット(コード,名称) + ソース(コード,名称) + 備考 の最大5列。
    最終ページ等で備考列が省略されているケースは4列に落とす。
    """
    # 名寄せ表であることをヘッダ行で確認（無関係な表を排除）
    header_row = next(
        (
            r
            for r in table
            if _is_header_row(r)
            and any(_flat(c) == "名寄せ先" for c in r)
            and any(_flat(c) == "名寄せ元" for c in r)
        ),
        None,
    )
    if header_row is None:
        return None
    target_pos = next(i for i, c in enumerate(header_row) if _flat(c) == "名寄せ先")
    source_pos = next(i for i, c in enumerate(header_row) if _flat(c) == "名寄せ元")
    note_pos = next(
        (i for i, c in enumerate(header_row) if _flat(c) == "備考"), -1
    )

    data = _data_rows(table)
    if not data:
        return None
    # 5列（備考あり）→ 4列（備考なし）の順で試す
    for n in (5, 4):
        candidates = _detect_logical_columns(data, n)
        if candidates is None:
            continue
        target_side = [c for c in candidates if c < source_pos]
        if note_pos > 0:
            source_side = [c for c in candidates if source_pos <= c < note_pos]
            note_side = [c for c in candidates if c >= note_pos]
        else:
            source_side = [c for c in candidates if c >= source_pos]
            note_side = []
        if len(target_side) == 2 and len(source_side) == 2 and (
            (n == 5 and len(note_side) == 1) or (n == 4 and not note_side)
        ):
            return NayoseCols(
                target_code=target_side[0],
                target_name=target_side[1],
                source_code=source_side[0],
                source_name=source_side[1],
                note=note_side[0] if note_side else None,
            )
    return None


def extract_nayose(
    pdf: pdfplumber.PDF, page_range: tuple[int, int]
) -> tuple[list[dict[str, Any]], list[dict[str, int]]]:
    """名寄せコード一覧を groups 形式で返す。"""
    start, end = page_range
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for page_no in range(start, end + 1):
        page = pdf.pages[page_no - 1]
        tables = page.extract_tables() or []
        for table in tables:
            if not table or len(table) < 2:
                continue
            cols = _detect_nayose_columns(table)
            if cols is None:
                continue
            for row in _data_rows(table):
                def _at(idx: int | None) -> str:
                    if idx is None or idx >= len(row):
                        return ""
                    return _cell_text(row[idx])

                t_code = _normalize_code(row[cols.target_code]) if cols.target_code < len(row) else ""
                t_name = _at(cols.target_name)
                s_code = _normalize_code(row[cols.source_code]) if cols.source_code < len(row) else ""
                s_name = _at(cols.source_name)
                note = _at(cols.note) if cols.note is not None else ""

                if t_code and t_code.isdigit():
                    current = {
                        "targetCode": t_code,
                        "targetName": t_name,
                        "sources": [],
                        "note": note,
                    }
                    groups.append(current)
                elif current is not None and note:
                    if not current["note"]:
                        current["note"] = note
                    elif note not in current["note"]:
                        current["note"] = (current["note"] + " " + note).strip()

                if s_code and current is not None:
                    current["sources"].append({"code": s_code, "name": s_name})

    return groups, [{"start": start, "end": end}]


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


def extract_all(pdf: pdfplumber.PDF) -> list[CodeTableArtifact]:
    """別紙PDFから抽出できる code tables を全て返す"""
    kinds = classify_pages(pdf)
    shisetsu_ranges = _group_consecutive(kinds, "shisetsu")
    nayose_ranges = _group_consecutive(kinds, "nayose")

    artifacts: list[CodeTableArtifact] = []

    if shisetsu_ranges:
        codes, used = extract_shisetsu_kijun(pdf, shisetsu_ranges)
        artifacts.append(
            CodeTableArtifact(
                code_table_id="shisetsu_kijun",
                code_table_name="施設基準コード一覧",
                kind="codeNamePairs",
                pages=used,
                payload={"codes": codes, "rowCount": len(codes)},
            )
        )

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
