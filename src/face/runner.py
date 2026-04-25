import argparse
import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

import dotenv
from bs4 import BeautifulSoup
from notifier import send_notification
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_CHROME_PROFILE = Path(__file__).resolve().parent / "profile"
OUTPUT_ROOT_DIR = Path(__file__).resolve().parents[2] / "output"
OUTPUT_SUBDIR_NAME = "face"

dotenv.load_dotenv(ENV_FILE)


def get_chrome_profile_path():
    profile_path = os.getenv(
        "FACE_CHROME_PROFILE_DIRECTORY",
        os.getenv("CHROME_PROFILE_DIRECTORY", str(DEFAULT_CHROME_PROFILE)),
    )
    profile_path = Path(profile_path)

    if not profile_path.is_absolute():
        profile_path = ENV_FILE.parent / profile_path

    profile_path.mkdir(parents=True, exist_ok=True)
    return profile_path


def get_runtime_profile_path():
    return get_chrome_profile_path()


def create_driver(headless=True, profile_path=None):
    options = Options()
    chrome_profile_path = Path(profile_path) if profile_path else get_chrome_profile_path()
    chrome_profile_path.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={chrome_profile_path}")
    options.add_argument("--window-size=1920,1080")

    if headless:
        options.add_argument("--headless=new")

    return webdriver.Chrome(options=options)


def resolve_writable_output_path(path):
    if not path.exists():
        return path

    try:
        with path.open("a", encoding="utf-8"):
            return path
    except PermissionError:
        return path.with_name(f"{path.stem}_updated{path.suffix}")


def get_execution_output_dir(output_dir=None):
    execution_root_dir = Path(output_dir) if output_dir else OUTPUT_ROOT_DIR / datetime.now().strftime("%d-%m-%y--%H-%M")

    if execution_root_dir.name.lower() == OUTPUT_SUBDIR_NAME:
        return execution_root_dir

    return execution_root_dir / OUTPUT_SUBDIR_NAME


def save_results(records, output_dir=None):
    execution_output_dir = get_execution_output_dir(output_dir)
    execution_output_dir.mkdir(parents=True, exist_ok=True)
    json_output_path = resolve_writable_output_path(execution_output_dir / "facebook.json")
    csv_output_path = resolve_writable_output_path(execution_output_dir / "facebook.csv")

    json_output_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with csv_output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=["fonte", "status", "data"],
            delimiter=";",
        )
        writer.writeheader()
        writer.writerows(records)

    return json_output_path, csv_output_path


def build_google_query(search_term, after=None, before=None):
    query_parts = ["site:facebook.com"]

    if search_term:
        query_parts.append(f'"{search_term}"')

    if after:
        query_parts.append(f"after:{after}")

    if before:
        query_parts.append(f"before:{before}")

    return " ".join(query_parts)


def normalize_google_result_url(href):
    if not href:
        return None

    target_url = None

    if href.startswith("/url?"):
        parsed_href = urlparse(href)
        target_url = parse_qs(parsed_href.query).get("q", [None])[0]
    elif href.startswith(("http://", "https://")):
        target_url = href

    if not target_url:
        return None

    parsed_target = urlparse(target_url)
    host = parsed_target.netloc.lower()

    if "google." in host or "facebook.com" not in host:
        return None

    return parsed_target._replace(fragment="").geturl()


def extract_facebook_results(page_source):
    soup = BeautifulSoup(page_source, "html.parser")
    records = []
    seen_links = set()

    for anchor in soup.select("a[href]"):
        link = normalize_google_result_url(anchor.get("href", ""))

        if not link or link in seen_links:
            continue

        seen_links.add(link)
        records.append(
            {
                "fonte": link,
                "status": 0,
                "data": 0,
            }
        )

    return records


def is_facebook_logged_in(driver):
    return driver.get_cookie("c_user") is not None or driver.get_cookie("xs") is not None


def wait_for_login_completion(driver, platform_name, login_check, poll_interval=5):
    print(f"Aguardando login no {platform_name}...")

    while not login_check(driver):
        time.sleep(poll_interval)

    print(f"Login no {platform_name} concluido.")


def is_google_captcha_page(driver):
    current_url = driver.current_url.lower()
    parsed_url = urlparse(current_url)
    current_host = parsed_url.netloc.lower()
    current_path = parsed_url.path.lower()
    page_source = driver.page_source.lower()
    page_title = (driver.title or "").lower()

    if "/sorry/" in current_path or "/sorry/" in current_url:
        return True

    if "google." not in current_host:
        return False

    captcha_markers = [
        "our systems have detected unusual traffic",
        "detected unusual traffic from your computer network",
        "unusual traffic from your computer network",
        "this page appears when google automatically detects requests",
        "not a robot",
        'id="captcha-form"',
        'action="/sorry/index"',
        'name="captcha"',
        "g-recaptcha",
        "recaptcha/api.js",
    ]
    title_markers = [
        "unusual traffic",
        "not a robot",
        "sorry",
    ]

    return any(marker in page_source for marker in captcha_markers) or any(
        marker in page_title for marker in title_markers
    )


