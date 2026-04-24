"""reseden-master-spec CLI

診療報酬制度「基本マスターファイル仕様書」PDFを構造化JSONで扱うCLI。
AIエージェント/人間のいずれも、この help の指示だけで必要な情報に辿り着けるよう設計している。

== はじめの一歩 ==

  reseden info                    -- 同梱データの概要と主要サブコマンドの紹介
  reseden masters                 -- 全マスター一覧（masterId を確認）
  reseden fields <masterId>       -- そのマスターの全フィールド

== 引く ==

  reseden field <masterId> <seq>           -- 1フィールドの詳細（codes 含む）
  reseden code  <masterId> <seq> <code>    -- コード値名称の逆引き
  reseden search <keyword> [--master-id M] [--limit N]
                                           -- 全マスター横断のキーワード検索

== 補助 ==

  reseden versions                -- 抽出済みの版 (YYYYMMDD) 一覧
  reseden verify                  -- 抽出結果の健全性チェック（終了コード 0/1）
  reseden schema                  -- フィールド/コード の JSON スキーマ概要
  reseden skill                   -- エージェント用 SKILL.md を stdout に出力
  reseden --version               -- CLI ツール自体のバージョン

== 出力と慣習 ==

- すべてのサブコマンドは UTF-8 の JSON を stdout に返す（`jq` で加工可能）。
- 補助メッセージ・ヒント・警告は stderr に書く（パイプしても混ざらない）。
- 失敗時は非0終了コード + stderr に `error: ...` を 1 行、可能なら次に打つべき
  コマンド候補を併記する。

== データ解決 ==

- --out-dir 省略時: (1) CWD/data に版があればそれ、(2) パッケージ同梱 data、
  (3) なければ CWD/data（fetch 用の書き込み先デフォルト）
- --version 省略時: 最新版 (YYYYMMDD 降順の先頭)

== 配布版と開発版 ==

  fetch は Poppler (pdftotext) 必須で開発者向け。
  配布版（uv tool install）では fetch 以外のすべてが使える。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib.resources import files as _pkg_files
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import extract as extractor


# ---------------------------------------------------------------------------
# データディレクトリ解決
# ---------------------------------------------------------------------------


def _packaged_data_dir() -> Path | None:
    try:
        resource = _pkg_files("reseden_master_spec") / "data"
        path = Path(str(resource))
    except (ModuleNotFoundError, FileNotFoundError):
        return None
    return path if path.is_dir() else None


def _resolve_default_data_dir() -> Path:
    cwd_data = Path.cwd() / "data"
    if cwd_data.is_dir() and any(cwd_data.glob("*/manifest.json")):
        return cwd_data.resolve()
    pkg = _packaged_data_dir()
    if pkg is not None and any(pkg.glob("*/manifest.json")):
        return pkg
    return cwd_data


def _data_dir(value: str | None) -> Path:
    return Path(value).resolve() if value else _resolve_default_data_dir()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VERSION_NAME_RE = re.compile(r"^\d{8}$")


def _list_versions(data_dir: Path) -> list[str]:
    if not data_dir.exists():
        return []
    dirs = [
        p.name
        for p in data_dir.iterdir()
        if p.is_dir()
        and _VERSION_NAME_RE.match(p.name)
        and (p / "manifest.json").exists()
    ]
    dirs.sort()
    return dirs


def _resolve_version(data_dir: Path, version: str | None) -> str:
    versions = _list_versions(data_dir)
    if not versions:
        sys.exit(
            f"error: no extracted versions under {data_dir}\n"
            "hint: run `reseden fetch <PDF URL>` to create one, "
            "or pass --out-dir <DIR> to point at existing data."
        )
    if version is None:
        return versions[-1]
    if version not in versions:
        sys.exit(
            f"error: version {version!r} not found.\n"
            f"hint: available = [{', '.join(versions)}]. Try `reseden versions`."
        )
    return version


def _load_manifest(data_dir: Path, version: str) -> dict[str, Any]:
    path = data_dir / version / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _master_ids(data_dir: Path, version: str) -> list[str]:
    return [m["masterId"] for m in _load_manifest(data_dir, version)["masters"]]


def _load_master(data_dir: Path, version: str, master_id: str) -> dict[str, Any]:
    path = data_dir / version / f"{master_id}.json"
    if not path.exists():
        available = _master_ids(data_dir, version)
        sys.exit(
            f"error: master {master_id!r} not found in version {version}.\n"
            f"hint: available masterIds = [{', '.join(available)}]. "
            f"Try `reseden masters`."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _snippet(text: str, keyword: str, width: int = 60) -> str:
    """text の keyword ヒット周辺を切り出す。単行スニペット。"""
    if not text:
        return ""
    flat = text.replace("\n", " ")
    idx = flat.lower().find(keyword.lower())
    if idx < 0:
        return flat[:width].strip() + ("…" if len(flat) > width else "")
    half = width // 2
    start = max(0, idx - half)
    end = min(len(flat), idx + len(keyword) + half)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return prefix + flat[start:end].strip() + suffix


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    from . import __version__ as pkg_version

    data_dir = _data_dir(args.out_dir)
    versions = _list_versions(data_dir)
    payload: dict[str, Any] = {
        "cliVersion": pkg_version,
        "dataDir": str(data_dir),
        "availableVersions": versions,
        "latestVersion": versions[-1] if versions else None,
    }
    if versions:
        manifest = _load_manifest(data_dir, versions[-1])
        payload["latest"] = {
            "sourcePdf": manifest.get("sourcePdf"),
            "sourceUrl": manifest.get("sourceUrl"),
            "extractorVersion": manifest.get("extractorVersion"),
            "extractedAt": manifest.get("extractedAt"),
            "masterCount": len(manifest["masters"]),
            "masterIds": [m["masterId"] for m in manifest["masters"]],
        }
    payload["hints"] = [
        "`reseden masters` でマスター一覧を取得",
        "`reseden fields <masterId>` でそのマスターの全フィールド",
        "`reseden field <masterId> <seq>` で1フィールドの詳細（codes含む）",
        "`reseden search <keyword>` で全マスター横断検索",
        "`reseden schema` で出力スキーマの概要",
    ]
    _dump(payload)
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    """出力 JSON のスキーマ概要を返す。AI が構造を把握するのに使う想定。"""
    schema = {
        "master": {
            "masterId": "string (例: iyakuhin)",
            "masterName": "string",
            "subName": "string | null (サブテーブル名)",
            "version": "string (YYYYMMDD)",
            "pages": {"start": "int", "end": "int"},
            "fields": "list[field]",
        },
        "field": {
            "seq": "int (1始まり)",
            "name": "string (フル項目名、サブ項目は '/' 区切り)",
            "shortName": "string?（サブ項目のみ）",
            "mode": "'numeric' | 'alphanumeric' | 'text' | 'date' | null",
            "maxBytes": "int?",
            "itemFormat": "'固定' | '可変' | string | null",
            "description": "string?",
            "codes": "list[code]? （列挙値がある項目のみ）",
            "flags": "dict? （共通区分などの追加メタ）",
        },
        "code": {
            "code": "string (NFKC 正規化済み)",
            "name": "string",
        },
        "manifest": {
            "version": "string",
            "sourcePdf": "string",
            "sourceUrl": "string | null",
            "sourceSha256": "string",
            "extractorVersion": "string",
            "extractedAt": "ISO8601",
            "dependencies": "dict",
            "masters": "list[{masterId, masterName, subName, pages, file, fieldCount}]",
        },
    }
    _dump(schema)
    return 0


def cmd_skill(args: argparse.Namespace) -> int:
    """同梱されている SKILL.md をそのまま stdout に書き出す。

    エージェント環境（Claude Code / Cursor / Codex など）ごとに設置場所が違うため、
    このコマンドは出力するだけに留め、設置はユーザーに任せる:

      reseden skill > ~/.claude/skills/reseden-master-spec/SKILL.md
      reseden skill >> AGENTS.md
    """
    resource = _pkg_files("reseden_master_spec") / "SKILL.md"
    path = Path(str(resource))
    if not path.exists():
        sys.exit(
            "error: bundled SKILL.md not found.\n"
            "hint: re-install via `uv tool install --force 'git+https://github.com/kohii/reseden-master-spec'`."
        )
    sys.stdout.write(path.read_text(encoding="utf-8"))
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    raw_dir = data_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    url = args.url
    file_name = Path(urlparse(url).path).name
    if not file_name.endswith(".pdf"):
        sys.exit(f"error: URL does not look like a PDF: {url}")
    pdf_path = raw_dir / file_name
    if args.force or not pdf_path.exists():
        print(f"[fetch] downloading {url}", file=sys.stderr)
        req = Request(url, headers={"User-Agent": "reseden-master-spec/0.1"})
        with urlopen(req) as resp, pdf_path.open("wb") as f:
            f.write(resp.read())
    else:
        print(f"[fetch] using cached {pdf_path}", file=sys.stderr)

    version = args.version or extractor.infer_version_from_path(pdf_path)
    if version == "unknown":
        sys.exit(
            f"error: could not infer version (YYYYMMDD) from filename {pdf_path.name!r}.\n"
            "hint: pass --version YYYYMMDD explicitly."
        )
    out_dir = data_dir / version
    print(f"[fetch] extracting to {out_dir}", file=sys.stderr)
    return extractor.main([str(pdf_path), str(out_dir), version, url])


def cmd_versions(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    _dump({"dataDir": str(data_dir), "versions": _list_versions(data_dir)})
    return 0


def cmd_masters(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    version = _resolve_version(data_dir, args.version)
    manifest = _load_manifest(data_dir, version)
    rows = []
    for m in manifest["masters"]:
        full_name = m["masterName"] + (f" / {m['subName']}" if m.get("subName") else "")
        rows.append(
            {
                "masterId": m["masterId"],
                "masterName": full_name,
                "fieldCount": m["fieldCount"],
                "pages": m["pages"],
            }
        )
    _dump(
        {
            "version": version,
            "masterCount": len(rows),
            "masters": rows,
            "hint": "次: reseden fields <masterId>",
        }
    )
    return 0


def cmd_fields(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    version = _resolve_version(data_dir, args.version)
    master = _load_master(data_dir, version, args.master_id)
    fields = master["fields"]
    summary = [
        {
            "seq": f["seq"],
            "name": f["name"],
            "mode": f.get("mode"),
            "maxBytes": f.get("maxBytes"),
            "hasCodes": bool(f.get("codes")),
        }
        for f in fields
    ]
    payload: dict[str, Any] = {
        "version": version,
        "masterId": master["masterId"],
        "masterName": master["masterName"],
        "subName": master.get("subName"),
        "fieldCount": len(fields),
    }
    if args.summary:
        payload["fields"] = summary
        payload["hint"] = "次: reseden field <masterId> <seq> で詳細"
    else:
        payload["fields"] = fields
    _dump(payload)
    return 0


def cmd_field(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    version = _resolve_version(data_dir, args.version)
    master = _load_master(data_dir, version, args.master_id)
    all_seqs = sorted({f["seq"] for f in master["fields"]})
    matched = [f for f in master["fields"] if f["seq"] == args.seq]
    if not matched:
        # 近傍 seq を提案
        near = sorted(all_seqs, key=lambda s: abs(s - args.seq))[:5]
        sys.exit(
            f"error: no field with seq={args.seq} in {args.master_id}.\n"
            f"hint: available seq range = {all_seqs[0]}..{all_seqs[-1]}, "
            f"nearest = {near}. Try `reseden fields {args.master_id} --summary`."
        )
    payload: dict[str, Any] = {
        "version": version,
        "masterId": master["masterId"],
        "fields": matched,
    }
    # codes がある field はコード検索の誘導を stderr で
    for f in matched:
        if f.get("codes"):
            print(
                f"[hint] seq={f['seq']} has {len(f['codes'])} codes. "
                f"use `reseden code {master['masterId']} {f['seq']} <code>` to look up.",
                file=sys.stderr,
            )
    _dump(payload)
    return 0


def cmd_code(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    version = _resolve_version(data_dir, args.version)
    master = _load_master(data_dir, version, args.master_id)
    code_query = extractor.normalize_code(args.code)
    field = next((f for f in master["fields"] if f["seq"] == args.seq), None)
    if field is None:
        sys.exit(
            f"error: no field with seq={args.seq} in {args.master_id}.\n"
            f"hint: try `reseden field {args.master_id} {args.seq}` first."
        )
    codes = field.get("codes", [])
    if not codes:
        sys.exit(
            f"error: seq={args.seq} ({field['name']!r}) has no enumerated codes.\n"
            f"hint: this field is a free-form value. See `reseden field {args.master_id} {args.seq}`."
        )
    hits = [
        {"field": field["name"], "code": c}
        for c in codes
        if extractor.normalize_code(c["code"]) == code_query
    ]
    if not hits:
        available = [c["code"] for c in codes]
        sys.exit(
            f"error: code {args.code!r} not found in {args.master_id} seq={args.seq}.\n"
            f"hint: available codes = [{', '.join(available)}]."
        )
    _dump({"version": version, "masterId": master["masterId"], "hits": hits})
    return 0


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

_VALID_MODES = {"numeric", "alphanumeric", "text", "date", ""}
_NAME_TRUNC_SUFFIXES = ("グルー", "コ", "ロ")
_MISSING_RATIO_ERROR = 0.2
_MISSING_ABS_ERROR = 20


def _verify_master(master: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    fields = master["fields"]
    if not fields:
        issues.append({"severity": "error", "message": "no fields extracted"})
        return issues

    seqs = [f["seq"] for f in fields]
    seq_set = sorted(set(seqs))
    expected = list(range(seq_set[0], seq_set[-1] + 1))
    missing = sorted(set(expected) - set(seq_set))
    if missing:
        missing_ratio = len(missing) / max(1, len(expected))
        severity = (
            "error"
            if (
                len(missing) >= _MISSING_ABS_ERROR
                or missing_ratio >= _MISSING_RATIO_ERROR
            )
            else "warning"
        )
        issues.append(
            {
                "severity": severity,
                "message": f"missing seq numbers: {missing[:20]}"
                + ("..." if len(missing) > 20 else ""),
                "missingCount": len(missing),
                "missingRatio": round(missing_ratio, 3),
            }
        )

    for f in fields:
        name = f.get("name", "")
        if not name:
            issues.append({"severity": "error", "seq": f["seq"], "message": "empty name"})
        elif "\n" in name:
            issues.append(
                {
                    "severity": "warning",
                    "seq": f["seq"],
                    "message": f"name contains newline: {name!r}",
                }
            )
        elif any(name.split("/")[-1].endswith(sfx) for sfx in _NAME_TRUNC_SUFFIXES) and len(name) <= 12:
            issues.append(
                {
                    "severity": "warning",
                    "seq": f["seq"],
                    "message": f"name may be truncated mid-word: {name!r}",
                }
            )
        mode = f.get("mode", "")
        if mode not in _VALID_MODES:
            issues.append(
                {
                    "severity": "warning",
                    "seq": f["seq"],
                    "message": f"unexpected mode value: {mode!r}",
                }
            )
        for c in f.get("codes", []):
            if len(c.get("name", "")) > 200:
                issues.append(
                    {
                        "severity": "warning",
                        "seq": f["seq"],
                        "code": c["code"],
                        "message": "code name too long (>200 chars); may contain spillover description",
                    }
                )
    return issues


def cmd_verify(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    version = _resolve_version(data_dir, args.version)
    baseline_version = _resolve_version(data_dir, args.baseline) if args.baseline else None
    manifest = _load_manifest(data_dir, version)
    report: dict[str, Any] = {"version": version, "masters": [], "ok": True}
    for m in manifest["masters"]:
        master = _load_master(data_dir, version, m["masterId"])
        issues = _verify_master(master)
        entry = {
            "masterId": m["masterId"],
            "fieldCount": len(master["fields"]),
            "issues": issues,
        }
        if baseline_version:
            baseline_manifest = _load_manifest(data_dir, baseline_version)
            baseline_m = next(
                (bm for bm in baseline_manifest["masters"] if bm["masterId"] == m["masterId"]),
                None,
            )
            if baseline_m is None:
                entry["baselineDiff"] = {"status": "added"}
            else:
                diff = len(master["fields"]) - baseline_m["fieldCount"]
                entry["baselineDiff"] = {
                    "status": "changed" if diff else "same",
                    "fieldCountDelta": diff,
                    "baselineFieldCount": baseline_m["fieldCount"],
                }
                if baseline_m["fieldCount"] > 0 and abs(diff) / baseline_m["fieldCount"] > 0.2:
                    issues.append(
                        {
                            "severity": "warning",
                            "message": (
                                f"field count changed by {diff:+d} "
                                f"({diff*100/baseline_m['fieldCount']:+.0f}%) vs {baseline_version}"
                            ),
                        }
                    )
        if any(i["severity"] == "error" for i in issues):
            report["ok"] = False
        report["masters"].append(entry)

    if baseline_version:
        baseline_manifest = _load_manifest(data_dir, baseline_version)
        base_ids = {bm["masterId"] for bm in baseline_manifest["masters"]}
        cur_ids = {m["masterId"] for m in manifest["masters"]}
        report["baseline"] = baseline_version
        report["baselineRemoved"] = sorted(base_ids - cur_ids)
        report["baselineAdded"] = sorted(cur_ids - base_ids)

    _dump(report)
    return 0 if report["ok"] else 1


def cmd_search(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    version = _resolve_version(data_dir, args.version)
    manifest = _load_manifest(data_dir, version)
    pattern = re.compile(re.escape(args.keyword), re.IGNORECASE)
    target_ids: list[str] = (
        [args.master_id] if args.master_id else [m["masterId"] for m in manifest["masters"]]
    )
    results: list[dict[str, Any]] = []
    for mid in target_ids:
        master = _load_master(data_dir, version, mid)
        for f in master["fields"]:
            name = f.get("name", "") or ""
            short = f.get("shortName", "") or ""
            desc = f.get("description", "") or ""
            # どこでマッチしたか
            where: list[str] = []
            if pattern.search(name):
                where.append("name")
            if short and pattern.search(short):
                where.append("shortName")
            if desc and pattern.search(desc):
                where.append("description")
            # codes 内のマッチ
            code_hits: list[dict[str, str]] = []
            for c in f.get("codes", []):
                if pattern.search(c.get("name", "")):
                    code_hits.append(c)
            if where or code_hits:
                hit: dict[str, Any] = {
                    "masterId": master["masterId"],
                    "masterName": master["masterName"],
                    "seq": f["seq"],
                    "name": name,
                    "where": where or ["code"],
                }
                if "description" in where:
                    hit["snippet"] = _snippet(desc, args.keyword)
                if code_hits:
                    hit["codes"] = code_hits[:5]
                results.append(hit)
    limit = args.limit if args.limit and args.limit > 0 else None
    truncated = False
    if limit and len(results) > limit:
        truncated = True
        results = results[:limit]
    _dump(
        {
            "version": version,
            "keyword": args.keyword,
            "hitCount": len(results),
            "truncated": truncated,
            "hits": results,
        }
    )
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _sub(sub: Any, name: str, *, help: str, description: str, epilog: str = "") -> argparse.ArgumentParser:
    return sub.add_parser(
        name,
        help=help,
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    from . import __version__ as _pkg_version

    p = argparse.ArgumentParser(
        prog="reseden",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"reseden-master-spec {_pkg_version}",
    )
    p.add_argument("--out-dir", help="データディレクトリ（省略時は docstring 参照）")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    sp = _sub(
        sub,
        "info",
        help="同梱データの概要と主要サブコマンドの案内を JSON で返す",
        description="AI/人が最初に叩くと CLI の全体像が掴める。cliVersion / dataDir / latestVersion / masterIds / hints を返す。",
        epilog="example:\n  reseden info",
    )
    sp.add_argument("--version", metavar="YYYYMMDD", help="データの版（省略時は最新）")
    sp.set_defaults(func=cmd_info)

    sp = _sub(
        sub,
        "schema",
        help="出力 JSON のスキーマ概要を返す",
        description="master / field / code / manifest の各スキーマを human-readable な JSON として返す。",
    )
    sp.set_defaults(func=cmd_schema)

    sp = _sub(
        sub,
        "versions",
        help="抽出済みの版 (YYYYMMDD) 一覧",
        description="--out-dir で指す場所に存在する YYYYMMDD ディレクトリを列挙する。",
    )
    sp.set_defaults(func=cmd_versions)

    sp = _sub(
        sub,
        "masters",
        help="全マスター一覧（masterId / 名称 / fieldCount）",
        description="指定版のマスター一覧を返す。まず何があるかを確認する時に使う。",
        epilog="example:\n  reseden masters\n  reseden masters --version 20260331",
    )
    sp.add_argument("--version", metavar="YYYYMMDD", help="データの版（省略時は最新）")
    sp.set_defaults(func=cmd_masters)

    sp = _sub(
        sub,
        "fields",
        help="指定マスターの全フィールド",
        description="master_id を指定してその全 field を返す。--summary でコンパクトな一覧に。",
        epilog=(
            "example:\n"
            "  reseden fields iyakuhin --summary\n"
            "  reseden fields shika_shinryoukoui_ケ"
        ),
    )
    sp.add_argument("master_id", metavar="<masterId>")
    sp.add_argument(
        "--summary",
        action="store_true",
        help="seq / name / mode / maxBytes / hasCodes だけの簡略出力",
    )
    sp.add_argument("--version", metavar="YYYYMMDD")
    sp.set_defaults(func=cmd_fields)

    sp = _sub(
        sub,
        "field",
        help="1フィールドの詳細（codes 含む）",
        description="seq 指定で 1 項目の全属性を返す。存在しない seq は近傍候補をエラーメッセージで提示。",
        epilog=(
            "example:\n"
            "  reseden field iyakuhin 14\n"
            "  reseden field shika_shinryoukoui_ケ 10"
        ),
    )
    sp.add_argument("master_id", metavar="<masterId>")
    sp.add_argument("seq", type=int, metavar="<seq>")
    sp.add_argument("--version", metavar="YYYYMMDD")
    sp.set_defaults(func=cmd_field)

    sp = _sub(
        sub,
        "code",
        help="指定フィールドのコード値名称を引く",
        description="enumerated code のみ対象。ない場合は field の codes 一覧で候補表示。",
        epilog=(
            "example:\n"
            "  reseden code iyakuhin 14 3       # -> 覚醒剤原料\n"
            "  reseden code ika_shinryoukoui 99 BK"
        ),
    )
    sp.add_argument("master_id", metavar="<masterId>")
    sp.add_argument("seq", type=int, metavar="<seq>")
    sp.add_argument("code", metavar="<code>", help="NFKC 正規化されて比較される")
    sp.add_argument("--version", metavar="YYYYMMDD")
    sp.set_defaults(func=cmd_code)

    sp = _sub(
        sub,
        "search",
        help="全マスター横断キーワード検索（name / shortName / description / code.name）",
        description=(
            "正規表現エスケープした keyword で大小無視検索。hit ごとに where (どこで当たったか)、"
            "description 由来なら snippet、code.name 由来なら codes 配列を含める。"
        ),
        epilog=(
            "example:\n"
            "  reseden search 後発品\n"
            "  reseden search 加算 --master-id ika_shinryoukoui --limit 10"
        ),
    )
    sp.add_argument("keyword", metavar="<keyword>")
    sp.add_argument("--master-id", dest="master_id", metavar="<masterId>", help="検索対象を1マスターに絞る")
    sp.add_argument("--limit", type=int, metavar="N", help="hit を N 件で打ち切る")
    sp.add_argument("--version", metavar="YYYYMMDD")
    sp.set_defaults(func=cmd_search)

    sp = _sub(
        sub,
        "verify",
        help="抽出結果の健全性チェック（exit 0=OK / 1=error）",
        description="seq 飛び、空name、modeの異常、コード名の過長などを検出する。--baseline で前版との差分も。",
        epilog="example:\n  reseden verify\n  reseden verify --baseline 20260331",
    )
    sp.add_argument("--version", metavar="YYYYMMDD")
    sp.add_argument("--baseline", metavar="YYYYMMDD", help="比較対象の版")
    sp.set_defaults(func=cmd_verify)

    sp = _sub(
        sub,
        "skill",
        help="エージェント用 SKILL.md を stdout に出力",
        description=(
            "同梱の SKILL.md（この CLI の使い方ガイド）をそのまま stdout に書き出す。"
            "設置先は利用者が選ぶ（Claude Code / Cursor / Codex などで場所が違うため）。"
        ),
        epilog=(
            "example:\n"
            "  mkdir -p ~/.claude/skills/reseden-master-spec\n"
            "  reseden skill > ~/.claude/skills/reseden-master-spec/SKILL.md\n"
            "\n"
            "  # or concatenate into an existing AGENTS.md\n"
            "  reseden skill >> AGENTS.md"
        ),
    )
    sp.set_defaults(func=cmd_skill)

    sp = _sub(
        sub,
        "fetch",
        help="PDF URL から取得して JSON 化 (開発者向け / Poppler 必須)",
        description="PDFファイルをダウンロードし、data/<YYYYMMDD>/ に抽出 JSON を書き出す。",
        epilog="example:\n  reseden fetch https://.../master_1_20260331.pdf",
    )
    sp.add_argument("url")
    sp.add_argument("--force", action="store_true", help="キャッシュがあっても再ダウンロード")
    sp.add_argument("--version", metavar="YYYYMMDD", help="推定された版を上書き")
    sp.set_defaults(func=cmd_fetch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
