from src.modules.token_scanner_solana import run_token_scanner_solana
from src.modules.market_ranker import run_cycle as run_market_ranker
from src.modules.social_inference import run_social_inference


def main():
    run_token_scanner_solana()
    run_market_ranker()
    run_social_inference()


if __name__ == "__main__":
    main()
