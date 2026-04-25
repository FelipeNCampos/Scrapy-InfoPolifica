import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from insta.runner import InstaLogin, InstaMain, InstaReview
from face.runner import FaceLogin, FaceMain
from notifier import send_notification

OUTPUT_ROOT_DIR = Path(__file__).resolve().parents[1] / "output"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("termo", nargs="?", default="")
    parser.add_argument("--after")
    parser.add_argument("--before")
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--review")
    args = parser.parse_args()

    if args.login and args.review:
        parser.error("--login e --review nao podem ser usados juntos.")

    if args.review and (args.termo or args.after or args.before):
        parser.error("--review usa os arquivos ja gerados e nao aceita termo/after/before.")

    return args


if __name__ == "__main__":
    args = parse_args()
    execution_output_dir = None

    with ThreadPoolExecutor(max_workers=20) as executor:
        if args.login:
            futures = [
                executor.submit(InstaLogin),
                executor.submit(FaceLogin),
            ]
        elif args.review:
            futures = [
                executor.submit(
                    InstaReview,
                    args.review,
                ),
            ]
        else:
            execution_output_dir = OUTPUT_ROOT_DIR / datetime.now().strftime("%d-%m-%y--%H-%M")
            execution_output_dir.mkdir(parents=True, exist_ok=True)
            futures = [
                executor.submit(
                    InstaMain,
                    args.termo,
                    after=args.after,
                    before=args.before,
                    output_dir=execution_output_dir,
                ),
                executor.submit(
                    FaceMain,
                    args.termo,
                    after=args.after,
                    before=args.before,
                    output_dir=execution_output_dir,
                ),
            ]

        try:
            for future in futures:
                future.result()
        except Exception as exc:
            send_notification("Execucao com erro", str(exc))
            raise

    if args.login:
        send_notification(
            "Logins concluidos",
            "Instagram e Facebook foram autenticados e salvos nos perfis.",
        )
    elif args.review:
        send_notification(
            "Review concluida",
            f"Instagram reprocessado a partir de {args.review}",
        )
    else:
        send_notification(
            "Execucao concluida",
            f"Arquivos salvos em {execution_output_dir}",
        )
