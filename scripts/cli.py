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
    out_dir = data_dir / version
    print(f"[fetch] extracting to {out_dir}", file=sys.stderr)
    return extractor.main([str(pdf_path), str(out_dir), version])


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


_FULLWIDTH_TO_HALF = str.maketrans(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
)


def cmd_code(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args.out_dir)
    version = _resolve_version(data_dir, args.version)
    master = _load_master(data_dir, version, args.master_id)
    code_query = args.code.translate(_FULLWIDTH_TO_HALF)
    found: list[dict[str, Any]] = []
    for f in master["fields"]:
        if f["seq"] != args.seq:
            continue
        for c in f.get("codes", []):
            if c["code"] == code_query:
                found.append({"field": f["name"], "code": c})
    if not found:
        sys.exit(
            f"error: no code {args.code!r} found in {args.master_id} seq={args.seq}"
        )
    _dump({"version": version, "masterId": master["masterId"], "hits": found})
    return 0


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
