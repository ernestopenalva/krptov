import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_FILE = PROJECT_ROOT / "logs" / f"token_scanner_{datetime.now():%Y-%m-%d}.txt"

COST_PER_POST_USD = 0.005
POST_SCENARIOS = (5, 20, 50, 100)

BLOCK_MARKER = "=== KRPTO-V | Token Scanner ==="
INT_FIELDS = {
    "tokens_returned": "Tokens retornados",
    "ethereum_found": "Ethereum encontrados",
    "new_added": "Novos adicionados",
    "updated": "Atualizados",
    "ignored_discard": "Ignorados por descarte",
    "ignored_external_status": "Ignorados por status externo",
    "structural_discards": "Descartados estruturalmente",
    "physical_removals": "Removidos fisicamente",
    "watchlist_total": "Total da WL",
}


def parse_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_datetime(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.strip().replace("Z", ""))
    except ValueError:
        return None


def parse_chains(value):
    chains = Counter()

    if not value:
        return chains

    for part in value.split(","):
        if "=" not in part:
            continue

        chain, count = part.split("=", 1)
        chain = chain.strip().lower()

        if chain:
            chains[chain] += parse_int(count)

    return chains


def split_blocks(text):
    blocks = []
    current = []

    for line in text.splitlines():
        if line.strip() == BLOCK_MARKER:
            if current:
                blocks.append(current)
            current = [line]
            continue

        if current:
            current.append(line)

    if current:
        blocks.append(current)

    return blocks


def parse_block(lines):
    cycle = {
        "cycle_at": None,
        "chains": Counter(),
    }

    for key in INT_FIELDS:
        cycle[key] = 0

    for raw_line in lines:
        line = raw_line.strip()

        if not line or ":" not in line:
            continue

        label, value = line.split(":", 1)
        label = label.strip()
        value = value.strip()

        if label == "Ciclo":
            cycle["cycle_at"] = parse_datetime(value)
            continue

        if label == "Chains encontradas":
            cycle["chains"] = parse_chains(value)
            continue

        for key, field_label in INT_FIELDS.items():
            if label == field_label:
                cycle[key] = parse_int(value)
                break

    return cycle


def load_cycles(log_file):
    text = log_file.read_text(encoding="utf-8", errors="replace")
    return [parse_block(block) for block in split_blocks(text)]


def format_number(value, decimals=2):
    if isinstance(value, int):
        return f"{value:,}".replace(",", ".")

    return f"{value:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_percent(value):
    return f"{value:.1f}%".replace(".", ",")


def format_money(value):
    return f"US$ {value:,.4f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_dt(value):
    if not value:
        return "indisponivel"

    return value.isoformat(timespec="seconds")


