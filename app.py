from src.modules.token_scanner import run_token_scanner
from src.modules.social_inference import run_social_inference


def main():
    run_token_scanner()
    run_social_inference()


if __name__ == "__main__":
    main()
