#!/usr/bin/env python3
"""Safely relabel local Codex sessions between model_provider buckets."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import tomllib
import uuid
import zipfile
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


TOOL_VERSION = "0.2.0"
MANIFEST_NAME = "manifest.json"
TRANSFER_MANIFEST_NAME = "codex-transfer.json"
TRANSFER_FORMAT_VERSION = 1
SESSION_DIRS = (("sessions", 8), ("archived_sessions", 4))


class MigrationError(RuntimeError):
    pass


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def default_codex_dir() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def default_backup_dir() -> Path:
    return Path.home() / ".codex-session-migrator" / "backups"


def canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def ensure_safe_codex_dir(path: Path) -> Path:
    result = canonical(path)
    if result == Path(result.anchor):
        raise MigrationError(f"拒绝把文件系统根目录作为 Codex 目录：{result}")
    return result


def iter_files_limited(root: Path, suffix: str, max_depth: int) -> Iterator[Path]:
    if not root.is_dir():
        return
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError as exc:
            print(f"警告：无法读取 {current}: {exc}", file=sys.stderr)
            continue
        for entry in entries:
            try:
                if entry.is_dir() and depth < max_depth:
                    stack.append((entry, depth + 1))
                elif entry.is_file() and entry.suffix.lower() == suffix:
                    yield entry
            except OSError as exc:
                print(f"警告：无法检查 {entry}: {exc}", file=sys.stderr)


def iter_session_files(codex_dir: Path) -> Iterator[Path]:
    for dirname, depth in SESSION_DIRS:
        yield from iter_files_limited(codex_dir / dirname, ".jsonl", depth)


def parse_session_meta_line(line: str) -> dict[str, Any] | None:
    if '"session_meta"' not in line or '"model_provider"' not in line:
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    if value.get("type") != "session_meta" or not isinstance(value.get("payload"), dict):
        return None
    return value


def read_session_meta(path: Path) -> tuple[str | None, str | None]:
    try:
        with path.open("r", encoding="utf-8", errors="strict") as handle:
            for line in handle:
                value = parse_session_meta_line(line)
                if value is not None:
                    payload = value["payload"]
                    return payload.get("id"), payload.get("model_provider")
    except (OSError, UnicodeError) as exc:
        print(f"警告：无法解析 {path}: {exc}", file=sys.stderr)
    return None, None


def read_full_session_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8", errors="strict") as handle:
            for line in handle:
                value = parse_session_meta_line(line)
                if value is not None:
                    return value["payload"]
    except (OSError, UnicodeError):
        return None
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_safe_sql_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$bytes_b64": base64.b64encode(value).decode("ascii")}
    return value


def restore_sql_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"$bytes_b64"}:
        return base64.b64decode(value["$bytes_b64"])
    return value


def config_sqlite_home(codex_dir: Path) -> Path | None:
    config = codex_dir / "config.toml"
    if not config.is_file():
        return None
    try:
        with config.open("rb") as handle:
            value = tomllib.load(handle).get("sqlite_home")
        return Path(value).expanduser() if isinstance(value, str) and value.strip() else None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"警告：无法读取 {config} 中的 sqlite_home: {exc}", file=sys.stderr)
        return None


def state_db_paths(codex_dir: Path) -> list[Path]:
    paths = [codex_dir / "state_5.sqlite"]
    external = config_sqlite_home(codex_dir)
    if external is None and os.environ.get("CODEX_SQLITE_HOME"):
        external = Path(os.environ["CODEX_SQLITE_HOME"]).expanduser()
    if external is not None:
        candidate = external / "state_5.sqlite"
        if canonical(candidate) not in {canonical(item) for item in paths}:
            paths.append(candidate)
    return paths


def has_threads_provider_column(conn: sqlite3.Connection) -> bool:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'"
    ).fetchone()
    if not table:
        return False
    return any(row[1] == "model_provider" for row in conn.execute("PRAGMA table_info(threads)"))


def count_state_providers(db_path: Path) -> Counter[str]:
    result: Counter[str] = Counter()
    if not db_path.is_file():
        return result
    try:
        with closing(
            sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
        ) as conn:
            if has_threads_provider_column(conn):
                for provider, count in conn.execute(
                    "SELECT model_provider, COUNT(*) FROM threads GROUP BY model_provider"
                ):
                    result[str(provider or "<null>")] += int(count)
    except sqlite3.Error as exc:
        print(f"警告：无法读取 SQLite {db_path}: {exc}", file=sys.stderr)
    return result


def scan(codex_dir: Path) -> dict[str, Any]:
    jsonl_counts: Counter[str] = Counter()
    invalid = 0
    total = 0
    for path in iter_session_files(codex_dir):
        total += 1
        _, provider = read_session_meta(path)
        if provider is None:
            invalid += 1
        else:
            jsonl_counts[str(provider)] += 1
    dbs = []
    for db_path in state_db_paths(codex_dir):
        dbs.append({"path": str(canonical(db_path)), "providers": dict(count_state_providers(db_path))})
    return {
        "codex_dir": str(codex_dir),
        "jsonl_files": total,
        "jsonl_without_provider_meta": invalid,
        "jsonl_providers": dict(jsonl_counts),
        "state_databases": dbs,
    }


def print_scan(report: dict[str, Any]) -> None:
    print(f"Codex 目录：{report['codex_dir']}")
    print(f"JSONL 会话文件：{report['jsonl_files']}（无法识别：{report['jsonl_without_provider_meta']}）")
    print("JSONL provider 分布：")
    for provider, count in sorted(report["jsonl_providers"].items()):
        print(f"  {provider}: {count}")
    if not report["jsonl_providers"]:
        print("  <无>")
    print("SQLite 索引：")
    for db in report["state_databases"]:
        print(f"  {db['path']}")
        if db["providers"]:
            for provider, count in sorted(db["providers"].items()):
                print(f"    {provider}: {count}")
        else:
            print("    <不存在、不可读或无兼容 threads 表>")


def relative_session_path(path: Path, codex_dir: Path) -> Path:
    try:
        return canonical(path).relative_to(canonical(codex_dir))
    except ValueError as exc:
        raise MigrationError(f"会话文件不在 Codex 目录中：{path}") from exc


def unique_generation_dir(backup_root: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return backup_root / f"{stamp}_{uuid.uuid4().hex[:8]}"


def atomic_write_text(path: Path, text: str, mode: int) -> None:
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
        ) as handle:
            temp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, stat.S_IMODE(mode))
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def rewrite_session_provider(
    path: Path,
    source_providers: set[str],
    target_provider: str,
    backup_path: Path | None,
    dry_run: bool,
) -> dict[str, str] | None:
    before = path.stat()
    text = path.read_text(encoding="utf-8")
    pieces = text.splitlines(keepends=True)
    change: dict[str, str] | None = None
    for index, piece in enumerate(pieces):
        line = piece.rstrip("\r\n")
        ending = piece[len(line) :]
        value = parse_session_meta_line(line)
        if value is None:
            continue
        payload = value["payload"]
        current = payload.get("model_provider")
        if current not in source_providers:
            return None
        session_id = str(payload.get("id") or "")
        payload["model_provider"] = target_provider
        pieces[index] = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + ending
        change = {"session_id": session_id, "from": str(current), "to": target_provider}
        break
    if change is None or dry_run:
        return change
    after_read = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after_read.st_mtime_ns, after_read.st_size):
        raise MigrationError(f"迁移期间文件发生变化，已停止：{path}")
    assert backup_path is not None
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_path)
    after_backup = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after_backup.st_mtime_ns, after_backup.st_size):
        raise MigrationError(f"备份期间文件发生变化，未写入：{path}")
    atomic_write_text(path, "".join(pieces), before.st_mode)
    return change


def sqlite_backup(source: sqlite3.Connection, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(target)) as destination:
        source.backup(destination)


def placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def migrate_state_db(
    db_path: Path,
    source_providers: set[str],
    target_provider: str,
    backup_path: Path | None,
    dry_run: bool,
) -> list[dict[str, str]]:
    if not db_path.is_file() or not source_providers:
        return []
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        if not has_threads_provider_column(conn):
            return []
        sources = sorted(source_providers)
        rows = conn.execute(
            f"SELECT id, model_provider FROM threads WHERE model_provider IN ({placeholders(len(sources))})",
            sources,
        ).fetchall()
        changes = [{"thread_id": str(row[0]), "from": str(row[1]), "to": target_provider} for row in rows]
        if not changes or dry_run:
            return changes
        assert backup_path is not None
        sqlite_backup(conn, backup_path)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"UPDATE threads SET model_provider=? WHERE model_provider IN ({placeholders(len(sources))})",
            [target_provider, *sources],
        )
        conn.commit()
        return changes
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def operation_lock(backup_root: Path) -> Iterator[None]:
    backup_root.mkdir(parents=True, exist_ok=True)
    lock_path = backup_root / ".operation.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()} started={now_utc()}\n")
    except FileExistsError as exc:
        raise MigrationError(f"检测到另一个迁移操作或遗留锁：{lock_path}") from exc
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def migrate(
    codex_dir: Path,
    backup_root: Path,
    source_providers: set[str],
    target_provider: str,
    dry_run: bool,
) -> dict[str, Any]:
    if not source_providers or "" in source_providers:
        raise MigrationError("至少需要一个非空源 provider")
    if not target_provider:
        raise MigrationError("目标 provider 不能为空")
    if target_provider in source_providers:
        raise MigrationError("目标 provider 不能同时是源 provider")

    generation = unique_generation_dir(backup_root)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "tool_version": TOOL_VERSION,
        "operation": "migrate",
        "created_at": now_utc(),
        "codex_dir": str(codex_dir),
        "source_providers": sorted(source_providers),
        "target_provider": target_provider,
        "dry_run": dry_run,
        "jsonl_changes": [],
        "state_db_changes": [],
    }
    with operation_lock(backup_root):
        if not dry_run:
            generation.mkdir(parents=True, exist_ok=False)
        try:
            for session_path in iter_session_files(codex_dir):
                relative = relative_session_path(session_path, codex_dir)
                backup_path = generation / "jsonl" / relative
                change = rewrite_session_provider(
                    session_path, source_providers, target_provider, backup_path, dry_run
                )
                if change:
                    manifest["jsonl_changes"].append(
                        {**change, "path": str(relative), "backup": str(Path("jsonl") / relative)}
                    )
            for index, db_path in enumerate(state_db_paths(codex_dir)):
                db_path = canonical(db_path)
                backup_rel = Path("state") / f"{index}_{db_path.name}"
                changes = migrate_state_db(
                    db_path, source_providers, target_provider, generation / backup_rel, dry_run
                )
                if changes:
                    manifest["state_db_changes"].append(
                        {"path": str(db_path), "backup": str(backup_rel), "threads": changes}
                    )
            manifest["completed_at"] = now_utc()
            if not dry_run:
                write_manifest(generation / MANIFEST_NAME, manifest)
                manifest["backup_dir"] = str(generation)
            return manifest
        except Exception:
            if not dry_run and generation.exists():
                manifest["failed_at"] = now_utc()
                manifest["incomplete"] = True
                write_manifest(generation / MANIFEST_NAME, manifest)
            raise


def load_manifest(generation: Path) -> dict[str, Any]:
    manifest_path = generation / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"无法读取迁移清单 {manifest_path}: {exc}") from exc
    if manifest.get("schema_version") != 1 or manifest.get("operation") != "migrate":
        raise MigrationError("不支持的迁移清单格式")
    return manifest


def safe_manifest_session_path(codex_dir: Path, relative: str) -> Path:
    candidate = canonical(codex_dir / relative)
    try:
        candidate.relative_to(canonical(codex_dir))
    except ValueError as exc:
        raise MigrationError(f"迁移清单包含越界路径：{relative}") from exc
    return candidate


def restore_session_from_manifest(
    path: Path, session_id: str, source: str, target: str, dry_run: bool
) -> bool:
    if not path.is_file():
        return False
    before = path.stat()
    text = path.read_text(encoding="utf-8")
    pieces = text.splitlines(keepends=True)
    for index, piece in enumerate(pieces):
        line = piece.rstrip("\r\n")
        ending = piece[len(line) :]
        value = parse_session_meta_line(line)
        if value is None:
            continue
        payload = value["payload"]
        if str(payload.get("id") or "") != session_id or payload.get("model_provider") != target:
            return False
        payload["model_provider"] = source
        pieces[index] = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + ending
        if dry_run:
            return True
        after_read = path.stat()
        if (before.st_mtime_ns, before.st_size) != (after_read.st_mtime_ns, after_read.st_size):
            raise MigrationError(f"恢复期间文件发生变化：{path}")
        atomic_write_text(path, "".join(pieces), before.st_mode)
        return True
    return False


def restore_state_db(entry: dict[str, Any], dry_run: bool) -> int:
    db_path = Path(entry["path"])
    if not db_path.is_file():
        return 0
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for item in entry.get("threads", []):
        groups[(item["from"], item["to"])].append(item["thread_id"])
    changed = 0
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        if not has_threads_provider_column(conn):
            return 0
        if not dry_run:
            conn.execute("BEGIN IMMEDIATE")
        for (source, target), ids in groups.items():
            for start in range(0, len(ids), 400):
                chunk = ids[start : start + 400]
                params: list[str] = [target, *chunk]
                query = (
                    f"SELECT COUNT(*) FROM threads WHERE model_provider=? "
                    f"AND id IN ({placeholders(len(chunk))})"
                )
                count = int(conn.execute(query, params).fetchone()[0])
                changed += count
                if not dry_run and count:
                    conn.execute(
                        f"UPDATE threads SET model_provider=? WHERE model_provider=? "
                        f"AND id IN ({placeholders(len(chunk))})",
                        [source, target, *chunk],
                    )
        if not dry_run:
            conn.commit()
    return changed


def restore(generation: Path, dry_run: bool) -> dict[str, Any]:
    generation = canonical(generation)
    manifest = load_manifest(generation)
    codex_dir = ensure_safe_codex_dir(Path(manifest["codex_dir"]))
    restored_files = 0
    restored_rows = 0
    backup_root = generation.parent
    with operation_lock(backup_root):
        for change in manifest.get("jsonl_changes", []):
            path = safe_manifest_session_path(codex_dir, change["path"])
            if restore_session_from_manifest(
                path, change["session_id"], change["from"], change["to"], dry_run
            ):
                restored_files += 1
        for entry in manifest.get("state_db_changes", []):
            restored_rows += restore_state_db(entry, dry_run)
    return {
        "backup_dir": str(generation),
        "codex_dir": str(codex_dir),
        "dry_run": dry_run,
        "restored_jsonl_files": restored_files,
        "restored_state_rows": restored_rows,
    }


def list_backups(backup_root: Path) -> list[dict[str, Any]]:
    results = []
    if not backup_root.is_dir():
        return results
    for item in sorted(backup_root.iterdir(), reverse=True):
        if not item.is_dir() or not (item / MANIFEST_NAME).is_file():
            continue
        try:
            manifest = load_manifest(item)
            results.append(
                {
                    "path": str(item),
                    "created_at": manifest.get("created_at"),
                    "codex_dir": manifest.get("codex_dir"),
                    "from": manifest.get("source_providers"),
                    "to": manifest.get("target_provider"),
                    "jsonl": len(manifest.get("jsonl_changes", [])),
                    "state_rows": sum(
                        len(entry.get("threads", []))
                        for entry in manifest.get("state_db_changes", [])
                    ),
                    "incomplete": bool(manifest.get("incomplete")),
                }
            )
        except MigrationError:
            continue
    return results


def find_thread_row(codex_dir: Path, session_id: str) -> dict[str, Any] | None:
    for db_path in state_db_paths(codex_dir):
        if not db_path.is_file():
            continue
        try:
            with closing(sqlite3.connect(db_path, timeout=5)) as conn:
                conn.row_factory = sqlite3.Row
                if not has_threads_provider_column(conn):
                    continue
                row = conn.execute("SELECT * FROM threads WHERE id=?", (session_id,)).fetchone()
                if row is not None:
                    return {key: json_safe_sql_value(row[key]) for key in row.keys()}
        except sqlite3.Error as exc:
            print(f"警告：无法从 {db_path} 导出 thread 元数据：{exc}", file=sys.stderr)
    return None


def export_transfer_package(
    codex_dir: Path,
    output_path: Path,
    providers: set[str] | None = None,
) -> dict[str, Any]:
    """Export rollout files plus optional SQLite thread rows into a portable ZIP."""
    codex_dir = ensure_safe_codex_dir(codex_dir)
    output_path = canonical(output_path)
    if output_path.exists():
        raise MigrationError(f"导出文件已存在：{output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sessions: list[dict[str, Any]] = []
    temp_path = output_path.with_name(output_path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(
            temp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
        ) as archive:
            for path in iter_session_files(codex_dir):
                meta = read_full_session_meta(path)
                if not meta:
                    continue
                provider = meta.get("model_provider")
                if providers and provider not in providers:
                    continue
                session_id = str(meta.get("id") or "")
                if not session_id:
                    continue
                relative = relative_session_path(path, codex_dir)
                archive_name = f"rollouts/{len(sessions):08d}/{path.name}"
                digest = sha256_file(path)
                archive.write(path, archive_name)
                sessions.append(
                    {
                        "session_id": session_id,
                        "provider": provider,
                        "cwd": meta.get("cwd"),
                        "original_relative_path": relative.as_posix(),
                        "archived": relative.parts[0] == "archived_sessions",
                        "archive_path": archive_name,
                        "size": path.stat().st_size,
                        "sha256": digest,
                        "thread_row": find_thread_row(codex_dir, session_id),
                    }
                )
            manifest = {
                "format": "codex-session-transfer",
                "format_version": TRANSFER_FORMAT_VERSION,
                "tool_version": TOOL_VERSION,
                "created_at": now_utc(),
                "source_codex_dir": str(codex_dir),
                "providers": sorted(providers) if providers else None,
                "session_count": len(sessions),
                "sessions": sessions,
            }
            archive.writestr(
                TRANSFER_MANIFEST_NAME,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
        os.replace(temp_path, output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return {"output": str(output_path), "sessions": len(sessions), "bytes": output_path.stat().st_size}


def read_transfer_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    try:
        info = archive.getinfo(TRANSFER_MANIFEST_NAME)
        if info.file_size > 100 * 1024 * 1024:
            raise MigrationError("迁移包清单异常过大")
        manifest = json.loads(archive.read(info).decode("utf-8"))
    except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"不是有效的 Codex 会话迁移包：{exc}") from exc
    if (
        manifest.get("format") != "codex-session-transfer"
        or manifest.get("format_version") != TRANSFER_FORMAT_VERSION
        or not isinstance(manifest.get("sessions"), list)
    ):
        raise MigrationError("不支持的会话迁移包格式或版本")
    return manifest


def existing_session_ids(codex_dir: Path) -> set[str]:
    result: set[str] = set()
    for path in iter_session_files(codex_dir):
        session_id, _ = read_session_meta(path)
        if session_id:
            result.add(session_id)
    return result


def portable_destination(codex_dir: Path, entry: dict[str, Any]) -> Path:
    raw = str(entry.get("original_relative_path") or "")
    parts = [part for part in Path(raw.replace("\\", "/")).parts if part not in ("/", "\\")]
    if not parts or parts[0] not in {"sessions", "archived_sessions"}:
        bucket = "archived_sessions" if entry.get("archived") else "sessions"
        parts = [bucket, Path(raw).name or f"rollout-{entry['session_id']}.jsonl"]
    candidate = canonical(codex_dir.joinpath(*parts))
    try:
        candidate.relative_to(canonical(codex_dir))
    except ValueError as exc:
        raise MigrationError(f"迁移包包含越界目标路径：{raw}") from exc
    return candidate


def import_rollout_stream(
    source: Any,
    destination: Path,
    expected_sha256: str,
    provider_override: str | None,
    cwd_override: str | None,
) -> tuple[str, str | None, str | None]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    digest = hashlib.sha256()
    found_id: str | None = None
    found_provider: str | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=destination.parent, delete=False) as raw_out:
            temp_name = raw_out.name
            with io.TextIOWrapper(source, encoding="utf-8", errors="strict", newline="") as text_in:
                for line in text_in:
                    digest.update(line.encode("utf-8"))
                    ending = "\n" if line.endswith("\n") else ""
                    content = line[:-1] if ending else line
                    if content.endswith("\r"):
                        content, ending = content[:-1], "\r\n"
                    value = parse_session_meta_line(content)
                    if value is not None:
                        payload = value["payload"]
                        found_id = str(payload.get("id") or "")
                        if provider_override:
                            payload["model_provider"] = provider_override
                        if cwd_override:
                            payload["cwd"] = cwd_override
                        found_provider = payload.get("model_provider")
                        content = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                    raw_out.write((content + ending).encode("utf-8"))
            raw_out.flush()
            os.fsync(raw_out.fileno())
        if digest.hexdigest() != expected_sha256:
            raise MigrationError(f"会话文件校验失败：{destination.name}")
        os.replace(temp_name, destination)
        temp_name = None
        return found_id or "", found_provider, cwd_override
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def merge_thread_row(
    db_path: Path,
    exported_row: dict[str, Any] | None,
    session_id: str,
    rollout_path: Path,
    provider: str | None,
    cwd_override: str | None,
) -> bool:
    if not db_path.is_file() or not exported_row:
        return False
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        if not has_threads_provider_column(conn):
            return False
        if conn.execute("SELECT 1 FROM threads WHERE id=?", (session_id,)).fetchone():
            return False
        columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        values = {
            key: restore_sql_value(value)
            for key, value in exported_row.items()
            if key in columns
        }
        values["id"] = session_id
        if "rollout_path" in columns:
            values["rollout_path"] = str(rollout_path)
        if "model_provider" in columns and provider is not None:
            values["model_provider"] = provider
        if "cwd" in columns and cwd_override:
            values["cwd"] = cwd_override
        names = list(values)
        if not names:
            return False
        quoted = ",".join(f'"{name}"' for name in names)
        conn.execute(
            f"INSERT OR IGNORE INTO threads ({quoted}) VALUES ({placeholders(len(names))})",
            [values[name] for name in names],
        )
        inserted = conn.execute("SELECT changes()").fetchone()[0] > 0
        conn.commit()
        return inserted


def import_transfer_package(
    codex_dir: Path,
    package_path: Path,
    provider_override: str | None = None,
    cwd_override: str | None = None,
) -> dict[str, Any]:
    codex_dir = ensure_safe_codex_dir(codex_dir)
    package_path = canonical(package_path)
    if not package_path.is_file():
        raise MigrationError(f"迁移包不存在：{package_path}")
    imported = 0
    skipped = 0
    indexed = 0
    index_warnings: list[str] = []
    known_ids = existing_session_ids(codex_dir)
    db_candidates = state_db_paths(codex_dir)
    with zipfile.ZipFile(package_path, "r", allowZip64=True) as archive:
        manifest = read_transfer_manifest(archive)
        names = set(archive.namelist())
        for entry in manifest["sessions"]:
            session_id = str(entry.get("session_id") or "")
            archive_path = str(entry.get("archive_path") or "")
            if not session_id or archive_path not in names:
                raise MigrationError("迁移包清单与文件内容不一致")
            if session_id in known_ids:
                skipped += 1
                continue
            destination = portable_destination(codex_dir, entry)
            if destination.exists():
                skipped += 1
                continue
            with archive.open(archive_path, "r") as source:
                actual_id, provider, _ = import_rollout_stream(
                    source,
                    destination,
                    str(entry.get("sha256") or ""),
                    provider_override,
                    cwd_override,
                )
            if actual_id != session_id:
                destination.unlink(missing_ok=True)
                raise MigrationError(f"迁移包会话 ID 不一致：{session_id} != {actual_id}")
            known_ids.add(session_id)
            imported += 1
            row_indexed = False
            for db_path in db_candidates:
                try:
                    if merge_thread_row(
                        db_path,
                        entry.get("thread_row"),
                        session_id,
                        destination,
                        provider,
                        cwd_override,
                    ):
                        row_indexed = True
                        indexed += 1
                        break
                except sqlite3.Error as exc:
                    index_warnings.append(f"{session_id}: {exc}")
            if not row_indexed and entry.get("thread_row"):
                index_warnings.append(f"{session_id}: 目标 SQLite 架构不兼容，等待 Codex 从 JSONL 重建索引")
    return {
        "package": str(package_path),
        "codex_dir": str(codex_dir),
        "imported": imported,
        "skipped_existing": skipped,
        "indexed": indexed,
        "index_warnings": index_warnings,
    }


def confirm_or_fail(args: argparse.Namespace, message: str) -> None:
    if args.dry_run or args.yes:
        return
    if not sys.stdin.isatty():
        raise MigrationError("非交互环境必须传入 --yes，或先使用 --dry-run")
    answer = input(f"{message}\n输入 MIGRATE 继续：")
    if answer != "MIGRATE":
        raise MigrationError("用户取消")


def add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codex-dir", type=Path, default=default_codex_dir())
    parser.add_argument("--backup-dir", type=Path, default=default_backup_dir())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安全迁移 Codex 本地会话的 model_provider 分桶")
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    scan_parser = sub.add_parser("scan", help="只读扫描会话和索引中的 provider 分布")
    scan_parser.add_argument("--codex-dir", type=Path, default=default_codex_dir())
    scan_parser.add_argument("--json", action="store_true", help="输出 JSON")

    migrate_parser = sub.add_parser("migrate", help="迁移 provider 标签")
    add_common_paths(migrate_parser)
    migrate_parser.add_argument("--from-provider", action="append", required=True)
    migrate_parser.add_argument("--to-provider", required=True)
    migrate_parser.add_argument("--dry-run", action="store_true")
    migrate_parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    migrate_parser.add_argument("--json", action="store_true")

    restore_parser = sub.add_parser("restore", help="根据迁移清单精确恢复")
    restore_parser.add_argument("backup", type=Path, help="包含 manifest.json 的备份代际目录")
    restore_parser.add_argument("--dry-run", action="store_true")
    restore_parser.add_argument("--yes", action="store_true")
    restore_parser.add_argument("--json", action="store_true")

    backups_parser = sub.add_parser("backups", help="列出可恢复的迁移备份")
    backups_parser.add_argument("--backup-dir", type=Path, default=default_backup_dir())
    backups_parser.add_argument("--json", action="store_true")

    export_parser = sub.add_parser("export", help="导出跨电脑会话迁移包")
    export_parser.add_argument("output", type=Path, help="输出 .codexsessions 文件")
    export_parser.add_argument("--codex-dir", type=Path, default=default_codex_dir())
    export_parser.add_argument("--provider", action="append", help="仅导出指定 provider，可重复")
    export_parser.add_argument("--json", action="store_true")

    import_parser = sub.add_parser("import", help="从另一台电脑的迁移包导入会话")
    import_parser.add_argument("package", type=Path)
    import_parser.add_argument("--codex-dir", type=Path, default=default_codex_dir())
    import_parser.add_argument("--provider", help="导入时统一改成此 provider")
    import_parser.add_argument("--cwd", help="导入时统一改成此项目目录")
    import_parser.add_argument("--yes", action="store_true")
    import_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            report = scan(ensure_safe_codex_dir(args.codex_dir))
            if args.json:
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print_scan(report)
            return 0

        if args.command == "migrate":
            codex_dir = ensure_safe_codex_dir(args.codex_dir)
            backup_root = canonical(args.backup_dir)
            sources = set(args.from_provider)
            confirm_or_fail(
                args,
                f"将把 {codex_dir} 中 provider {sorted(sources)} 迁移为 {args.to_provider}。请先退出 Codex。",
            )
            result = migrate(codex_dir, backup_root, sources, args.to_provider, args.dry_run)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"{'预演' if args.dry_run else '迁移'}完成：")
                print(f"  JSONL 文件：{len(result['jsonl_changes'])}")
                print(
                    "  SQLite 行："
                    + str(sum(len(entry['threads']) for entry in result['state_db_changes']))
                )
                if result.get("backup_dir"):
                    print(f"  备份与清单：{result['backup_dir']}")
            return 0

        if args.command == "restore":
            confirm_or_fail(args, f"将按 {args.backup} 的清单精确恢复 provider 标签。请先退出 Codex。")
            result = restore(args.backup, args.dry_run)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"{'预演' if args.dry_run else '恢复'}完成：")
                print(f"  JSONL 文件：{result['restored_jsonl_files']}")
                print(f"  SQLite 行：{result['restored_state_rows']}")
            return 0

        if args.command == "backups":
            items = list_backups(canonical(args.backup_dir))
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=2))
            elif not items:
                print("没有找到可恢复备份。")
            else:
                for item in items:
                    print(
                        f"{item['path']}\n"
                        f"  {item['created_at']}  {item['from']} -> {item['to']}  "
                        f"JSONL={item['jsonl']} SQLite={item['state_rows']}"
                        + ("  [未完整完成，可按清单恢复已改部分]" if item['incomplete'] else "")
                    )
            return 0

        if args.command == "export":
            result = export_transfer_package(
                ensure_safe_codex_dir(args.codex_dir),
                args.output,
                set(args.provider) if args.provider else None,
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"已导出 {result['sessions']} 个会话：{result['output']}")
            return 0

        if args.command == "import":
            args.dry_run = False
            confirm_or_fail(args, f"将把 {args.package} 合并导入本机 Codex。请先退出 Codex。")
            result = import_transfer_package(
                ensure_safe_codex_dir(args.codex_dir), args.package, args.provider, args.cwd
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(
                    f"导入完成：新增 {result['imported']}，已存在跳过 {result['skipped_existing']}，"
                    f"立即写入索引 {result['indexed']}"
                )
                for warning in result["index_warnings"]:
                    print(f"警告：{warning}", file=sys.stderr)
            return 0
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