def format_duration(seconds):
    if seconds is None:
        return "indisponivel"

    seconds = int(max(0, seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}min")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def intervals_between(timestamps):
    clean = [value for value in timestamps if value]
    return [
        (clean[index] - clean[index - 1]).total_seconds()
        for index in range(1, len(clean))
    ]


def longest_zero_streak(cycles, key):
    longest = 0
    current = 0

    for cycle in cycles:
        if cycle.get(key, 0) <= 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0

    return longest


def first_time_delta(cycles, predicate, start_at):
    if not start_at:
        return None

    for cycle in cycles:
        cycle_at = cycle.get("cycle_at")
        if cycle_at and predicate(cycle):
            return (cycle_at - start_at).total_seconds()

    return None


def classify_ethereum_volume(new_ethereum, days_analyzed):
    if days_analyzed <= 0:
        return "indisponivel"

    per_day = new_ethereum / days_analyzed

    if per_day == 0:
        return "baixo"
    if per_day < 5:
        return "baixo"
    if per_day < 25:
        return "moderado"
    return "alto"


def build_comments(stats):
    comments = []
    chains_total = stats["chains_total"]
    chain_counts = stats["chain_counts"]
    cycles_count = stats["cycles_count"]
    ethereum_cycle_percent = stats["ethereum_cycle_percent"]
    longest_without_eth = stats["longest_without_eth"]

    if chains_total > 0:
        top_chain, top_count = chain_counts.most_common(1)[0]
        top_percent = top_count / chains_total * 100

        if top_chain == "solana" and top_percent >= 50:
            comments.append(f"Solana domina o feed analisado ({format_percent(top_percent)} dos tokens retornados por chain).")
        else:
            comments.append(f"A chain mais frequente foi {top_chain} ({format_percent(top_percent)}).")

    if cycles_count > 0:
        if ethereum_cycle_percent == 0:
            comments.append("Ethereum nao apareceu em nenhum ciclo do periodo analisado.")
        elif ethereum_cycle_percent < 20:
            comments.append("Ethereum apareceu pouco dentro dos ciclos observados.")
        elif ethereum_cycle_percent < 60:
            comments.append("Ethereum apareceu com frequencia moderada nos ciclos observados.")
        else:
            comments.append("Ethereum apareceu com frequencia alta nos ciclos observados.")

    if longest_without_eth >= 10:
        comments.append(f"Houve uma sequencia longa sem Ethereum: {longest_without_eth} ciclos seguidos.")

    if stats["initial_wl"] == 0 and stats["final_wl"] == 0:
        comments.append("A watchlist ficou vazia durante todo o periodo analisado.")
    elif stats["time_to_leave_zero"] is not None:
        comments.append(f"A watchlist saiu de zero apos {format_duration(stats['time_to_leave_zero'])}.")

    if stats["structural_discards"] > 0:
        comments.append("Descartes estruturais ocorreram; isso indica tokens novos sumindo do feed antes de virarem observacao persistente.")

    comments.append(f"O volume de novos Ethereum parece {stats['ethereum_volume_label']} para um MVP social/X.")

    return comments


def calculate_stats(cycles):
    dated_cycles = [cycle for cycle in cycles if cycle.get("cycle_at")]
    first_at = dated_cycles[0]["cycle_at"] if dated_cycles else None
    last_at = dated_cycles[-1]["cycle_at"] if dated_cycles else None
    duration_seconds = (last_at - first_at).total_seconds() if first_at and last_at else None
    interval_values = intervals_between([cycle.get("cycle_at") for cycle in dated_cycles])

    cycles_count = len(cycles)
    total_tokens_returned = sum(cycle["tokens_returned"] for cycle in cycles)
    chain_counts = Counter()

    for cycle in cycles:
        chain_counts.update(cycle["chains"])

    total_ethereum_found = sum(cycle["ethereum_found"] for cycle in cycles)
    cycles_with_ethereum = sum(1 for cycle in cycles if cycle["ethereum_found"] > 0)
    cycles_without_ethereum = cycles_count - cycles_with_ethereum
    ethereum_cycle_percent = cycles_with_ethereum / cycles_count * 100 if cycles_count else 0
    new_ethereum = sum(cycle["new_added"] for cycle in cycles)
    new_event_times = [cycle["cycle_at"] for cycle in cycles if cycle["new_added"] > 0 and cycle.get("cycle_at")]
    new_event_intervals = intervals_between(new_event_times)

    wl_values = [cycle["watchlist_total"] for cycle in cycles if "watchlist_total" in cycle]
    initial_wl = wl_values[0] if wl_values else 0
    final_wl = wl_values[-1] if wl_values else 0
    max_wl = max(wl_values) if wl_values else 0
    min_wl = min(wl_values) if wl_values else 0

    days_analyzed = duration_seconds / 86400 if duration_seconds and duration_seconds > 0 else 0
    days_for_cost = days_analyzed if days_analyzed > 0 else 1

    stats = {
        "cycles_count": cycles_count,
        "first_at": first_at,
        "last_at": last_at,
        "duration_seconds": duration_seconds,
        "avg_interval": sum(interval_values) / len(interval_values) if interval_values else None,
        "max_interval": max(interval_values) if interval_values else None,
        "total_tokens_returned": total_tokens_returned,
        "avg_tokens_returned": total_tokens_returned / cycles_count if cycles_count else 0,
        "chain_counts": chain_counts,
        "chains_total": sum(chain_counts.values()),
        "total_ethereum_found": total_ethereum_found,
        "cycles_with_ethereum": cycles_with_ethereum,
        "cycles_without_ethereum": cycles_without_ethereum,
        "ethereum_cycle_percent": ethereum_cycle_percent,
        "longest_without_eth": longest_zero_streak(cycles, "ethereum_found"),
        "new_ethereum": new_ethereum,
        "new_ethereum_per_cycle": new_ethereum / cycles_count if cycles_count else 0,
        "new_ethereum_per_day": new_ethereum / days_analyzed if days_analyzed > 0 else 0,
        "new_event_intervals": new_event_intervals,
        "initial_wl": initial_wl,
        "final_wl": final_wl,
        "max_wl": max_wl,
        "min_wl": min_wl,
        "time_to_leave_zero": first_time_delta(cycles, lambda cycle: cycle["watchlist_total"] > 0, first_at) if initial_wl == 0 else 0,
        "time_to_max_wl": first_time_delta(cycles, lambda cycle: cycle["watchlist_total"] == max_wl, first_at),
        "ignored_discard": sum(cycle["ignored_discard"] for cycle in cycles),
        "ignored_external_status": sum(cycle["ignored_external_status"] for cycle in cycles),
        "structural_discards": sum(cycle["structural_discards"] for cycle in cycles),
        "physical_removals": sum(cycle["physical_removals"] for cycle in cycles),
        "days_analyzed": days_analyzed,
        "days_for_cost": days_for_cost,
    }
    stats["ethereum_volume_label"] = classify_ethereum_volume(new_ethereum, days_analyzed)
    stats["comments"] = build_comments(stats)
    return stats


def print_section(title):
    print()
    print(title)
    print("-" * len(title))


def print_report(log_file, cycles, stats):
    print("KRPTO-V | Analise do log do Token Scanner")
    print(f"Arquivo: {log_file}")

    print_section("1. Periodo analisado")
    print(f"Primeiro ciclo: {format_dt(stats['first_at'])}")
    print(f"Ultimo ciclo: {format_dt(stats['last_at'])}")
    print(f"Duracao total: {format_duration(stats['duration_seconds'])}")
    print(f"Quantidade de ciclos: {format_number(stats['cycles_count'])}")
    print(f"Intervalo medio entre ciclos: {format_duration(stats['avg_interval'])}")
    print(f"Maior intervalo entre ciclos: {format_duration(stats['max_interval'])}")

    print_section("2. Tokens retornados")
    print(f"Total retornado: {format_number(stats['total_tokens_returned'])}")
    print(f"Media por ciclo: {format_number(stats['avg_tokens_returned'])}")

    print_section("3. Chains encontradas")
    if not stats["chain_counts"]:
        print("Nenhuma chain encontrada no log.")
    else:
        for index, (chain, count) in enumerate(stats["chain_counts"].most_common(), start=1):
            percent = count / stats["chains_total"] * 100 if stats["chains_total"] else 0
            print(f"{index}. {chain}: {format_number(count)} ({format_percent(percent)})")

    print_section("4. Ethereum")
    print(f"Total de aparicoes Ethereum: {format_number(stats['total_ethereum_found'])}")
    print(f"Ciclos com Ethereum > 0: {format_number(stats['cycles_with_ethereum'])}")
    print(f"Ciclos sem Ethereum: {format_number(stats['cycles_without_ethereum'])}")
    print(f"Percentual de ciclos com Ethereum: {format_percent(stats['ethereum_cycle_percent'])}")
    print(f"Maior sequencia sem Ethereum: {format_number(stats['longest_without_eth'])} ciclos")
    print(f"Novos Ethereum adicionados: {format_number(stats['new_ethereum'])}")
    print(f"Frequencia media de novos Ethereum: {format_number(stats['new_ethereum_per_cycle'], 4)} por ciclo")
    print(f"Frequencia media de novos Ethereum: {format_number(stats['new_ethereum_per_day'], 4)} por dia")

    if stats["new_event_intervals"]:
        print(f"Menor intervalo entre eventos de novos Ethereum: {format_duration(min(stats['new_event_intervals']))}")
        print(f"Maior intervalo entre eventos de novos Ethereum: {format_duration(max(stats['new_event_intervals']))}")
    else:
        print("Intervalo entre eventos de novos Ethereum: indisponivel")

    print_section("5. Watchlist")
    print(f"Total da WL inicial: {format_number(stats['initial_wl'])}")
    print(f"Total da WL final: {format_number(stats['final_wl'])}")
    print(f"Maior tamanho observado: {format_number(stats['max_wl'])}")
    print(f"Menor tamanho observado: {format_number(stats['min_wl'])}")
    print(f"Tempo ate sair de WL zero: {format_duration(stats['time_to_leave_zero'])}")
    print(f"Tempo ate atingir maior tamanho observado: {format_duration(stats['time_to_max_wl'])}")

    print_section("6. Descartes e remocoes")
    print(f"Ignorados por descarte: {format_number(stats['ignored_discard'])}")
    print(f"Ignorados por status externo: {format_number(stats['ignored_external_status'])}")
    print(f"Descartados estruturalmente: {format_number(stats['structural_discards'])}")
    print(f"Removidos fisicamente: {format_number(stats['physical_removals'])}")

    print_section("7. Estimativa financeira preliminar para X")
    print(f"Custo por post: {format_money(COST_PER_POST_USD)}")
    print(f"Tokens relevantes: {format_number(stats['new_ethereum'])}")
    print("Janela social por token: 24h")
    print("Premissa: posts ja consultados nao sao recobrados dentro da janela.")
    print(f"Dias analisados usados na estimativa: {format_number(stats['days_for_cost'], 6)}")

    for posts_per_token in POST_SCENARIOS:
        period_cost = stats["new_ethereum"] * posts_per_token * COST_PER_POST_USD
        daily_cost = period_cost / stats["days_for_cost"] if stats["days_for_cost"] else 0
        monthly_cost = daily_cost * 30
        print(
            f"{posts_per_token} posts/token: "
            f"periodo {format_money(period_cost)} | "
            f"dia {format_money(daily_cost)} | "
            f"30 dias {format_money(monthly_cost)}"
        )

    print_section("8. Padroes observados")
    for comment in stats["comments"]:
        print(f"- {comment}")

    if not cycles:
        print("- O arquivo foi lido, mas nenhum bloco do scanner foi reconhecido.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analisa o log textual do token scanner do KRPTO-V.",
    )
    parser.add_argument(
        "log_file",
        nargs="?",
        default=str(DEFAULT_LOG_FILE),
        help="Arquivo de log. Padrao: logs/token_scanner_YYYY-MM-DD.txt",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    log_file = Path(args.log_file)

    if not log_file.is_absolute():
        log_file = PROJECT_ROOT / log_file

    if not log_file.exists():
        raise SystemExit(f"Arquivo de log nao encontrado: {log_file}")

    cycles = load_cycles(log_file)
    stats = calculate_stats(cycles)
    print_report(log_file, cycles, stats)


if __name__ == "__main__":
    main()
