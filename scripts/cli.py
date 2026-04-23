"""kihon-master-spec CLI

URLからPDFを取得してJSON化するfetch、および抽出済みJSONを検索するクエリを提供する。

Usage:
    uv run scripts/cli.py fetch <url> [--out-dir DIR]
    uv run scripts/cli.py versions [--out-dir DIR]
    uv run scripts/cli.py masters [--version V] [--out-dir DIR]
    uv run scripts/cli.py fields <master_id> [--version V] [--out-dir DIR]
    uv run scripts/cli.py field <master_id> <seq> [--version V] [--out-dir DIR]
    uv run scripts/cli.py code <master_id> <seq> <code> [--version V] [--out-dir DIR]
    uv run scripts/cli.py search <keyword> [--master-id M] [--version V] [--out-dir DIR]

デフォルトの --out-dir は "./data"。
--version を省略した場合は最新（ファイル名ソート順の最後）のバージョンを使う。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import extract as extractor

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _data_dir(value: str | None) -> Path:
    return Path(value).resolve() if value else DEFAULT_DATA_DIR


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
        sys.exit(f"error: no extracted versions under {data_dir}")
    if version is None:
        return versions[-1]
    if version not in versions:
        sys.exit(
            f"error: version {version!r} not found. available: {', '.join(versions)}"
        )
    return version


def _load_manifest(data_dir: Path, version: str) -> dict[str, Any]:
    path = data_dir / version / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _load_master(data_dir: Path, version: str, master_id: str) -> dict[str, Any]:
    path = data_dir / version / f"{master_id}.json"
    if not path.exists():
        sys.exit(f"error: master {master_id!r} not found in version {version}")
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


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
        req = Request(url, headers={"User-Agent": "kihon-master-spec/0.1"})
        with urlopen(req) as resp, pdf_path.open("wb") as f:
            f.write(resp.read())
    else:
        print(f"[fetch] using cached {pdf_path}", file=sys.stderr)

    version = args.version or extractor.infer_version_from_path(pdf_path)
    if version == "unknown":
        sys.exit(
            f"error: could not infer version (YYYYMMDD) from filename {pdf_path.name!r}. "
            "Pass --version explicitly."
        )
    out_dir = data_dir / version
    print(f"[fetch] extracting to {out_dir}", file=sys.stderr)
    return extractor.main([str(pdf_path), str(out_dir), version, url])


def cmd_versions(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    for v in _list_versions(data_dir):
        print(v)
    return 0


def cmd_masters(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    version = _resolve_version(data_dir, args.version)
    manifest = _load_manifest(data_dir, version)
    rows = [
        {
            "masterId": m["masterId"],
            "masterName": m["masterName"]
            + (f" / {m['subName']}" if m.get("subName") else ""),
            "fieldCount": m["fieldCount"],
            "pages": m["pages"],
        }
        for m in manifest["masters"]
    ]
    _dump({"version": version, "masters": rows})
    return 0


def cmd_fields(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    version = _resolve_version(data_dir, args.version)
    master = _load_master(data_dir, version, args.master_id)
    _dump(
        {
            "version": version,
            "masterId": master["masterId"],
            "masterName": master["masterName"],
            "subName": master.get("subName"),
            "fields": master["fields"],
        }
    )
    return 0


def cmd_field(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    version = _resolve_version(data_dir, args.version)
    master = _load_master(data_dir, version, args.master_id)
    matched = [f for f in master["fields"] if f["seq"] == args.seq]
    if not matched:
        sys.exit(f"error: no field with seq={args.seq}")
    _dump({"version": version, "masterId": master["masterId"], "fields": matched})
    return 0


def cmd_code(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    version = _resolve_version(data_dir, args.version)
    master = _load_master(data_dir, version, args.master_id)
    code_query = extractor.normalize_code(args.code)
    found: list[dict[str, Any]] = []
    for f in master["fields"]:
        if f["seq"] != args.seq:
            continue
        for c in f.get("codes", []):
            if extractor.normalize_code(c["code"]) == code_query:
                found.append({"field": f["name"], "code": c})
    if not found:
        sys.exit(
            f"error: no code {args.code!r} found in {args.master_id} seq={args.seq}"
        )
    _dump({"version": version, "masterId": master["masterId"], "hits": found})
    return 0


_VALID_MODES = {"numeric", "alphanumeric", "text", "date", ""}
_NAME_TRUNC_SUFFIXES = ("グルー", "コ", "ロ")  # 項目名の末尾が途中切れっぽい断片
_MISSING_RATIO_ERROR = 0.2  # missing / expected がこの比率を超えたら error
_MISSING_ABS_ERROR = 20  # missingCount がこの絶対値を超えたら error


def _verify_master(master: dict[str, Any]) -> list[dict[str, Any]]:
    """1マスターの自己整合性をチェック。問題を dict のリストで返す。"""
    issues: list[dict[str, Any]] = []
    fields = master["fields"]
    if not fields:
        issues.append({"severity": "error", "message": "no fields extracted"})
        return issues

    seqs = [f["seq"] for f in fields]
    seq_set = sorted(set(seqs))
    # seqの飛び（範囲表記の取りこぼしや pdfplumber 欠落の検出）
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
            issues.append(
                {"severity": "error", "seq": f["seq"], "message": "empty name"}
            )
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
    report: dict[str, Any] = {
        "version": version,
        "masters": [],
        "ok": True,
    }
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
    target_ids: list[str] = [args.master_id] if args.master_id else [
        m["masterId"] for m in manifest["masters"]
    ]
    results: list[dict[str, Any]] = []
    for mid in target_ids:
        master = _load_master(data_dir, version, mid)
        for f in master["fields"]:
            hay = "\n".join(
                [
                    f.get("name", ""),
                    f.get("shortName", "") or "",
                    f.get("description", "") or "",
                ]
            )
            if pattern.search(hay):
                results.append(
                    {
                        "masterId": master["masterId"],
                        "masterName": master["masterName"],
                        "seq": f["seq"],
                        "name": f["name"],
                    }
                )
    _dump({"version": version, "keyword": args.keyword, "hits": results})
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kms", description=__doc__)
    p.add_argument(
        "--out-dir",
        help="Data directory (default: ./data)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("fetch", help="Download PDF from URL and extract JSON")
    sp.add_argument("url")
    sp.add_argument("--force", action="store_true", help="Re-download even if cached")
    sp.add_argument("--version", help="Override inferred version (YYYYMMDD)")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("versions", help="List available versions")
    sp.set_defaults(func=cmd_versions)

    sp = sub.add_parser("masters", help="List masters for a version")
    sp.add_argument("--version")
    sp.set_defaults(func=cmd_masters)

    sp = sub.add_parser("fields", help="Show all fields of a master")
    sp.add_argument("master_id")
    sp.add_argument("--version")
    sp.set_defaults(func=cmd_fields)

    sp = sub.add_parser("field", help="Show detail of a specific field (by seq)")
    sp.add_argument("master_id")
    sp.add_argument("seq", type=int)
    sp.add_argument("--version")
    sp.set_defaults(func=cmd_field)

    sp = sub.add_parser("code", help="Look up a code value within a field")
    sp.add_argument("master_id")
    sp.add_argument("seq", type=int)
    sp.add_argument("code")
    sp.add_argument("--version")
    sp.set_defaults(func=cmd_code)

    sp = sub.add_parser(
        "verify", help="Check extracted JSON for anomalies (seq gaps, empty names, etc.)"
    )
    sp.add_argument("--version")
    sp.add_argument("--baseline", help="Compare with another version for regression detection")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("search", help="Keyword search within field names/descriptions")
    sp.add_argument("keyword")
    sp.add_argument("--master-id", dest="master_id")
    sp.add_argument("--version")
    sp.set_defaults(func=cmd_search)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
