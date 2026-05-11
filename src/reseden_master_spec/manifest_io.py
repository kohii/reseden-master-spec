"""manifest.json の読み書きヘルパ。

manifest.json は `data/<YYYYMMDD>/manifest.json` に1つだけ存在し、
複数の source PDF（master_0 = 基本マスター仕様書、master_2 = 別紙）から
抽出した結果を1つに集約する。

スキーマ:
{
  "version": "YYYYMMDD",                    // 適用日（ディレクトリ名）
  "extractorVersion": "x.y.z",
  "extractedAt": "ISO8601",                 // 最後に書き込んだ時刻
  "dependencies": {...},
  "sources": [
    {
      "kind": "master" | "appendix",
      "sourcePdf": "master_0_YYYYMMDD.pdf",
      "sourceUrl": "...",
      "sourceSha256": "...",
      "sourceVersion": "YYYYMMDD",          // PDFファイル名由来
      "extractedAt": "ISO8601"
    }
  ],
  "masters":    [ ... ],                    // master_0 由来
  "codeTables": [ ... ]                     // master_2 由来
}
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_VERSION_DIR_RE = re.compile(r"^\d{8}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(out_dir: Path) -> dict[str, Any] | None:
    p = out_dir / "manifest.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def init_manifest(
    version: str, extractor_version: str, dependencies: dict[str, str]
) -> dict[str, Any]:
    return {
        "version": version,
        "extractorVersion": extractor_version,
        "extractedAt": now_iso(),
        "dependencies": dependencies,
        "sources": [],
        "masters": [],
        "codeTables": [],
    }


def ensure_shape(manifest: dict[str, Any]) -> dict[str, Any]:
    """旧スキーマや欠損キーを補正して返す（mutateする）。

    旧スキーマでは sourcePdf/sourceUrl/sourceSha256 がトップレベルにあった。
    これを sources[kind=master] に移してトップレベルからは除去する。
    """
    manifest.setdefault("sources", [])
    manifest.setdefault("masters", [])
    manifest.setdefault("codeTables", [])
    legacy_pdf = manifest.pop("sourcePdf", None)
    legacy_url = manifest.pop("sourceUrl", None)
    legacy_sha = manifest.pop("sourceSha256", None)
    if legacy_pdf and not any(s.get("kind") == "master" for s in manifest["sources"]):
        version_match = re.search(r"(\d{8})", legacy_pdf)
        manifest["sources"].append(
            {
                "kind": "master",
                "sourcePdf": legacy_pdf,
                "sourceUrl": legacy_url,
                "sourceSha256": legacy_sha,
                "sourceVersion": version_match.group(1) if version_match else manifest.get("version"),
                "extractedAt": manifest.get("extractedAt"),
            }
        )
    return manifest


def upsert_source(
    manifest: dict[str, Any],
    *,
    kind: str,
    source_pdf: str,
    source_url: str | None,
    source_sha256: str,
    source_version: str,
) -> None:
    """同じ kind が既にあれば置換、なければ追加"""
    sources = manifest.setdefault("sources", [])
    entry = {
        "kind": kind,
        "sourcePdf": source_pdf,
        "sourceUrl": source_url,
        "sourceSha256": source_sha256,
        "sourceVersion": source_version,
        "extractedAt": now_iso(),
    }
    for i, src in enumerate(sources):
        if src.get("kind") == kind:
            sources[i] = entry
            return
    sources.append(entry)


def write_manifest(out_dir: Path, manifest: dict[str, Any]) -> None:
    manifest["extractedAt"] = now_iso()
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def list_version_dirs(data_dir: Path) -> list[Path]:
    if not data_dir.exists():
        return []
    return sorted(
        p
        for p in data_dir.iterdir()
        if p.is_dir() and _VERSION_DIR_RE.match(p.name) and (p / "manifest.json").exists()
    )


def find_latest_version_dir(data_dir: Path, exclude: str | None = None) -> Path | None:
    dirs = list_version_dirs(data_dir)
    if exclude:
        dirs = [d for d in dirs if d.name != exclude]
    return dirs[-1] if dirs else None


def seed_from_previous(
    data_dir: Path, new_version: str, *, exclude_kind: str | None = None
) -> Path:
    """`data/<new_version>/` を作成。既存最新版があれば、その内容を新ディレクトリに
    コピーして「片側だけ更新」した状態を作る。

    exclude_kind を指定すると、その種別の成果物はコピーしない（直後にこれから上書きする想定）。
    """
    new_dir = data_dir / new_version
    if new_dir.exists():
        return new_dir
    prev = find_latest_version_dir(data_dir, exclude=new_version)
    new_dir.mkdir(parents=True, exist_ok=True)
    if prev is None:
        return new_dir
    prev_manifest = read_manifest(prev)
    if prev_manifest is None:
        return new_dir
    ensure_shape(prev_manifest)

    # exclude_kind に対応するファイル名集合を取得
    skip_files: set[str] = set()
    if exclude_kind == "master":
        skip_files = {m["file"] for m in prev_manifest.get("masters", [])}
        # masters[] と master source は新規取得側で上書きするのでメタからも除外
    elif exclude_kind == "appendix":
        skip_files = {c["file"] for c in prev_manifest.get("codeTables", [])}

    # ファイルコピー
    for src in prev.iterdir():
        if src.is_dir():
            continue
        if src.name == "manifest.json":
            continue
        if src.name in skip_files:
            continue
        (new_dir / src.name).write_bytes(src.read_bytes())

    # manifest コピー（version を上書き、exclude_kind 側は空に）
    new_manifest = json.loads(json.dumps(prev_manifest))  # deepcopy
    new_manifest["version"] = new_version
    if exclude_kind == "master":
        new_manifest["masters"] = []
        new_manifest["sources"] = [
            s for s in new_manifest.get("sources", []) if s.get("kind") != "master"
        ]
    elif exclude_kind == "appendix":
        new_manifest["codeTables"] = []
        new_manifest["sources"] = [
            s for s in new_manifest.get("sources", []) if s.get("kind") != "appendix"
        ]
    write_manifest(new_dir, new_manifest)
    return new_dir
