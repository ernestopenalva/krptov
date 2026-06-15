import argparse
import gzip
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

PROTECTED_NAMES = {
    ".env",
    "social_alerts.json",
    "watchlist.json",
    "watchlist.lock",
}


@dataclass(frozen=True)
class CleanupPolicy:
    name: str
    directory: Path
    pattern: str
    action: str
    older_than_days: int


@dataclass
class CleanupAction:
    policy: str
    action: str
    path: Path
    size_bytes: int
    age_days: float
    target_path: Path | None = None
    status: str = "pending"
    error: str | None = None


def utc_now():
    return datetime.now(timezone.utc)


def parse_iso_datetime(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_bytes(value):
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.2f} {unit}"
        amount /= 1024
    return f"{amount:.2f} TB"


def is_relative_to(path, parent):
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def is_protected_path(path, project_root):
    resolved = path.resolve()
    if not is_relative_to(resolved, project_root):
        return True
    if resolved.name in PROTECTED_NAMES:
        return True
    if resolved.suffix == ".lock":
        return True
    return False


def file_age_days(path, current_time):
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return max(0.0, (current_time - modified_at).total_seconds() / 86400)


def discover_actions(policies, current_time, project_root=PROJECT_ROOT):
    actions = []
    for policy in policies:
        directory = policy.directory
        if not directory.exists():
            continue
        for path in sorted(directory.glob(policy.pattern)):
            if not path.is_file():
                continue
            if is_protected_path(path, project_root):
                continue

            age_days = file_age_days(path, current_time)
            if age_days < policy.older_than_days:
                continue

            target_path = None
            if policy.action == "compress":
                if path.suffix == ".gz":
                    continue
                target_path = path.with_name(f"{path.name}.gz")
                if target_path.exists():
                    actions.append(
                        CleanupAction(
                            policy=policy.name,
                            action="skip_existing_gzip",
                            path=path,
                            target_path=target_path,
                            size_bytes=path.stat().st_size,
                            age_days=age_days,
                            status="skipped",
                            error="arquivo .gz ja existe",
                        )
                    )
                    continue

            actions.append(
                CleanupAction(
                    policy=policy.name,
                    action=policy.action,
                    path=path,
                    target_path=target_path,
                    size_bytes=path.stat().st_size,
                    age_days=age_days,
                )
            )
    return actions


def compress_file(path, target_path):
    temp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    try:
        with path.open("rb") as source, gzip.open(temp_path, "wb", compresslevel=9) as target:
            shutil.copyfileobj(source, target)
        os.replace(temp_path, target_path)
        path.unlink()
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def apply_actions(actions):
    for action in actions:
        if action.status == "skipped":
            continue
        try:
            if action.action == "compress":
                compress_file(action.path, action.target_path)
                action.status = "done"
            elif action.action == "delete":
                action.path.unlink()
                action.status = "done"
            else:
                action.status = "skipped"
                action.error = f"acao desconhecida: {action.action}"
        except Exception as error:
            action.status = "error"
            action.error = str(error)
    return actions


def build_policies(args, project_root=PROJECT_ROOT):
    data_dir = project_root / "data"
    logs_dir = project_root / "logs"
    return [
        CleanupPolicy(
            name="market_ranker_snapshots_jsonl",
            directory=data_dir / "market_ranker",
            pattern="snapshots_*.jsonl",
            action="compress",
            older_than_days=args.market_ranker_jsonl_days,
        ),
        CleanupPolicy(
            name="market_ranker_snapshots_gz",
            directory=data_dir / "market_ranker",
            pattern="snapshots_*.jsonl.gz",
            action="delete",
            older_than_days=args.market_ranker_gz_days,
        ),
        CleanupPolicy(
            name="pool_scanner_events_jsonl",
            directory=data_dir / "pool_scanner",
            pattern="events_*.jsonl",
            action="compress",
            older_than_days=args.pool_scanner_jsonl_days,
        ),
        CleanupPolicy(
            name="pool_scanner_events_gz",
            directory=data_dir / "pool_scanner",
            pattern="events_*.jsonl.gz",
            action="delete",
            older_than_days=args.pool_scanner_gz_days,
        ),
        CleanupPolicy(
            name="social_inference_jsonl",
            directory=data_dir,
            pattern="social_inference_*.jsonl",
            action="compress",
            older_than_days=args.social_jsonl_days,
        ),
        CleanupPolicy(
            name="social_inference_gz",
            directory=data_dir,
            pattern="social_inference_*.jsonl.gz",
            action="delete",
            older_than_days=args.social_gz_days,
        ),
        CleanupPolicy(
            name="logs",
            directory=logs_dir,
            pattern="*.log",
            action="delete",
            older_than_days=args.logs_days,
        ),
        CleanupPolicy(
            name="legacy_logs",
            directory=logs_dir,
            pattern="*.txt",
            action="delete",
            older_than_days=args.logs_days,
        ),
    ]


