import argparse
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

FILES = [
    "data/watchlist.json",
    "data/token_scanner_latest.json",
    "data/token_scanner_latest_profiles_raw.json",
    "data/social_inference_latest.json",
    "data/social_alerts.json",
]

PATTERNS = [
    "data/token_scanner_*.jsonl",
    "data/social_inference_*.jsonl",
    "data/social_inference_error_*.json",
    "data/social_inference_usage_*.json",
    "data/social_alerts_*.jsonl",
    "logs/token_scanner_*.txt",
    "logs/social_inference_*.txt",
    "logs/krptov_runner_*.txt",
]

DIRECTORIES = [
    "data/social_posts",
]

RECREATE_DIRS = [
    "data",
    "logs",
    "data/social_posts",
]


def now_stamp():
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def resolve_root(value):
    root = Path(value).expanduser() if value else PROJECT_ROOT
    return root.resolve()


def relative(root, path):
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def is_inside(root, path):
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def validate_root(root):
    required = [
        root / "src",
        root / "config",
        root / "app.py",
    ]
    missing = [path for path in required if not path.exists()]

    if missing:
        missing_text = ", ".join(str(relative(root, path)) for path in missing)
        raise SystemExit(f"Root nao parece ser o projeto KRPTO-V. Ausente: {missing_text}")


def add_path(root, items, path):
    if not path.exists() and not path.is_symlink():
        return

    resolved = path.resolve()

    if not is_inside(root, resolved):
        print(f"Aviso: ignorando caminho fora do projeto: {path}")
        return

    items[resolved] = resolved


def collect_targets(root, include_cloud, keep_watchlist):
    files = {}
    dirs = {}

    for item in FILES:
        if keep_watchlist and item == "data/watchlist.json":
            continue
        add_path(root, files, root / item)

    for pattern in PATTERNS:
        for path in root.glob(pattern):
            if path.is_file() or path.is_symlink():
                add_path(root, files, path)

    for item in DIRECTORIES:
        path = root / item
        if not path.exists():
            continue

        if path.is_file() or path.is_symlink():
            add_path(root, files, path)
        else:
            for child in path.iterdir():
                add_path(root, dirs if child.is_dir() and not child.is_symlink() else files, child)

    if include_cloud:
        cloud_dir = root / "logs" / "cloud"
        if cloud_dir.exists():
            if cloud_dir.is_file() or cloud_dir.is_symlink():
                add_path(root, files, cloud_dir)
            else:
                for child in cloud_dir.iterdir():
                    add_path(root, dirs if child.is_dir() and not child.is_symlink() else files, child)

    return sorted(files.values()), sorted(dirs.values())


def estimate_size(paths):
    total = 0

    for path in paths:
        if path.is_symlink():
            continue

        if path.is_file():
            total += path.stat().st_size
            continue

        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size

    return total


def format_bytes(value):
    units = ["B", "KB", "MB", "GB"]
    size = float(value)

    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} GB"


def print_targets(root, files, dirs, mode):
    print("=== KRPTO-V | Reset Runtime Data ===")
    print(f"Root: {root}")
    print(f"Modo: {mode}")
    print()

    print("Arquivos que seriam apagados:" if mode == "dry-run" else "Arquivos apagados/selecionados:")
    if files:
        for path in files:
            print(f"- {relative(root, path)}")
    else:
        print("- nenhum")

    print()
    print("Diretorios que seriam apagados:" if mode == "dry-run" else "Diretorios apagados/selecionados:")
    if dirs:
        for path in dirs:
            print(f"- {relative(root, path)}")
    else:
        print("- nenhum")

    print()
    print(f"Total de arquivos: {len(files)}")
    print(f"Total de diretorios: {len(dirs)}")
    print(f"Total estimado: {format_bytes(estimate_size(files + dirs))}")
    print()


def backup_targets(root, files, dirs):
    backup_dir = root / "backups" / f"runtime_reset_{now_stamp()}"
    backup_zip = backup_dir / "runtime_data.zip"
    backup_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(backup_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if path.exists() and path.is_file() and not path.is_symlink():
                archive.write(path, relative(root, path))

        for directory in dirs:
            if not directory.exists() or directory.is_symlink():
                continue

            for child in directory.rglob("*"):
                if child.is_file() and not child.is_symlink():
                    archive.write(child, relative(root, child))

    return backup_zip


def remove_file(path):
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)


def remove_directory(path):
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return

    if path.is_dir():
        shutil.rmtree(path)


def delete_targets(files, dirs):
    for path in files:
        remove_file(path)

    for path in sorted(dirs, key=lambda item: len(item.parts), reverse=True):
        remove_directory(path)


def recreate_runtime_files(root, keep_watchlist):
    for directory in RECREATE_DIRS:
        (root / directory).mkdir(parents=True, exist_ok=True)

    if not keep_watchlist:
        watchlist_file = root / "data" / "watchlist.json"
        watchlist_file.write_text(json.dumps({}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    alerts_file = root / "data" / "social_alerts.json"
    alerts_file.write_text(json.dumps([], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Limpa dados runtime do KRPTO-V para iniciar uma nova rodada.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Mostra o que seria apagado. Padrao.")
    parser.add_argument("--confirm", action="store_true", help="Apaga de verdade. Necessario para executar.")
    parser.add_argument("--backup", action="store_true", help="Cria backup zip antes de apagar.")
    parser.add_argument("--include-cloud", action="store_true", help="Tambem limpa logs/cloud.")
    parser.add_argument("--keep-watchlist", action="store_true", help="Nao apaga data/watchlist.json.")
    parser.add_argument("--root", help="Root do projeto. Default: dois niveis acima deste script.")
    return parser.parse_args()


def main():
    args = parse_args()
    root = resolve_root(args.root)
    validate_root(root)

    dry_run = not args.confirm
    mode = "dry-run" if dry_run else "confirm"
    files, dirs = collect_targets(root, args.include_cloud, args.keep_watchlist)

    print_targets(root, files, dirs, mode)

    if dry_run:
        print("Nada foi apagado. Rode com --confirm para executar.")
        print("Comando real recomendado:")
        print("python src/tools/reset_runtime_data.py --backup --confirm")
        return

    backup_file = None
    if args.backup:
        backup_file = backup_targets(root, files, dirs)
        print(f"Backup criado em: {relative(root, backup_file)}")

    delete_targets(files, dirs)
    recreate_runtime_files(root, args.keep_watchlist)

    print()
    print("Reset concluido.")
    print("Diretorios recriados:")
    for directory in RECREATE_DIRS:
        print(f"- {directory}")

    if not args.keep_watchlist:
        print("Arquivo inicial criado: data/watchlist.json ({})")
    print("Arquivo inicial criado: data/social_alerts.json ([])")

    if backup_file:
        print(f"Backup: {relative(root, backup_file)}")


if __name__ == "__main__":
    main()
