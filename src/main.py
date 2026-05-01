import argparse
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from insta.runner import InstaLogin, InstaMain, InstaReview
from face.runner import FaceLogin, FaceMain
from notifier import send_notification

APP_NAME = "ScrapyInfoPolitica"


def get_default_output_root_dir():
    if getattr(sys, "frozen", False):
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return local_app_data / APP_NAME / "output"

    return Path(__file__).resolve().parents[1] / "output"


OUTPUT_ROOT_DIR = get_default_output_root_dir()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("termo", nargs="?", default="")
    parser.add_argument("--after")
    parser.add_argument("--before")
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--review")
    parser.add_argument("--face", action="store_true")
    parser.add_argument("--insta", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers deve ser maior ou igual a 1.")

    if args.login and args.review:
        parser.error("--login e --review nao podem ser usados juntos.")

    if args.review and (args.termo or args.after or args.before):
        parser.error("--review usa os arquivos ja gerados e nao aceita termo/after/before.")

    if args.review and (args.face or args.insta):
        parser.error("--review nao aceita --face/--insta.")

    return args


def get_selected_sources(args, default_to_all=False):
    selected_sources = []

    if args.insta:
        selected_sources.append("insta")

    if args.face:
        selected_sources.append("face")

    if default_to_all and not selected_sources:
        return ["insta", "face"]

    return selected_sources


def should_close_chrome(args, selected_sources):
    return args.login or args.review or bool(selected_sources)


def close_chrome_instances():
    for process_name in ("chrome.exe", "chromedriver.exe"):
        subprocess.run(
            ["taskkill", "/F", "/T", "/IM", process_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def run_login_sources(selected_sources):
    close_chrome_instances()
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = []

        if "insta" in selected_sources:
            futures.append(executor.submit(InstaLogin))

        if "face" in selected_sources:
            futures.append(executor.submit(FaceLogin))

        for future in futures:
            future.result()


def run_review(review_path):
    close_chrome_instances()
    InstaReview(review_path)


def run_search(search_term, selected_sources, after=None, before=None, workers=1, output_dir=None):
    execution_output_dir = None

    if not selected_sources:
        return execution_output_dir

    execution_output_dir = Path(output_dir) if output_dir else OUTPUT_ROOT_DIR / datetime.now().strftime("%d-%m-%y--%H-%M")
    execution_output_dir.mkdir(parents=True, exist_ok=True)

    close_chrome_instances()

    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = []

        if "insta" in selected_sources:
            futures.append(
                executor.submit(
                    InstaMain,
                    search_term,
                    after=after,
                    before=before,
                    output_dir=execution_output_dir,
                )
            )

        if "face" in selected_sources:
            futures.append(
                executor.submit(
                    FaceMain,
                    search_term,
                    after=after,
                    before=before,
                    output_dir=execution_output_dir,
                    workers=workers,
                )
            )

        for future in futures:
            future.result()

    return execution_output_dir


def run_cli():
    args = parse_args()
    selected_sources = get_selected_sources(args)

    try:
        if args.login:
            selected_login_sources = get_selected_sources(args, default_to_all=True)
            run_login_sources(selected_login_sources)
        elif args.review:
            run_review(args.review)
            selected_login_sources = []
            execution_output_dir = None
        else:
            execution_output_dir = run_search(
                args.termo,
                selected_sources,
                after=args.after,
                before=args.before,
                workers=args.workers,
            )
            selected_login_sources = []
    except Exception as exc:
        send_notification("Execucao com erro", str(exc))
        raise

    if args.login:
        selected_login_sources = get_selected_sources(args, default_to_all=True)
        send_notification(
            "Logins concluidos",
            f"{', '.join(selected_login_sources)} autenticado(s) e salvo(s) nos perfis.",
        )
    elif args.review:
        send_notification(
            "Review concluida",
            f"Instagram reprocessado a partir de {args.review}",
        )
    elif not selected_sources:
        send_notification(
            "Nenhuma fonte selecionada",
            "Use --face e/ou --insta para executar a busca.",
        )
    else:
        send_notification(
            "Execucao concluida",
            f"Arquivos salvos em {execution_output_dir}",
        )


def launch_gui():
    root = tk.Tk()
    root.title("Scrapy InfoPolitica")
    root.geometry("620x470")
    root.minsize(560, 430)

    search_var = tk.StringVar()
    after_var = tk.StringVar()
    before_var = tk.StringVar()
    workers_var = tk.IntVar(value=1)
    face_var = tk.BooleanVar(value=True)
    insta_var = tk.BooleanVar(value=False)
    output_var = tk.StringVar()
    review_var = tk.StringVar()
    status_var = tk.StringVar(value="Pronto.")

    running_buttons = []

    def selected_sources():
        sources = []
        if insta_var.get():
            sources.append("insta")
        if face_var.get():
            sources.append("face")
        return sources

    def set_status(message):
        root.after(0, lambda: status_var.set(message))

    def set_running(is_running):
        state = tk.DISABLED if is_running else tk.NORMAL
        root.after(0, lambda: [button.configure(state=state) for button in running_buttons])

    def run_background(label, target):
        def worker():
            set_running(True)
            set_status(f"{label} em andamento...")
            try:
                result = target()
            except Exception as exc:
                set_status(f"Erro: {exc}")
                root.after(0, lambda: messagebox.showerror("Erro", str(exc)))
            else:
                set_status(result or f"{label} concluido.")
                root.after(0, lambda: messagebox.showinfo("Concluido", result or f"{label} concluido."))
            finally:
                set_running(False)

        threading.Thread(target=worker, daemon=True).start()

    def browse_output():
        selected_path = filedialog.askdirectory(title="Escolha a pasta de output")
        if selected_path:
            output_var.set(selected_path)

    def browse_review():
        selected_path = filedialog.askdirectory(title="Escolha a pasta de output para review")
        if selected_path:
            review_var.set(selected_path)

    def start_search():
        sources = selected_sources()
        if not sources:
            messagebox.showwarning("Fontes", "Marque Instagram e/ou Facebook para executar.")
            return

        workers = workers_var.get()
        if workers < 1:
            messagebox.showwarning("Workers", "Workers deve ser maior ou igual a 1.")
            return

        output_dir = output_var.get().strip() or None

        def task():
            execution_output_dir = run_search(
                search_var.get().strip(),
                sources,
                after=after_var.get().strip() or None,
                before=before_var.get().strip() or None,
                workers=workers,
                output_dir=output_dir,
            )
            send_notification("Execucao concluida", f"Arquivos salvos em {execution_output_dir}")
            return f"Busca concluida. Arquivos salvos em {execution_output_dir}"

        run_background("Busca", task)

    def start_login():
        sources = selected_sources()
        if not sources:
            messagebox.showwarning("Fontes", "Marque Instagram e/ou Facebook para abrir login.")
            return

        def task():
            run_login_sources(sources)
            send_notification("Logins concluidos", f"{', '.join(sources)} autenticado(s).")
            return f"Login concluido para: {', '.join(sources)}"

        run_background("Login", task)

    def start_review():
        review_path = review_var.get().strip()
        if not review_path:
            messagebox.showwarning("Review", "Selecione a pasta de output para review.")
            return

        def task():
            run_review(review_path)
            send_notification("Review concluida", f"Instagram reprocessado a partir de {review_path}")
            return f"Review concluido a partir de {review_path}"

        run_background("Review", task)

    content = ttk.Frame(root, padding=16)
    content.pack(fill=tk.BOTH, expand=True)
    content.columnconfigure(1, weight=1)

    ttk.Label(content, text="String de busca").grid(row=0, column=0, sticky=tk.W, pady=(0, 8))
    ttk.Entry(content, textvariable=search_var).grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=(0, 8))

    ttk.Label(content, text="Fontes").grid(row=1, column=0, sticky=tk.W, pady=8)
    source_frame = ttk.Frame(content)
    source_frame.grid(row=1, column=1, columnspan=2, sticky=tk.W, pady=8)
    ttk.Checkbutton(source_frame, text="Facebook", variable=face_var).pack(side=tk.LEFT, padx=(0, 16))
    ttk.Checkbutton(source_frame, text="Instagram", variable=insta_var).pack(side=tk.LEFT)

    ttk.Label(content, text="After").grid(row=2, column=0, sticky=tk.W, pady=8)
    ttk.Entry(content, textvariable=after_var, width=18).grid(row=2, column=1, sticky=tk.W, pady=8)
    ttk.Label(content, text="Before").grid(row=2, column=1, sticky=tk.E, padx=(0, 160), pady=8)
    ttk.Entry(content, textvariable=before_var, width=18).grid(row=2, column=2, sticky=tk.EW, pady=8)

    ttk.Label(content, text="Workers").grid(row=3, column=0, sticky=tk.W, pady=8)
    ttk.Spinbox(content, from_=1, to=50, textvariable=workers_var, width=8).grid(row=3, column=1, sticky=tk.W, pady=8)

    ttk.Label(content, text="Pasta de output").grid(row=4, column=0, sticky=tk.W, pady=8)
    ttk.Entry(content, textvariable=output_var).grid(row=4, column=1, sticky=tk.EW, pady=8)
    ttk.Button(content, text="Escolher", command=browse_output).grid(row=4, column=2, sticky=tk.E, padx=(8, 0), pady=8)

    ttk.Label(content, text="Output para review").grid(row=5, column=0, sticky=tk.W, pady=8)
    ttk.Entry(content, textvariable=review_var).grid(row=5, column=1, sticky=tk.EW, pady=8)
    ttk.Button(content, text="Escolher", command=browse_review).grid(row=5, column=2, sticky=tk.E, padx=(8, 0), pady=8)

    actions = ttk.Frame(content)
    actions.grid(row=6, column=0, columnspan=3, sticky=tk.EW, pady=(20, 8))
    actions.columnconfigure((0, 1, 2), weight=1)

    run_button = ttk.Button(actions, text="Executar busca", command=start_search)
    login_button = ttk.Button(actions, text="Abrir login", command=start_login)
    review_button = ttk.Button(actions, text="Executar review", command=start_review)
    run_button.grid(row=0, column=0, sticky=tk.EW, padx=(0, 8))
    login_button.grid(row=0, column=1, sticky=tk.EW, padx=8)
    review_button.grid(row=0, column=2, sticky=tk.EW, padx=(8, 0))
    running_buttons.extend([run_button, login_button, review_button])

    ttk.Separator(content).grid(row=7, column=0, columnspan=3, sticky=tk.EW, pady=(18, 10))
    ttk.Label(content, textvariable=status_var).grid(row=8, column=0, columnspan=3, sticky=tk.W)

    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        launch_gui()
    else:
        run_cli()