def summarize(actions):
    selected = [action for action in actions if action.status != "skipped"]
    by_action = {}
    for action in selected:
        by_action.setdefault(action.action, {"files": 0, "bytes": 0})
        by_action[action.action]["files"] += 1
        by_action[action.action]["bytes"] += action.size_bytes
    return {
        "files": len(selected),
        "bytes": sum(action.size_bytes for action in selected),
        "by_action": by_action,
        "errors": sum(1 for action in actions if action.status == "error"),
        "skipped": sum(1 for action in actions if action.status == "skipped"),
    }


def print_report(actions, dry_run):
    summary = summarize(actions)
    mode = "dry-run" if dry_run else "apply"
    print("=== KRPTO-V | Runtime Cleanup ===")
    print(f"Modo: {mode}")
    print(f"Arquivos candidatos: {summary['files']}")
    print(f"Espaco selecionado: {format_bytes(summary['bytes'])}")
    if summary["skipped"]:
        print(f"Ignorados: {summary['skipped']}")
    if summary["errors"]:
        print(f"Erros: {summary['errors']}")

    for action_name, item in sorted(summary["by_action"].items()):
        print(
            f"- {action_name}: {item['files']} arquivo(s), "
            f"{format_bytes(item['bytes'])}"
        )

    if not actions:
        print("Nenhuma acao necessaria.")
        return

    print("Detalhes:")
    for action in actions:
        target = f" -> {action.target_path}" if action.target_path else ""
        status = f" | {action.status}" if not dry_run or action.status == "skipped" else ""
        error = f" | {action.error}" if action.error else ""
        print(
            f"- {action.action} | {format_bytes(action.size_bytes)} | "
            f"{action.age_days:.1f}d | {action.path}{target}{status}{error}"
        )


def parse_args():
    parser = argparse.ArgumentParser(
        description="Limpa arquivos de runtime do KRPTO-V com dry-run por padrao.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Executa as acoes. Sem esta flag, apenas mostra o que seria feito.",
    )
    parser.add_argument(
        "--now",
        help="Horario UTC para calculo de idade, em ISO. Uso principal: testes.",
    )
    parser.add_argument("--market-ranker-jsonl-days", type=int, default=2)
    parser.add_argument("--market-ranker-gz-days", type=int, default=30)
    parser.add_argument("--pool-scanner-jsonl-days", type=int, default=7)
    parser.add_argument("--pool-scanner-gz-days", type=int, default=30)
    parser.add_argument("--social-jsonl-days", type=int, default=14)
    parser.add_argument("--social-gz-days", type=int, default=45)
    parser.add_argument("--logs-days", type=int, default=30)
    return parser.parse_args()


def run(args=None, project_root=PROJECT_ROOT):
    args = args or parse_args()
    current_time = parse_iso_datetime(args.now) if args.now else utc_now()
    policies = build_policies(args, project_root=project_root)
    actions = discover_actions(policies, current_time, project_root=project_root)
    if args.apply:
        apply_actions(actions)
    print_report(actions, dry_run=not args.apply)
    return actions


def main():
    run()


if __name__ == "__main__":
    main()
