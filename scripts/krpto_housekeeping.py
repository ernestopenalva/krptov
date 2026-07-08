#!/usr/bin/env python3
"""Housekeeping seguro para os projetos KRPTO-V e KRPTO3."""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path("/root")
KRPTO_V_ROOT = ROOT / "krptov"
KRPTO3_ROOT = ROOT / "krpto3"
TOP_FILE_LIMIT = 20


@dataclass(frozen=True)
class Action:
    kind: str
    path: Path
    size: int
    destination: Path | None = None


@dataclass
class Summary:
    compressed_count: int = 0
    compressed_original_size: int = 0
    compressed_gz_size: int = 0
    deleted_count: int = 0
    deleted_size: int = 0
    skipped_count: int = 0


def human_size(size: int | float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def iter_files_safe(root: Path, skip_history: bool = False) -> Iterable[Path]:
    if not root.exists():
        return
    if root.is_symlink() or not root.is_dir():
        return

    for current_root, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        kept_dirs: list[str] = []
        for name in dir_names:
            child = current / name
            if child.is_symlink():
                continue
            if skip_history and name == "history":
                continue
            kept_dirs.append(name)
        dir_names[:] = kept_dirs

        for name in file_names:
            path = current / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
            except OSError:
                continue
            yield path


def dir_size(path: Path) -> int:
    total = 0
    for file_path in iter_files_safe(path):
        try:
            total += file_path.stat().st_size
        except OSError:
            continue
    return total


def is_older_than_days(path: Path, days: int, now: float | None = None) -> bool:
    if now is None:
        now = time.time()
    try:
        modified_at = path.stat().st_mtime
    except OSError:
        return False
    return (now - modified_at) > days * 24 * 60 * 60


def compress_file(path: Path) -> tuple[bool, int, str]:
    destination = path.with_suffix(path.suffix + ".gz")
    if destination.exists():
        return False, 0, f"SKIP compactacao: destino ja existe: {destination}"
    if path.is_symlink() or not path.is_file():
        return False, 0, f"SKIP compactacao: nao e arquivo regular: {path}"

    try:
        original_size = path.stat().st_size
        with path.open("rb") as source, gzip.open(destination, "wb", compresslevel=9) as target:
            shutil.copyfileobj(source, target)

        gz_size = destination.stat().st_size
        if gz_size <= 0:
            try:
                destination.unlink()
            except OSError:
                pass
            return False, 0, f"SKIP compactacao: .gz ficou vazio: {destination}"

        path.unlink()
        return True, gz_size, (
            f"OK compactado: {path} -> {destination} "
            f"({human_size(original_size)} -> {human_size(gz_size)})"
        )
    except OSError as exc:
        try:
            if destination.exists() and destination.stat().st_size <= 0:
                destination.unlink()
        except OSError:
            pass
        return False, 0, f"ERRO compactacao: {path}: {exc}"


def delete_file(path: Path) -> tuple[bool, str]:
    if path.is_symlink() or not path.is_file():
        return False, f"SKIP remocao: nao e arquivo regular: {path}"
    try:
        path.unlink()
        return True, f"OK removido: {path}"
    except OSError as exc:
        return False, f"ERRO remocao: {path}: {exc}"


def collect_report() -> tuple[list[tuple[str, Path, int | None]], list[tuple[int, Path]]]:
    directories = [
        ("krptov total", KRPTO_V_ROOT),
        ("krptov/data", KRPTO_V_ROOT / "data"),
        ("krptov/data/market_ranker", KRPTO_V_ROOT / "data" / "market_ranker"),
        ("krptov/logs", KRPTO_V_ROOT / "logs"),
        ("krptov/backups", KRPTO_V_ROOT / "backups"),
        ("krpto3 total", KRPTO3_ROOT),
        ("krpto3/data", KRPTO3_ROOT / "data"),
        ("krpto3/data/position_monitor", KRPTO3_ROOT / "data" / "position_monitor"),
        ("krpto3/data/position_monitor_abb", KRPTO3_ROOT / "data" / "position_monitor_abb"),
        ("krpto3/data/market_data", KRPTO3_ROOT / "data" / "market_data"),
        ("krpto3/logs", KRPTO3_ROOT / "logs"),
        ("krpto3/reports", KRPTO3_ROOT / "reports"),
    ]

    usage: list[tuple[str, Path, int | None]] = []
    for label, path in directories:
        usage.append((label, path, dir_size(path) if path.exists() else None))

    largest: list[tuple[int, Path]] = []
    for project_root, skip_history in ((KRPTO_V_ROOT, False), (KRPTO3_ROOT, True)):
        for file_path in iter_files_safe(project_root, skip_history=skip_history):
            try:
                largest.append((file_path.stat().st_size, file_path))
            except OSError:
                continue
    largest.sort(key=lambda item: item[0], reverse=True)
    return usage, largest[:TOP_FILE_LIMIT]


def _matching_files(directory: Path, pattern: str, days: int, skip_history: bool = False) -> Iterable[Path]:
    now = time.time()
    if not directory.exists() or directory.is_symlink() or not directory.is_dir():
        return
    for file_path in iter_files_safe(directory, skip_history=skip_history):
        if file_path.match(pattern) and is_older_than_days(file_path, days, now=now):
            yield file_path


def collect_actions() -> tuple[list[Action], list[Action]]:
    compressions: list[Action] = []
    deletions: list[Action] = []

    krptov_market_ranker = KRPTO_V_ROOT / "data" / "market_ranker"
    for file_path in _matching_files(krptov_market_ranker, "snapshots_*.jsonl", 7):
        compressions.append(Action("compress", file_path, _file_size(file_path), file_path.with_suffix(".jsonl.gz")))
    for file_path in _matching_files(krptov_market_ranker, "snapshots_*.jsonl.gz", 60):
        deletions.append(Action("delete", file_path, _file_size(file_path)))

    krptov_logs = KRPTO_V_ROOT / "logs"
    for pattern in ("*.log", "*.txt"):
        for file_path in _matching_files(krptov_logs, pattern, 30):
            deletions.append(Action("delete", file_path, _file_size(file_path)))

    krpto3_dirs = [
        KRPTO3_ROOT / "data" / "position_monitor",
        KRPTO3_ROOT / "data" / "position_monitor_abb",
        KRPTO3_ROOT / "data" / "market_data",
    ]
    protected_files = {
        KRPTO3_ROOT / "data" / "position_monitor" / "closed_trades.json",
    }
    for directory in krpto3_dirs:
        for file_path in _matching_files(directory, "*.jsonl", 7, skip_history=True):
            if file_path in protected_files:
                continue
            compressions.append(Action("compress", file_path, _file_size(file_path), file_path.with_suffix(".jsonl.gz")))
        for file_path in _matching_files(directory, "*.jsonl.gz", 60, skip_history=True):
            if file_path in protected_files:
                continue
            deletions.append(Action("delete", file_path, _file_size(file_path)))

    krpto3_logs = KRPTO3_ROOT / "logs"
    for pattern in ("*.log", "*.txt"):
        for file_path in _matching_files(krpto3_logs, pattern, 30, skip_history=True):
            deletions.append(Action("delete", file_path, _file_size(file_path)))

    return compressions, deletions


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def print_report() -> None:
    usage, largest = collect_report()
    print("Uso por diretorio")
    print("=================")
    for label, path, size in usage:
        if size is None:
            print(f"{label:36} MISSING   {path}")
        else:
            print(f"{label:36} {human_size(size):>10}   {path}")

    print()
    print(f"{TOP_FILE_LIMIT} maiores arquivos")
    print("===================")
    if not largest:
        print("Nenhum arquivo encontrado.")
    for size, path in largest:
        print(f"{human_size(size):>10}   {path}")


def print_actions(compressions: list[Action], deletions: list[Action]) -> None:
    print("Compactacoes planejadas")
    print("=======================")
    if not compressions:
        print("Nenhuma compactacao planejada.")
    for action in compressions:
        destination = action.destination or action.path.with_suffix(action.path.suffix + ".gz")
        if destination.exists():
            note = " destino ja existe; sera pulado"
        else:
            note = ""
        print(f"{human_size(action.size):>10}   {action.path} -> {destination}{note}")

    print()
    print("Remocoes planejadas")
    print("===================")
    if not deletions:
        print("Nenhuma remocao planejada.")
    for action in deletions:
        print(f"{human_size(action.size):>10}   {action.path}")


def dry_run() -> None:
    compressions, deletions = collect_actions()
    print_actions(compressions, deletions)
    summary = Summary(
        compressed_count=len(compressions),
        compressed_original_size=sum(action.size for action in compressions),
        deleted_count=len(deletions),
        deleted_size=sum(action.size for action in deletions),
    )
    print_summary(summary, dry_run=True)


def apply_actions() -> None:
    compressions, deletions = collect_actions()
    print_actions(compressions, deletions)
    print()
    print("Executando")
    print("==========")

    summary = Summary()
    for action in compressions:
        ok, gz_size, message = compress_file(action.path)
        print(message)
        if ok:
            summary.compressed_count += 1
            summary.compressed_original_size += action.size
            summary.compressed_gz_size += gz_size
        else:
            summary.skipped_count += 1

    for action in deletions:
        ok, message = delete_file(action.path)
        print(message)
        if ok:
            summary.deleted_count += 1
            summary.deleted_size += action.size
        else:
            summary.skipped_count += 1

    print_summary(summary, dry_run=False)


def print_summary(summary: Summary, dry_run: bool) -> None:
    print()
    print("Resumo")
    print("======")
    suffix = " (dry-run)" if dry_run else ""
    print(f"Arquivos a compactar/compactados{suffix}: {summary.compressed_count}")
    print(f"Espaco original compactado{suffix}: {human_size(summary.compressed_original_size)}")
    if dry_run:
        print("Tamanho final dos .gz: n/a em dry-run")
    else:
        print(f"Tamanho final dos .gz: {human_size(summary.compressed_gz_size)}")
    print(f"Arquivos a apagar/apagados{suffix}: {summary.deleted_count}")
    print(f"Espaco liberado por remocao{suffix}: {human_size(summary.deleted_size)}")
    if summary.skipped_count:
        print(f"Arquivos pulados/erro: {summary.skipped_count}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Housekeeping seguro para /root/krptov e /root/krpto3.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", action="store_true", help="Mostra uso de disco e maiores arquivos.")
    mode.add_argument("--dry-run", action="store_true", help="Mostra o que seria compactado/removido.")
    mode.add_argument("--apply", action="store_true", help="Executa compactacao e remocao seguras.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.report:
        print_report()
    elif args.dry_run:
        dry_run()
    elif args.apply:
        apply_actions()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