def reopen_driver(driver, url, headless, profile_path):
    try:
        driver.quit()
    except Exception:
        pass

    new_driver = create_driver(headless=headless, profile_path=profile_path)
    new_driver.get(url)
    return new_driver


def wait_for_captcha_resolution(driver, headless_mode, profile_path):
    captcha_message_shown = False

    while is_google_captcha_page(driver):
        if headless_mode:
            send_notification(
                "Captcha do Google",
                "Resolva o captcha do Google no navegador para a busca continuar.",
            )
            print("Captcha detectado em headless. Abrindo navegador visivel para voce resolver.")
            driver = reopen_driver(driver, driver.current_url, headless=False, profile_path=profile_path)
            headless_mode = False
            captcha_message_shown = False
            continue

        if not captcha_message_shown:
            print("Captcha detectado no Google. Resolva no navegador para o script continuar.")
            captcha_message_shown = True

        time.sleep(5)

    return driver, headless_mode


def restore_headless_mode(driver, headless_mode, profile_path, timeout=15):
    if headless_mode:
        return driver, headless_mode

    print("Captcha resolvido. Retornando ao modo headless.")
    driver = reopen_driver(driver, driver.current_url, headless=True, profile_path=profile_path)
    headless_mode = True

    try:
        WebDriverWait(driver, timeout).until(
            lambda current_driver: extract_facebook_results(current_driver.page_source)
            or current_driver.find_elements(By.CSS_SELECTOR, "#search")
            or current_driver.find_elements(By.CSS_SELECTOR, "a#pnnext")
            or is_google_captcha_page(current_driver)
        )
    except TimeoutException:
        pass

    return driver, headless_mode


def wait_for_results_page(driver, headless_mode, profile_path, timeout=15):
    driver, headless_mode = wait_for_captcha_resolution(driver, headless_mode, profile_path)

    try:
        WebDriverWait(driver, timeout).until(
            lambda current_driver: extract_facebook_results(current_driver.page_source)
            or current_driver.find_elements(By.CSS_SELECTOR, "#search")
            or current_driver.find_elements(By.CSS_SELECTOR, "a#pnnext")
            or is_google_captcha_page(current_driver)
        )
    except TimeoutException:
        pass

    driver, headless_mode = wait_for_captcha_resolution(driver, headless_mode, profile_path)
    driver, headless_mode = restore_headless_mode(driver, headless_mode, profile_path, timeout=timeout)
    return driver, headless_mode


def get_next_page_url(driver):
    next_selectors = [
        "a#pnnext",
        "a[aria-label='Next page']",
    ]

    for selector in next_selectors:
        next_links = driver.find_elements(By.CSS_SELECTOR, selector)
        for next_link in next_links:
            href = next_link.get_attribute("href")
            if href:
                return href

    current_url = urlparse(driver.current_url)
    current_query = parse_qs(current_url.query)
    current_start = int(current_query.get("start", ["0"])[0])

    for next_link in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
        href = next_link.get_attribute("href")
        if not href or "google." not in urlparse(href).netloc.lower():
            continue

        parsed_href = urlparse(href)
        if "/search" not in parsed_href.path:
            continue

        next_query = parse_qs(parsed_href.query)
        next_start = next_query.get("start", [None])[0]
        if next_start is None:
            continue

        try:
            if int(next_start) > current_start:
                return href
        except ValueError:
            continue

    return None


def FaceMain(search_term="", after=None, before=None, output_dir=None):
    google_query = build_google_query(search_term, after=after, before=before)
    url = f"https://www.google.com/search?q={quote_plus(google_query)}"
    collected_records = []
    seen_links = set()
    visited_pages = set()
    headless_mode = True
    runtime_profile_path = get_runtime_profile_path()
    driver = create_driver(headless=headless_mode, profile_path=runtime_profile_path)

    try:
        driver.get(url)

        while True:
            driver, headless_mode = wait_for_results_page(driver, headless_mode, runtime_profile_path)

            current_page_url = driver.current_url
            if current_page_url in visited_pages:
                break

            visited_pages.add(current_page_url)

            for record in extract_facebook_results(driver.page_source):
                if record["fonte"] in seen_links:
                    continue

                seen_links.add(record["fonte"])
                collected_records.append(record)

            save_results(collected_records, output_dir=output_dir)

            next_page_url = get_next_page_url(driver)
            if not next_page_url or next_page_url in visited_pages:
                break

            driver.get(next_page_url)
    finally:
        saved_json_path, saved_csv_path = save_results(collected_records, output_dir=output_dir)
        try:
            driver.quit()
        except Exception:
            pass

    print(json.dumps(collected_records, ensure_ascii=False, indent=2))
    print(f"Arquivos salvos em: {saved_json_path} e {saved_csv_path}")
    return collected_records


def FaceLogin():
    driver = create_driver(headless=False, profile_path=get_chrome_profile_path())

    try:
        driver.get("https://www.facebook.com/login")
        wait_for_login_completion(driver, "Facebook", is_facebook_logged_in)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("termo", nargs="?", default="")
    parser.add_argument("--after")
    parser.add_argument("--before")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    FaceMain(args.termo, after=args.after, before=args.before)
