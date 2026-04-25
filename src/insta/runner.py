import json
import os
import tempfile
import re
import threading
import time
import unicodedata
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import parse_qs, quote_plus, urlparse
from xml.sax.saxutils import escape

import dotenv
from bs4 import BeautifulSoup
from notifier import send_notification
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchWindowException,
    SessionNotCreatedException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
DEFAULT_CHROME_PROFILE = Path(__file__).resolve().parent / "profile"
OUTPUT_ROOT_DIR = Path(__file__).resolve().parents[2] / "output"
OUTPUT_SUBDIR_NAME = "insta"
RUNTIME_PROFILES_DIR = OUTPUT_ROOT_DIR / "_runtime_profiles"

dotenv.load_dotenv(ENV_FILE)

INSTAGRAM_CATEGORY_ORDER = [
    "perfil",
    "post",
    "reel",
    "nao_identificado",
]
INSTAGRAM_DEFAULT_HEADERS = ["fonte", "status", "error", "data"]
INSTAGRAM_POST_HEADERS = [
    *INSTAGRAM_DEFAULT_HEADERS,
    "perfil_publicador",
    "verificado",
    "descricao_post",
    "numero_likes",
    "numero_comentarios",
    "numero_reposts",
]
INSTAGRAM_REEL_HEADERS = [
    *INSTAGRAM_DEFAULT_HEADERS,
    "perfil_publicador",
    "verificado",
    "descricao_reel",
    "numero_likes",
    "numero_comentarios",
    "numero_reposts",
]
INSTAGRAM_POST_DATA_DEFAULTS = {
    "perfil_publicador": "",
    "verificado": "",
    "descricao_post": "",
    "numero_likes": "",
    "numero_comentarios": "",
    "numero_reposts": "",
    "data": "",
}
INSTAGRAM_REEL_DATA_DEFAULTS = {
    "perfil_publicador": "",
    "verificado": "",
    "descricao_reel": "",
    "numero_likes": "",
    "numero_comentarios": "",
    "numero_reposts": "",
    "data": "",
}
INSTAGRAM_METRIC_EXTRACTION_RULES = {
    "numero_likes": {
        "keywords": ("like", "likes", "curtida", "curtidas", "curtido por", "curtiram"),
        "extra_patterns": [
            r"(?:curtido por|liked by).*?(?:outras?|others?|and)\s+(?P<count>\d+(?:[.,]\d+)?)\s*(?P<suffix>mil|mi|bi|k|m|b)?\b",
        ],
    },
    "numero_comentarios": {
        "keywords": ("comment", "comments", "comentario", "comentarios"),
        "extra_patterns": [],
    },
    "numero_reposts": {
        "keywords": ("share", "shares", "repost", "reposts", "compartilhar", "compartilhamentos"),
        "extra_patterns": [],
    },
}
INSTAGRAM_PROFILE_RESERVED_PATHS = {
    "about",
    "accounts",
    "api",
    "challenge",
    "developer",
    "developers",
    "direct",
    "download",
    "emails",
    "explore",
    "graphql",
    "help",
    "legal",
    "oauth",
    "p",
    "press",
    "privacy",
    "reel",
    "reels",
    "session",
    "share",
    "stories",
    "terms",
    "tv",
    "web",
}
INSTAGRAM_LOGIN_POPUP_CLOSE_XPATHS = [
    "/html/body/div[1]/div/div/div[2]/div/div/div[1]/div[1]/div[2]/section/main/div[1]/div[2]/div/div/div/div/div[1]/div",
    "//div[@role='dialog']//*[self::button or @role='button'][@aria-label='Fechar' or @aria-label='Close']",
    "//div[@role='dialog']//*[self::button or @role='button'][.//*[local-name()='svg' and (@aria-label='Fechar' or @aria-label='Close')]]",
    "//div[@role='dialog']//*[self::button or @role='button'][.//*[local-name()='title' and (normalize-space()='Fechar' or normalize-space()='Close')]]",
    "//div[@role='dialog']//*[self::button or @role='button'][normalize-space()='Agora nao' or normalize-space()='Agora não' or normalize-space()='Not now']",
]
INSTAGRAM_LOGIN_POPUP_TEXT_MARKERS = [
    "continue as",
    "entre para ver o conteudo",
    "entre para ver o conteúdo",
    "entrar para curtir ou comentar",
    "nao perca nenhum post",
    "não perca nenhum post",
    "cadastre-se no instagram",
    "log in to instagram",
    "see instagram photos and videos",
    "sign up to see photos and videos from your friends",
    "veja fotos e videos do instagram",
    "veja fotos e vídeos do instagram",
]


def log_instagram(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[instagram {timestamp}] {message}")


def format_progress(current, total):
    if total <= 0:
        return str(current)

    percentage = int((current / total) * 100)
    return f"{current}/{total} ({percentage}%)"


def summarize_record_counts(records):
    counts = {category: 0 for category in INSTAGRAM_CATEGORY_ORDER}

    for record in records:
        counts[record.get("categoria", "nao_identificado")] = (
            counts.get(record.get("categoria", "nao_identificado"), 0) + 1
        )

    ordered_categories = INSTAGRAM_CATEGORY_ORDER + [
        category for category in counts if category not in INSTAGRAM_CATEGORY_ORDER
    ]
    return ", ".join(
        f"{category}={counts[category]}"
        for category in ordered_categories
        if counts.get(category, 0) > 0
    ) or "sem_resultados"


def get_chrome_profile_path():
    profile_path = os.getenv(
        "INSTAGRAM_CHROME_PROFILE_DIRECTORY",
        str(DEFAULT_CHROME_PROFILE),
    )
    profile_path = Path(profile_path)

    if not profile_path.is_absolute():
        profile_path = ENV_FILE.parent / profile_path

    profile_path.mkdir(parents=True, exist_ok=True)
    return profile_path


def get_runtime_profile_path():
    return get_chrome_profile_path()


def get_instagram_post_worker_count():
    try:
        configured_workers = int(os.getenv("INSTAGRAM_POST_WORKERS", "4"))
    except ValueError:
        configured_workers = 4

    return max(1, configured_workers)


def get_instagram_post_checkpoint_interval():
    try:
        configured_interval = int(os.getenv("INSTAGRAM_POST_CHECKPOINT_INTERVAL", "10"))
    except ValueError:
        configured_interval = 10

    return max(1, configured_interval)


def should_block_images_for_instagram_driver():
    return os.getenv("INSTAGRAM_BLOCK_IMAGES", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def create_driver(headless=True, profile_path=None):
    chrome_profile_path = Path(profile_path) if profile_path else get_chrome_profile_path()
    chrome_profile_path.mkdir(parents=True, exist_ok=True)
    block_images = should_block_images_for_instagram_driver()

    def build_options(user_data_dir):
        options = Options()
        options.page_load_strategy = "eager"
        options.add_argument(f"--user-data-dir={user_data_dir}")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--disable-sync")

        if block_images:
            options.add_argument("--blink-settings=imagesEnabled=false")

        options.add_experimental_option(
            "prefs",
            {
                "profile.default_content_setting_values.notifications": 2,
                "profile.managed_default_content_settings.images": 2 if block_images else 1,
            },
        )

        if headless:
            options.add_argument("--headless=new")

        return options

    try:
        return webdriver.Chrome(options=build_options(chrome_profile_path))
    except SessionNotCreatedException as exc:
        error_text = clean_text(exc).lower()
        if "failed to write prefs file" not in error_text:
            raise

        fallback_profile_path = RUNTIME_PROFILES_DIR / f"insta_fallback_{uuid.uuid4().hex[:8]}"
        fallback_profile_path.mkdir(parents=True, exist_ok=True)
        log_instagram(
            "Nao foi possivel escrever prefs no perfil do Instagram. "
            f"Usando perfil temporario: {fallback_profile_path}"
        )
        return webdriver.Chrome(options=build_options(fallback_profile_path))


def is_webdriver_window_lost_error(exception):
    error_text = clean_text(exception).lower()
    return isinstance(exception, NoSuchWindowException) or any(
        marker in error_text
        for marker in (
            "no such window",
            "target window already closed",
            "web view not found",
            "invalid session id",
        )
    )


def get_driver_current_url(driver, fallback=""):
    try:
        return driver.current_url
    except WebDriverException:
        return fallback


def get_driver_page_source(driver, fallback=""):
    try:
        return driver.page_source
    except WebDriverException:
        return fallback


def get_driver_title(driver, fallback=""):
    try:
        return driver.title or fallback
    except WebDriverException:
        return fallback


def recover_instagram_driver(driver, url, headless_mode, profile_path):
    log_instagram("Janela do Chrome fechou durante a coleta. Reabrindo navegador do worker.")

    try:
        driver.quit()
    except Exception:
        pass

    recovered_driver = create_driver(headless=headless_mode, profile_path=profile_path)
    if url:
        recovered_driver.get(url)

    return recovered_driver, headless_mode


def resolve_writable_output_path(path):
    if not path.exists():
        return path

    try:
        with path.open("ab"):
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
    json_output_path = resolve_writable_output_path(execution_output_dir / "insta.json")
    xlsx_output_path = resolve_writable_output_path(execution_output_dir / "insta.xlsx")
    categorized_records = build_categorized_output(records)

    json_output_path.write_text(
        json.dumps(categorized_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_instagram_workbook(xlsx_output_path, categorized_records)

    return json_output_path, xlsx_output_path


def resolve_instagram_review_paths(review_path):
    review_path = Path(review_path).expanduser()

    if not review_path.is_absolute():
        review_path = Path.cwd() / review_path

    review_path = review_path.resolve()

    if review_path.is_file():
        if review_path.name.lower() != "insta.json":
            raise FileNotFoundError(
                f"Arquivo de review invalido: {review_path}. Esperado insta.json."
            )
        return review_path, review_path.parent

    if not review_path.exists():
        raise FileNotFoundError(f"Caminho de review nao encontrado: {review_path}")

    direct_json_path = review_path / "insta.json"
    nested_json_path = review_path / OUTPUT_SUBDIR_NAME / "insta.json"

    if direct_json_path.is_file():
        return direct_json_path, review_path

    if nested_json_path.is_file():
        return nested_json_path, review_path

    raise FileNotFoundError(
        "Nao foi encontrado insta.json no caminho informado. "
        f"Verifique {review_path} ou {nested_json_path.parent}."
    )


def load_instagram_records_for_review(review_path):
    json_path, output_dir = resolve_instagram_review_paths(review_path)
    categorized_records = json.loads(json_path.read_text(encoding="utf-8"))
    loaded_records = []

    for category, records in categorized_records.items():
        if not isinstance(records, list):
            continue

        for record in records:
            if not isinstance(record, dict):
                continue

            loaded_record = dict(record)
            loaded_record["categoria"] = loaded_record.get("categoria", category)
            loaded_record["fonte"] = loaded_record.get("fonte", "")
            loaded_record["status"] = loaded_record.get("status", 0)
            loaded_record["error"] = loaded_record.get("error", "")
            loaded_record["data"] = loaded_record.get("data", 0)
            if loaded_record["categoria"] == "post":
                loaded_record.update(normalize_instagram_post_data(loaded_record))
            elif loaded_record["categoria"] == "reel":
                loaded_record.update(normalize_instagram_reel_data(loaded_record))
            loaded_records.append(loaded_record)

    return loaded_records, json_path, output_dir


def build_google_query(search_term, after=None, before=None):
    query_parts = ["site:instagram.com"]

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

    if "google." in host or "instagram.com" not in host:
        return None

    return parsed_target._replace(fragment="").geturl()


def categorize_instagram_url(url):
    path_parts = [part for part in urlparse(url).path.split("/") if part]

    if not path_parts:
        return "nao_identificado"

    first_path_part = path_parts[0].lower()

    if first_path_part == "p":
        return "post"

    if first_path_part in {"reel", "reels"}:
        return "reel"

    if len(path_parts) == 1 and first_path_part not in INSTAGRAM_PROFILE_RESERVED_PATHS:
        return "perfil"

    return "nao_identificado"


def build_categorized_output(records):
    categorized_records = {category: [] for category in INSTAGRAM_CATEGORY_ORDER}

    for record in records:
        category = record.get("categoria", "nao_identificado")
        categorized_records.setdefault(category, [])
        normalized_record = {
            "fonte": record.get("fonte", ""),
            "status": record.get("status", 0),
            "error": record.get("error", ""),
            "data": record.get("data", 0),
        }

        if category == "post":
            normalized_record.update(
                {
                    "perfil_publicador": record.get("perfil_publicador", ""),
                    "verificado": record.get("verificado", ""),
                    "descricao_post": record.get("descricao_post", ""),
                    "numero_likes": record.get("numero_likes", ""),
                    "numero_comentarios": record.get("numero_comentarios", ""),
                    "numero_reposts": record.get("numero_reposts", ""),
                }
            )
        elif category == "reel":
            normalized_record.update(
                {
                    "perfil_publicador": record.get("perfil_publicador", ""),
                    "verificado": record.get("verificado", ""),
                    "descricao_reel": record.get("descricao_reel", ""),
                    "numero_likes": record.get("numero_likes", ""),
                    "numero_comentarios": record.get("numero_comentarios", ""),
                    "numero_reposts": record.get("numero_reposts", ""),
                }
            )

        categorized_records[category].append(normalized_record)

    return categorized_records


def get_headers_for_category(category):
    if category == "post":
        return INSTAGRAM_POST_HEADERS
    if category == "reel":
        return INSTAGRAM_REEL_HEADERS

    return INSTAGRAM_DEFAULT_HEADERS


def get_excel_column_name(column_number):
    column_name = ""

    while column_number > 0:
        column_number, remainder = divmod(column_number - 1, 26)
        column_name = chr(65 + remainder) + column_name

    return column_name


def build_excel_cell(cell_reference, value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{cell_reference}"><v>{value}</v></c>'

    escaped_value = escape("" if value is None else str(value))
    return (
        f'<c r="{cell_reference}" t="inlineStr">'
        f"<is><t>{escaped_value}</t></is>"
        f"</c>"
    )


def build_excel_sheet_xml(records, headers):
    rows = []

    for row_index, row_values in enumerate([headers, *[
        [record.get(header, "") for header in headers]
        for record in records
    ]], start=1):
        cells = []
        for column_index, value in enumerate(row_values, start=1):
            cell_reference = f"{get_excel_column_name(column_index)}{row_index}"
            cells.append(build_excel_cell(cell_reference, value))

        rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rows)}</sheetData>"
        "</worksheet>"
    )


def write_instagram_workbook(path, categorized_records):
    workbook_xml = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
        "<sheets>",
    ]
    workbook_rels_xml = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">',
    ]
    content_types_xml = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
    ]

    for sheet_index, category in enumerate(INSTAGRAM_CATEGORY_ORDER, start=1):
        workbook_xml.append(
            f'<sheet name="{escape(category)}" sheetId="{sheet_index}" r:id="rId{sheet_index}"/>'
        )
        workbook_rels_xml.append(
            '<Relationship '
            f'Id="rId{sheet_index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{sheet_index}.xml"/>'
        )
        content_types_xml.append(
            '<Override '
            f'PartName="/xl/worksheets/sheet{sheet_index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    workbook_xml.extend(["</sheets>", "</workbook>"])
    workbook_rels_xml.append("</Relationships>")
    content_types_xml.append("</Types>")

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as workbook_file:
        workbook_file.writestr(
            "[Content_Types].xml",
            "".join(content_types_xml),
        )
        workbook_file.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        workbook_file.writestr("xl/workbook.xml", "".join(workbook_xml))
        workbook_file.writestr("xl/_rels/workbook.xml.rels", "".join(workbook_rels_xml))

        for sheet_index, category in enumerate(INSTAGRAM_CATEGORY_ORDER, start=1):
            workbook_file.writestr(
                f"xl/worksheets/sheet{sheet_index}.xml",
                build_excel_sheet_xml(
                    categorized_records.get(category, []),
                    get_headers_for_category(category),
                ),
            )


def is_instagram_scrape_blocked(driver):
    current_url = get_driver_current_url(driver).lower()
    page_source = get_driver_page_source(driver).lower()
    blocked_markers = [
        "captcha",
        "challenge_required",
        "checkpoint_required",
        "please wait a few minutes before you try again",
        "sorry, this page isn't available",
        "page isn't available",
        "login • instagram",
        "entrar • instagram",
    ]
    blocked_paths = [
        "/accounts/login",
        "/challenge/",
        "/checkpoint/",
    ]

    if any(path in current_url for path in blocked_paths):
        return True

    return any(marker in page_source for marker in blocked_markers)


def clean_text(value):
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_search_text(value):
    cleaned_value = clean_text(value)
    if not cleaned_value:
        return ""

    return (
        unicodedata.normalize("NFKD", cleaned_value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )


def parse_count(value):
    if value in (None, ""):
        return ""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)

    normalized_text = clean_text(value).lower()
    normalized_text = normalized_text.replace("\xa0", " ")
    number_match = re.search(r"(\d+(?:[.,]\d+)?)", normalized_text)
    if not number_match:
        return ""

    try:
        number_text = number_match.group(1)
        multiplier = 1
        suffix_text = normalized_text[number_match.end():].strip()
        suffix_match = re.match(
            r"^(mil|mi|bi|k|m|b)\b",
            suffix_text,
        )

        if suffix_match:
            suffix = suffix_match.group(1)
            if suffix in ("mil", "k"):
                multiplier = 1_000
            elif suffix in ("mi", "m"):
                multiplier = 1_000_000
            elif suffix in ("bi", "b"):
                multiplier = 1_000_000_000
        elif re.search(r"\b(million|milhao|milhoes)\b", normalized_text):
            multiplier = 1_000_000
        elif re.search(r"\b(billion|bilhao|bilhoes)\b", normalized_text):
            multiplier = 1_000_000_000

        if multiplier > 1:
            if "," in number_text and "." in number_text:
                decimal_text = number_text.replace(".", "").replace(",", ".")
            elif "," in number_text:
                decimal_text = number_text.replace(",", ".")
            else:
                decimal_text = number_text

            decimal_number = float(decimal_text)
            return int(decimal_number * multiplier)

        return int(re.sub(r"[^\d]", "", number_text))
    except ValueError:
        return ""


def normalize_instagram_media_data(data=None, defaults=None, description_field="descricao_post"):
    normalized_data = dict(defaults or {})

    if isinstance(data, dict):
        normalized_data.update(data)

    for key in ("perfil_publicador", "verificado", description_field, "data"):
        normalized_data[key] = clean_text(normalized_data.get(key, ""))

    normalized_data["perfil_publicador"] = normalized_data["perfil_publicador"].lstrip("@")

    return normalized_data


def normalize_instagram_post_data(data=None):
    return normalize_instagram_media_data(
        data=data,
        defaults=INSTAGRAM_POST_DATA_DEFAULTS,
        description_field="descricao_post",
    )


def normalize_instagram_reel_data(data=None):
    return normalize_instagram_media_data(
        data=data,
        defaults=INSTAGRAM_REEL_DATA_DEFAULTS,
        description_field="descricao_reel",
    )


def is_instagram_login_popup_present(driver):
    page_source = get_driver_page_source(driver).lower()

    if not page_source:
        return False

    for close_xpath in INSTAGRAM_LOGIN_POPUP_CLOSE_XPATHS:
        try:
            close_buttons = driver.find_elements(By.XPATH, close_xpath)
            if any(button.is_displayed() for button in close_buttons):
                return True
        except Exception:
            continue

    if not any(marker in page_source for marker in INSTAGRAM_LOGIN_POPUP_TEXT_MARKERS):
        return False

    try:
        dialog_elements = driver.find_elements(By.XPATH, "//div[@role='dialog']")
        if any(dialog.is_displayed() for dialog in dialog_elements):
            return True
    except Exception:
        pass

    return False


def has_instagram_media_content(driver):
    page_source = get_driver_page_source(driver)
    normalized_source = page_source.lower()

    if (
        "application/ld+json" in normalized_source
        or '"is_verified":' in normalized_source
        or '"edge_media_to_comment"' in normalized_source
        or '"edge_media_preview_like"' in normalized_source
    ):
        return True

    content_selectors = [
        "main time[datetime]",
        "main video",
        "main a[href*='/reel/']",
        "main a[href*='/p/']",
        "main svg[aria-label='Curtir']",
        "main svg[aria-label='Like']",
        "main svg[aria-label='Comentar']",
        "main svg[aria-label='Comment']",
    ]

    for selector in content_selectors:
        try:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
        except Exception:
            continue

    return False


def get_visible_instagram_captcha_marker(driver):
    captcha_selectors = [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "iframe[title*='captcha' i]",
        ".g-recaptcha",
        ".h-captcha",
        "[data-sitekey]",
        "textarea[name='g-recaptcha-response']",
        "textarea[name='h-captcha-response']",
    ]

    for selector in captcha_selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if any(element.is_displayed() for element in elements):
                return selector
        except Exception:
            continue

    return None


def get_instagram_captcha_marker(driver, page_source, page_title):
    visible_marker = get_visible_instagram_captcha_marker(driver)
    if visible_marker:
        return f"visible:{visible_marker}"

    source_markers = [
        "g-recaptcha",
        "h-captcha",
        "recaptcha/api.js",
        "hcaptcha.com/1/api.js",
        'name="g-recaptcha-response"',
        'name="h-captcha-response"',
        "security code",
        "enter the code",
        "confirm you're not a robot",
        "confirm you are not a robot",
    ]

    for marker in source_markers:
        if marker in page_source:
            return f"source:{marker}"

    title_markers = [
        "confirm you're not a robot",
        "confirm you are not a robot",
    ]

    for marker in title_markers:
        if marker in page_title:
            return f"title:{marker}"

    return None


def get_instagram_scrape_block(driver):
    current_url = get_driver_current_url(driver).lower()
    current_host = urlparse(current_url).netloc.lower()
    page_source = get_driver_page_source(driver).lower()
    page_title = get_driver_title(driver).lower()

    if not current_url and not page_source:
        return {
            "type": "webdriver_disconnected",
            "error": "instagram_webdriver_disconnected",
            "detail": "Conexao com o navegador foi perdida (invalid session/devtools).",
            "manual_resolution": False,
        }
    captcha_marker = get_instagram_captcha_marker(driver, page_source, page_title)

    if is_instagram_login_popup_present(driver):
        return None

    if "/challenge/" in current_url or "challenge_required" in page_source:
        return {
            "type": "challenge_required",
            "error": "instagram_challenge_required",
            "detail": "Instagram solicitou challenge/verificacao.",
            "manual_resolution": True,
        }

    if "/checkpoint/" in current_url or "checkpoint_required" in page_source:
        return {
            "type": "checkpoint_required",
            "error": "instagram_checkpoint_required",
            "detail": "Instagram solicitou checkpoint/verificacao de conta.",
            "manual_resolution": True,
        }

    if "instagram." in current_host and captcha_marker:
        return {
            "type": "captcha",
            "error": "instagram_captcha",
            "detail": f"Instagram exibiu captcha. marcador={captcha_marker}",
            "manual_resolution": True,
        }

    if "/accounts/login" in current_url:
        return {
            "type": "login_required",
            "error": "instagram_login_required",
            "detail": "Instagram redirecionou para login.",
            "manual_resolution": True,
        }

    if "please wait a few minutes before you try again" in page_source:
        return {
            "type": "rate_limit",
            "error": "instagram_rate_limit",
            "detail": "Instagram pediu para aguardar alguns minutos antes de tentar novamente.",
            "manual_resolution": False,
        }

    if "sorry, this page isn't available" in page_source or "page isn't available" in page_source:
        return {
            "type": "page_unavailable",
            "error": "instagram_page_unavailable",
            "detail": "Pagina do Instagram indisponivel ou removida.",
            "manual_resolution": False,
        }

    return None


def is_instagram_scrape_blocked(driver):
    return get_instagram_scrape_block(driver) is not None


def click_instagram_login_popup_close_with_script(driver):
    return driver.execute_script(
        """
        const dialogs = Array.from(document.querySelectorAll('div[role="dialog"]'));
        for (const dialog of dialogs) {
            if (!dialog.offsetParent && getComputedStyle(dialog).position !== 'fixed') {
                continue;
            }

            const labelledNodes = Array.from(dialog.querySelectorAll('[aria-label], title'));
            for (const node of labelledNodes) {
                const label = (
                    node.getAttribute('aria-label') || node.textContent || ''
                ).trim().toLowerCase();
                if (!['fechar', 'close'].includes(label)) {
                    continue;
                }

                const clickable = node.closest('button, [role="button"], a');
                if (!clickable) {
                    continue;
                }

                clickable.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                clickable.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                clickable.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                clickable.click();
                return true;
            }
        }
        return false;
        """
    )


def close_instagram_login_popup(driver, timeout=3):
    deadline = time.time() + timeout

    try:
        if click_instagram_login_popup_close_with_script(driver):
            time.sleep(0.5)
            if not is_instagram_login_popup_present(driver):
                log_instagram("Popup de login do Instagram fechado.")
                return True
    except Exception:
        pass

    for close_xpath in INSTAGRAM_LOGIN_POPUP_CLOSE_XPATHS:
        while time.time() < deadline:
            close_buttons = driver.find_elements(By.XPATH, close_xpath)
            visible_buttons = [button for button in close_buttons if button.is_displayed()]

            if not visible_buttons:
                break

            close_button = visible_buttons[0]

            try:
                driver.execute_script("arguments[0].click();", close_button)
            except Exception:
                try:
                    close_button.click()
                except Exception:
                    break

            time.sleep(0.5)

            if not any(
                button.is_displayed()
                for button in driver.find_elements(By.XPATH, close_xpath)
            ):
                log_instagram("Popup de login do Instagram fechado.")
                return True

    try:
        driver.execute_script(
            "document.dispatchEvent(new KeyboardEvent('keydown', "
            "{key: 'Escape', code: 'Escape', keyCode: 27, which: 27, bubbles: true}));"
        )
        time.sleep(0.5)
        if not is_instagram_login_popup_present(driver):
            log_instagram("Popup de login do Instagram fechado com Escape.")
            return True
    except Exception:
        pass

    return False


def format_publication_date(value):
    if value in (None, "", 0, "0"):
        return ""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed_datetime = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return parsed_datetime.strftime("%Y-%m-%d %H:%M:%S")

    cleaned_value = clean_text(value)
    if not cleaned_value:
        return ""

    if cleaned_value.isdigit():
        parsed_datetime = datetime.fromtimestamp(int(cleaned_value), tz=timezone.utc)
        return parsed_datetime.strftime("%Y-%m-%d %H:%M:%S")

    iso_candidate = cleaned_value.replace("Z", "+00:00")
    try:
        parsed_datetime = datetime.fromisoformat(iso_candidate)
        return parsed_datetime.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return cleaned_value


def load_json_objects_from_scripts(soup):
    json_objects = []

    for script in soup.select('script[type="application/ld+json"]'):
        script_content = script.string or script.get_text(strip=True)
        if not script_content:
            continue

        try:
            loaded_json = json.loads(script_content)
        except json.JSONDecodeError:
            continue

        if isinstance(loaded_json, list):
            json_objects.extend(item for item in loaded_json if isinstance(item, dict))
        elif isinstance(loaded_json, dict):
            json_objects.append(loaded_json)

    return json_objects


def extract_count_from_interaction_statistic(interaction_statistic):
    if isinstance(interaction_statistic, dict):
        interaction_statistic = [interaction_statistic]

    if not isinstance(interaction_statistic, list):
        return ""

    for statistic in interaction_statistic:
        if not isinstance(statistic, dict):
            continue

        interaction_type = statistic.get("interactionType")
        if isinstance(interaction_type, dict):
            interaction_type = interaction_type.get("@type", "")

        if interaction_type and "like" in str(interaction_type).lower():
            return parse_count(statistic.get("userInteractionCount"))

    return ""


def extract_post_data_from_json_objects(json_objects):
    extracted_data = normalize_instagram_post_data()

    for json_object in json_objects:
        author = json_object.get("author")
        if isinstance(author, dict):
            if not extracted_data.get("perfil_publicador"):
                extracted_data["perfil_publicador"] = clean_text(
                    author.get("alternateName")
                    or author.get("identifier")
                    or author.get("name")
                ).lstrip("@")

        if not extracted_data.get("descricao_post"):
            extracted_data["descricao_post"] = clean_text(
                json_object.get("caption")
                or json_object.get("description")
            )

        if extracted_data.get("numero_likes", "") == "":
            extracted_data["numero_likes"] = extract_count_from_interaction_statistic(
                json_object.get("interactionStatistic")
            )

        if extracted_data.get("numero_comentarios", "") == "":
            extracted_data["numero_comentarios"] = parse_count(json_object.get("commentCount"))

        if extracted_data.get("numero_reposts", "") == "":
            extracted_data["numero_reposts"] = parse_count(
                json_object.get("shareCount")
                or json_object.get("reshareCount")
                or json_object.get("repostCount")
            )

        if not extracted_data.get("data"):
            extracted_data["data"] = format_publication_date(
                json_object.get("uploadDate")
                or json_object.get("datePublished")
                or json_object.get("dateCreated")
            )

    return normalize_instagram_post_data(extracted_data)


def extract_post_data_from_page_source(page_source):
    soup = BeautifulSoup(page_source, "html.parser")
    json_objects = load_json_objects_from_scripts(soup)
    extracted_data = normalize_instagram_post_data(extract_post_data_from_json_objects(json_objects))

    meta_title = soup.find("meta", attrs={"property": "og:title"})
    meta_description = soup.find("meta", attrs={"property": "og:description"})
    time_element = soup.find("time")

    if not extracted_data.get("data") and time_element:
        extracted_data["data"] = format_publication_date(time_element.get("datetime"))

    if not extracted_data.get("descricao_post") and meta_title:
        title_content = clean_text(meta_title.get("content", ""))
        title_match = re.search(r'Instagram:\s*"(.+?)"', title_content)
        if title_match:
            extracted_data["descricao_post"] = clean_text(title_match.group(1))

    if not extracted_data.get("perfil_publicador") and meta_title:
        title_content = clean_text(meta_title.get("content", ""))
        title_match = re.match(r"([^:]+?) on Instagram", title_content, flags=re.IGNORECASE)
        if title_match:
            extracted_data["perfil_publicador"] = clean_text(title_match.group(1)).lstrip("@")

    if meta_description:
        description_content = clean_text(meta_description.get("content", ""))

        for field, rule in INSTAGRAM_METRIC_EXTRACTION_RULES.items():
            if extracted_data.get(field, "") != "":
                continue

            extracted_value = extract_metric_from_text_candidates(
                [description_content],
                rule["keywords"],
                extra_patterns=rule["extra_patterns"],
            )
            if extracted_value != "":
                extracted_data[field] = extracted_value

        if not extracted_data.get("descricao_post"):
            description_match = re.search(r':\s*"(.+?)"', description_content)
            if description_match:
                extracted_data["descricao_post"] = clean_text(description_match.group(1))

    owner_match = re.search(r'"owner":\{.*?"username":"([^"]+)"', page_source, flags=re.S)
    if owner_match and not extracted_data.get("perfil_publicador"):
        extracted_data["perfil_publicador"] = clean_text(owner_match.group(1)).lstrip("@")

    verified_match = re.search(r'"is_verified":(true|false)', page_source)
    if verified_match:
        extracted_data["verificado"] = "sim" if verified_match.group(1) == "true" else "nao"

    if extracted_data.get("numero_likes", "") == "":
        likes_match = re.search(
            r'"edge_media_preview_like":\{"count":(\d+)\}|'
            r'"edge_liked_by":\{"count":(\d+)\}',
            page_source,
        )
        if likes_match:
            extracted_data["numero_likes"] = parse_count(likes_match.group(1) or likes_match.group(2))

    if extracted_data.get("numero_comentarios", "") == "":
        comments_match = re.search(
            r'"edge_media_to_parent_comment":\{"count":(\d+)\}|'
            r'"edge_media_to_comment":\{"count":(\d+)\}',
            page_source,
        )
        if comments_match:
            extracted_data["numero_comentarios"] = parse_count(
                comments_match.group(1) or comments_match.group(2)
            )

    if extracted_data.get("numero_reposts", "") == "":
        reposts_match = re.search(
            r'"share_count":(\d+)|'
            r'"reshare_count":(\d+)|'
            r'"repost_count":(\d+)|'
            r'"edge_media_to_reshare":\{"count":(\d+)\}|'
            r'"edge_media_to_repost":\{"count":(\d+)\}',
            page_source,
        )
        if reposts_match:
            extracted_data["numero_reposts"] = parse_count(
                reposts_match.group(1)
                or reposts_match.group(2)
                or reposts_match.group(3)
                or reposts_match.group(4)
                or reposts_match.group(5)
            )

    if not extracted_data.get("data"):
        publication_date_match = re.search(
            r'"taken_at_timestamp":(\d+)|'
            r'"taken_at":(\d+)|'
            r'"datePublished":"([^"]+)"|'
            r'"uploadDate":"([^"]+)"',
            page_source,
        )
        if publication_date_match:
            extracted_data["data"] = format_publication_date(
                publication_date_match.group(1)
                or publication_date_match.group(2)
                or publication_date_match.group(3)
                or publication_date_match.group(4)
            )

    extracted_data["data"] = format_publication_date(extracted_data["data"])

    return normalize_instagram_post_data(extracted_data)


def extract_instagram_profile_from_href(href):
    if not href:
        return ""

    path_parts = [part for part in urlparse(href).path.split("/") if part]
    if len(path_parts) != 1:
        return ""

    username = clean_text(path_parts[0]).lstrip("@")
    if not username or username.lower() in INSTAGRAM_PROFILE_RESERVED_PATHS:
        return ""

    return username


def is_likely_instagram_caption_text(text):
    normalized_text = normalize_search_text(text)
    if len(text) < 20 or not normalized_text:
        return False

    rejected_markers = [
        "entrar",
        "cadastre se",
        "curtir",
        "responder",
        "carregar mais comentarios",
        "para curtir ou comentar",
        "mais posts de",
        "ver mais posts",
        "audio original",
    ]
    if any(marker in normalized_text for marker in rejected_markers):
        return False

    if parse_compact_metric_text(text) != "":
        return False

    return True


def extract_caption_from_rendered_soup(soup):
    time_element = soup.find("time")
    if time_element:
        search_elements = time_element.find_all_next(["span", "div"], limit=160)
    else:
        search_elements = soup.select("span, div")

    for element in search_elements:
        if element.name == "div" and element.find(["span", "div"]):
            continue

        text = clean_text(element.get_text(" ", strip=True))
        if is_likely_instagram_caption_text(text):
            return text

    return ""


def capture_post_page_snapshot(driver):
    rendered_html_fragments = []
    seen_html = set()
    for selector in ("article", "main"):
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue

        for element in elements:
            try:
                html = element.get_attribute("outerHTML")
            except Exception:
                continue

            html = html or ""
            if not clean_text(html) or html in seen_html:
                continue

            rendered_html_fragments.append(html)
            seen_html.add(html)

    return {
        "page_source": get_driver_page_source(driver),
        "rendered_html_fragments": rendered_html_fragments,
    }


def build_post_soups_from_snapshot(page_source, rendered_html_fragments=None):
    soups = []
    seen_html = set()

    for html in rendered_html_fragments or []:
        html = html or ""
        if not clean_text(html) or html in seen_html:
            continue

        soups.append(BeautifulSoup(html, "html.parser"))
        seen_html.add(html)

    if not soups:
        soups.append(BeautifulSoup(page_source, "html.parser"))

    return soups


def collect_metric_text_candidates_from_soup(soup):
    candidates = []
    seen_texts = set()

    for element in soup.select("[aria-label], [title], button, a, span, div, li, time"):
        raw_values = [
            element.get("aria-label", ""),
            element.get("title", ""),
            element.get_text(" ", strip=True),
        ]

        for raw_value in raw_values:
            text = clean_text(raw_value)
            normalized_text = normalize_search_text(text)
            if (
                not text
                or not normalized_text
                or len(text) > 160
                or normalized_text in seen_texts
            ):
                continue

            candidates.append(text)
            seen_texts.add(normalized_text)

    return candidates


def parse_compact_metric_text(text):
    cleaned_text = clean_text(text)
    if not cleaned_text:
        return ""

    normalized_text = normalize_search_text(cleaned_text)
    if not re.fullmatch(r"\d+(?:[.,]\d+)?(?:\s*(?:mil|mi|bi|k|m|b))?", normalized_text):
        return ""

    return parse_count(cleaned_text)


def classify_instagram_metric_action(label_text):
    normalized_label = normalize_search_text(label_text)
    if not normalized_label:
        return ""

    if any(keyword in normalized_label for keyword in ("curtir", "like")):
        return "numero_likes"
    if any(keyword in normalized_label for keyword in ("comentar", "comment")):
        return "numero_comentarios"
    if any(keyword in normalized_label for keyword in ("compartilhar", "repostar", "repost", "share")):
        return "numero_reposts"

    return ""


def iter_instagram_metric_action_nodes(soup):
    action_nodes = []
    seen_nodes = set()

    for svg in soup.select("svg[aria-label], svg[title]"):
        label_text = svg.get("aria-label") or svg.get("title") or ""
        metric_field = classify_instagram_metric_action(label_text)
        if metric_field and id(svg) not in seen_nodes:
            action_nodes.append({"field": metric_field, "node": svg})
            seen_nodes.add(id(svg))

    for title_element in soup.select("svg title"):
        metric_field = classify_instagram_metric_action(title_element.get_text(" ", strip=True))
        svg = title_element.parent
        if metric_field and svg is not None and id(svg) not in seen_nodes:
            action_nodes.append({"field": metric_field, "node": svg})
            seen_nodes.add(id(svg))

    return action_nodes


def select_instagram_metric_action_bar(action_nodes):
    ancestor_candidates = {}

    for action_node in action_nodes:
        current = action_node["node"]
        for depth in range(6):
            current = getattr(current, "parent", None)
            if current is None or not getattr(current, "name", None):
                break

            entry = ancestor_candidates.setdefault(
                id(current),
                {
                    "node": current,
                    "fields": set(),
                    "depth_score": 0,
                    "text_length": 0,
                },
            )
            entry["fields"].add(action_node["field"])
            entry["depth_score"] += depth + 1

    ranked_candidates = []
    for entry in ancestor_candidates.values():
        if len(entry["fields"]) < 3:
            continue

        entry["text_length"] = len(clean_text(entry["node"].get_text(" ", strip=True)))
        ranked_candidates.append(entry)

    if not ranked_candidates:
        return None

    ranked_candidates.sort(
        key=lambda entry: (
            -len(entry["fields"]),
            entry["text_length"],
            entry["depth_score"],
        )
    )
    return ranked_candidates[0]["node"]


def extract_metric_count_from_adjacent_nodes(node, container):
    current = node

    while current is not None and current is not container:
        for sibling in current.next_siblings:
            if not getattr(sibling, "name", None):
                continue

            direct_value = parse_compact_metric_text(sibling.get_text(" ", strip=True))
            if direct_value != "":
                return direct_value

            for nested_element in sibling.find_all(["span", "div", "a"], limit=6):
                nested_value = parse_compact_metric_text(nested_element.get_text(" ", strip=True))
                if nested_value != "":
                    return nested_value

        current = getattr(current, "parent", None)

    return ""


def extract_metrics_from_instagram_action_bar(soup):
    extracted_metrics = {}
    action_nodes = iter_instagram_metric_action_nodes(soup)
    action_bar = select_instagram_metric_action_bar(action_nodes)
    if action_bar is None:
        return extracted_metrics

    for action_node in action_nodes:
        if action_node["field"] in extracted_metrics:
            continue
        if action_bar not in getattr(action_node["node"], "parents", []):
            continue

        extracted_value = extract_metric_count_from_adjacent_nodes(
            action_node["node"],
            action_bar,
        )
        if extracted_value != "":
            extracted_metrics[action_node["field"]] = extracted_value

    return extracted_metrics


def parse_count_parts(number_part, suffix_part=""):
    value_parts = [clean_text(number_part), clean_text(suffix_part)]
    combined_value = " ".join(part for part in value_parts if part)
    return parse_count(combined_value)


def extract_metric_values_from_text(text, keywords, extra_patterns=None):
    normalized_text = normalize_search_text(text)
    if not normalized_text:
        return []

    normalized_keywords = [normalize_search_text(keyword) for keyword in keywords]
    normalized_keywords = [keyword for keyword in normalized_keywords if keyword]
    if not normalized_keywords:
        return []

    count_pattern = r"(?P<count>\d+(?:[.,]\d+)?)\s*(?P<suffix>mil|mi|bi|k|m|b)?"
    keywords_pattern = "|".join(re.escape(keyword) for keyword in normalized_keywords)
    regex_patterns = [
        rf"{count_pattern}\s+(?:{keywords_pattern})\b",
        rf"(?:{keywords_pattern})\b[\s:,-]*{count_pattern}",
    ]

    if extra_patterns:
        regex_patterns.extend(extra_patterns)

    extracted_values = []
    for pattern in regex_patterns:
        for match in re.finditer(pattern, normalized_text):
            parsed_value = parse_count_parts(match.group("count"), match.groupdict().get("suffix", ""))
            if parsed_value != "":
                extracted_values.append(parsed_value)

    return extracted_values


def extract_metric_from_text_candidates(text_candidates, keywords, extra_patterns=None):
    extracted_values = []

    for text in text_candidates:
        extracted_values.extend(
            extract_metric_values_from_text(
                text,
                keywords,
                extra_patterns=extra_patterns,
            )
        )

    if not extracted_values:
        return ""

    return max(extracted_values)


def extract_post_data_from_rendered_soups(soups):
    extracted_data = normalize_instagram_post_data()

    all_metric_candidates = []

    for soup in soups:
        if not extracted_data.get("data"):
            time_element = soup.find("time")
            if time_element:
                extracted_data["data"] = format_publication_date(
                    time_element.get("datetime") or time_element.get_text(" ", strip=True)
                )

        if not extracted_data.get("perfil_publicador"):
            for anchor in soup.select("header a[href], a[href]"):
                profile_name = extract_instagram_profile_from_href(anchor.get("href", ""))
                if profile_name:
                    extracted_data["perfil_publicador"] = profile_name
                    break

        if not extracted_data.get("descricao_post"):
            heading = soup.find("h1")
            if heading:
                description_text = clean_text(heading.get_text(" ", strip=True))
                if description_text:
                    extracted_data["descricao_post"] = description_text

        if not extracted_data.get("descricao_post"):
            caption_text = extract_caption_from_rendered_soup(soup)
            if caption_text:
                extracted_data["descricao_post"] = caption_text

        if not extracted_data.get("verificado"):
            for element in soup.select("[aria-label], [title], title"):
                marker_text = clean_text(
                    element.get("aria-label")
                    or element.get("title")
                    or element.get_text(" ", strip=True)
                )
                normalized_marker = normalize_search_text(marker_text)
                if any(
                    keyword in normalized_marker
                    for keyword in ("verified", "verificado", "meta verified")
                ):
                    extracted_data["verificado"] = "sim"
                    break

        for field, value in extract_metrics_from_instagram_action_bar(soup).items():
            if extracted_data.get(field, "") == "":
                extracted_data[field] = value

        all_metric_candidates.extend(collect_metric_text_candidates_from_soup(soup))

    for field, rule in INSTAGRAM_METRIC_EXTRACTION_RULES.items():
        if extracted_data.get(field, "") != "":
            continue

        extracted_value = extract_metric_from_text_candidates(
            all_metric_candidates,
            rule["keywords"],
            extra_patterns=rule["extra_patterns"],
        )
        if extracted_value != "":
            extracted_data[field] = extracted_value

    return normalize_instagram_post_data(extracted_data)


def extract_post_data_from_snapshot(snapshot):
    page_source = snapshot.get("page_source", "")
    rendered_html_fragments = snapshot.get("rendered_html_fragments", [])
    rendered_data = extract_post_data_from_rendered_soups(
        build_post_soups_from_snapshot(page_source, rendered_html_fragments)
    )
    extracted_data = normalize_instagram_post_data(extract_post_data_from_page_source(page_source))

    for field in INSTAGRAM_POST_DATA_DEFAULTS:
        if rendered_data.get(field) not in (None, ""):
            extracted_data[field] = rendered_data[field]

    return normalize_instagram_post_data(extracted_data)


def extract_post_data_from_driver(driver):
    return extract_post_data_from_snapshot(capture_post_page_snapshot(driver))


def prepare_media_record_for_enrichment(record):
    record.setdefault("perfil_publicador", "")
    record.setdefault("verificado", "")
    record.setdefault("descricao_post", "")
    record.setdefault("descricao_reel", "")
    record.setdefault("numero_likes", "")
    record.setdefault("numero_comentarios", "")
    record.setdefault("numero_reposts", "")
    record.setdefault("error", "")


def adapt_extracted_media_data_for_record(record, extracted_data):
    category = record.get("categoria")

    if category == "reel":
        return normalize_instagram_reel_data(
            {
                "perfil_publicador": extracted_data.get("perfil_publicador", ""),
                "verificado": extracted_data.get("verificado", ""),
                "descricao_reel": extracted_data.get("descricao_post", ""),
                "numero_likes": extracted_data.get("numero_likes", ""),
                "numero_comentarios": extracted_data.get("numero_comentarios", ""),
                "numero_reposts": extracted_data.get("numero_reposts", ""),
                "data": extracted_data.get("data", ""),
            }
        )

    return normalize_instagram_post_data(extracted_data)


def finalize_post_parse_job(parse_job, total_posts, records, output_dir=None):
    if not parse_job:
        return

    index = parse_job["index"]
    record = parse_job["record"]
    future = parse_job["future"]

    try:
        extracted_data = future.result()
        record.update(extracted_data)
        record["status"] = 2
        record["error"] = ""
        log_instagram(
            f"[posts {format_progress(index, total_posts)}] "
            "Post enriquecido com sucesso. "
            f"likes={record.get('numero_likes', '')}, "
            f"comentarios={record.get('numero_comentarios', '')}, "
            f"reposts={record.get('numero_reposts', '')}"
        )
    except Exception as exc:
        record["status"] = 1
        record["error"] = clean_text(exc)
        log_instagram(
            f"[posts {format_progress(index, total_posts)}] "
            f"Falha ao enriquecer post: {record['error']}"
        )

    save_results(records, output_dir=output_dir)
    log_instagram(
        f"[posts {format_progress(index, total_posts)}] "
        f"Checkpoint salvo. resumo={summarize_record_counts(records)}"
    )


def wait_for_post_page_content(driver, timeout=15):
    deadline = time.time() + timeout

    while time.time() < deadline:
        close_instagram_login_popup(driver, timeout=1)

        if has_instagram_media_content(driver):
            return

        if is_instagram_scrape_blocked(driver):
            return

        if is_instagram_login_popup_present(driver):
            time.sleep(0.5)
            continue

        if (
            driver.find_elements(By.TAG_NAME, "article")
            and driver.find_elements(By.CSS_SELECTOR, "article svg[aria-label]")
        ):
            return

        time.sleep(0.5)


def restore_headless_mode_for_post(driver, headless_mode, profile_path, timeout=15):
    if headless_mode:
        return driver, headless_mode

    log_instagram("Captcha resolvido na pagina do post. Retornando ao modo headless.")
    current_url = get_driver_current_url(driver)
    driver = reopen_driver(driver, current_url, headless=True, profile_path=profile_path)
    headless_mode = True
    wait_for_post_page_content(driver, timeout=timeout)
    return driver, headless_mode


def wait_for_post_scrape_page(driver, headless_mode, profile_path, timeout=15, target_url=None):
    started_headless = headless_mode
    driver, headless_mode = wait_for_captcha_resolution(driver, headless_mode, profile_path)
    wait_for_post_page_content(driver, timeout=timeout)
    driver, headless_mode = wait_for_captcha_resolution(driver, headless_mode, profile_path)
    driver, headless_mode = wait_for_instagram_block_resolution(driver, headless_mode, profile_path)

    if target_url and started_headless and not headless_mode and not get_instagram_scrape_block(driver):
        log_instagram(f"Reabrindo post original apos resolucao manual: {target_url}")
        driver.get(target_url)
        wait_for_post_page_content(driver, timeout=timeout)
        driver, headless_mode = wait_for_captcha_resolution(driver, headless_mode, profile_path)
        driver, headless_mode = wait_for_instagram_block_resolution(driver, headless_mode, profile_path)

    driver, headless_mode = restore_headless_mode_for_post(
        driver,
        headless_mode,
        profile_path,
        timeout=timeout,
    )
    return driver, headless_mode


def enrich_single_media_record(record, index, total_posts, driver, headless_mode, profile_path):
    prepare_media_record_for_enrichment(record)

    extracted_data = {}
    status = 1
    error = ""
    media_label = "reel" if record.get("categoria") == "reel" else "post"

    for attempt in range(2):
        try:
            log_instagram(
                f"[midia {format_progress(index, total_posts)}] "
                f"Abrindo {media_label}: {record['fonte']}"
            )
            driver.get(record["fonte"])
            driver, headless_mode = wait_for_post_scrape_page(
                driver,
                headless_mode,
                profile_path,
                target_url=record["fonte"],
            )

            if is_instagram_login_popup_present(driver):
                close_instagram_login_popup(driver, timeout=2)
                wait_for_post_page_content(driver, timeout=5)

            block = get_instagram_scrape_block(driver)
            if block:
                error = block["error"]
                log_instagram(
                    f"[midia {format_progress(index, total_posts)}] "
                    f"Bloqueio detectado ao abrir o {media_label}. "
                    f"tipo={block['type']}, erro={block['error']}, "
                    f"manual={block['manual_resolution']}, detalhe={block['detail']}, "
                    f"url={get_driver_current_url(driver, record['fonte'])}"
                )
            elif is_instagram_login_popup_present(driver) and not has_instagram_media_content(driver):
                error = "popup_login_instagram"
                log_instagram(
                    f"[midia {format_progress(index, total_posts)}] "
                    f"Popup de login persistiu e impediu a coleta do {media_label}."
                )
            else:
                snapshot = capture_post_page_snapshot(driver)
                extracted_data = adapt_extracted_media_data_for_record(
                    record,
                    extract_post_data_from_snapshot(snapshot),
                )
                status = 2
                error = ""
                log_instagram(
                    f"[midia {format_progress(index, total_posts)}] "
                    f"{media_label.capitalize()} enriquecido com sucesso. "
                    f"likes={extracted_data.get('numero_likes', '')}, "
                    f"comentarios={extracted_data.get('numero_comentarios', '')}, "
                    f"reposts={extracted_data.get('numero_reposts', '')}"
                )

            break
        except Exception as exc:
            if attempt == 0 and is_webdriver_window_lost_error(exc):
                driver, headless_mode = recover_instagram_driver(
                    driver,
                    record["fonte"],
                    headless_mode,
                    profile_path,
                )
                continue

            error = clean_text(exc)
            log_instagram(
                f"[midia {format_progress(index, total_posts)}] "
                f"Falha ao enriquecer {media_label}: {error}"
            )
            break

    return {
        "status": status,
        "error": error,
        "data": extracted_data,
        "driver": driver,
        "headless_mode": headless_mode,
    }


def enrich_post_records(records, driver, headless_mode, profile_path, output_dir=None):
    post_records = [
        record
        for record in records
        if record.get("categoria") in {"post", "reel"}
    ]
    pending_post_records = [record for record in post_records if record.get("status") != 2]

    log_instagram(
        "Iniciando enriquecimento de posts/reels. "
        f"midias_total={len(post_records)}, pendentes={len(pending_post_records)}"
    )

    if not pending_post_records:
        return records, driver, headless_mode

    worker_count = min(get_instagram_post_worker_count(), len(pending_post_records))
    checkpoint_interval = get_instagram_post_checkpoint_interval()
    indexed_post_records = {
        id(record): index for index, record in enumerate(post_records, start=1)
    }
    pending_records_queue = Queue()
    state_lock = threading.Lock()
    progress_state = {"completed": 0, "last_saved": 0}

    for record in pending_post_records:
        pending_records_queue.put(record)

    worker_contexts = create_instagram_worker_contexts(
        driver,
        headless_mode,
        profile_path,
        worker_count,
    )
    log_instagram(
        "Paralelismo de extracao configurado. "
        f"workers={worker_count}, checkpoint_interval={checkpoint_interval}"
    )

    def flush_checkpoint_if_needed(force=False):
        should_save = force or (
            progress_state["completed"] - progress_state["last_saved"] >= checkpoint_interval
        )
        if not should_save:
            return

        save_results(records, output_dir=output_dir)
        progress_state["last_saved"] = progress_state["completed"]
        log_instagram(
            "Checkpoint salvo durante o enriquecimento paralelo. "
            f"concluidos={progress_state['completed']}, resumo={summarize_record_counts(records)}"
        )

    def worker_loop(worker_context):
        local_driver = worker_context["driver"]
        local_headless_mode = worker_context["headless_mode"]
        local_profile_path = worker_context["profile_path"]
        worker_id = worker_context["worker_id"]

        while True:
            try:
                record = pending_records_queue.get_nowait()
            except Empty:
                break

            index = indexed_post_records[id(record)]
            result = enrich_single_media_record(
                record,
                index,
                len(post_records),
                local_driver,
                local_headless_mode,
                local_profile_path,
            )
            local_driver = result["driver"]
            local_headless_mode = result["headless_mode"]

            with state_lock:
                record.update(result["data"])
                record["status"] = result["status"]
                record["error"] = result["error"]
                progress_state["completed"] += 1
                flush_checkpoint_if_needed(force=result["status"] != 2)

            log_instagram(
                f"[worker {worker_id}] "
                f"Finalizado post {format_progress(index, len(post_records))}. "
                f"status={record['status']}, erro={record.get('error', '') or '-'}"
            )

        worker_context["driver"] = local_driver
        worker_context["headless_mode"] = local_headless_mode

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(worker_loop, context) for context in worker_contexts]
            for future in futures:
                future.result()

        with state_lock:
            flush_checkpoint_if_needed(force=True)

        primary_context = worker_contexts[0]
        returned_driver = primary_context["driver"]
        returned_headless_mode = primary_context["headless_mode"]
        cleanup_instagram_worker_contexts(worker_contexts, preserve_driver=returned_driver)
        return records, returned_driver, returned_headless_mode
    except Exception:
        cleanup_instagram_worker_contexts(
            worker_contexts,
            preserve_driver=worker_contexts[0]["driver"],
        )
        raise


def extract_instagram_results(page_source):
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
                "categoria": categorize_instagram_url(link),
                "status": 0,
                "error": "",
                "data": 0,
            }
        )

    return records


def is_instagram_logged_in(driver):
    return driver.get_cookie("sessionid") is not None or driver.get_cookie("ds_user_id") is not None


def wait_for_login_completion(driver, platform_name, login_check, poll_interval=5):
    log_instagram(f"Aguardando login no {platform_name}...")

    while not login_check(driver):
        time.sleep(poll_interval)

    log_instagram(f"Login no {platform_name} concluido.")


def is_google_captcha_page(driver):
    current_url = get_driver_current_url(driver).lower()
    if not current_url:
        return False

    parsed_url = urlparse(current_url)
    current_host = parsed_url.netloc.lower()
    current_path = parsed_url.path.lower()
    page_source = get_driver_page_source(driver).lower()
    page_title = get_driver_title(driver).lower()

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


def export_instagram_session_cookies(driver):
    try:
        current_host = urlparse(driver.current_url).netloc.lower()
    except Exception:
        current_host = ""

    if "instagram.com" not in current_host:
        driver.get("https://www.instagram.com/")

    allowed_keys = {"name", "value", "domain", "path", "expiry", "secure", "httpOnly", "sameSite"}
    exported_cookies = []

    for cookie in driver.get_cookies():
        cookie_domain = clean_text(cookie.get("domain", "")).lower()
        if "instagram.com" not in cookie_domain:
            continue

        exported_cookie = {
            key: value
            for key, value in cookie.items()
            if key in allowed_keys and value is not None
        }
        if exported_cookie.get("expiry") is not None:
            try:
                exported_cookie["expiry"] = int(exported_cookie["expiry"])
            except (TypeError, ValueError):
                exported_cookie.pop("expiry", None)

        exported_cookies.append(exported_cookie)

    return exported_cookies


def bootstrap_instagram_session(driver, instagram_cookies):
    driver.get("https://www.instagram.com/")

    for cookie in instagram_cookies:
        try:
            driver.add_cookie(cookie)
        except Exception:
            continue

    driver.get("https://www.instagram.com/")
    close_instagram_login_popup(driver, timeout=1)
    return driver


def create_instagram_worker_contexts(driver, headless_mode, profile_path, worker_count):
    worker_contexts = [
        {
            "worker_id": 1,
            "driver": driver,
            "headless_mode": headless_mode,
            "profile_path": profile_path,
            "temporary_profile_path": None,
        }
    ]

    if worker_count <= 1:
        return worker_contexts

    instagram_cookies = export_instagram_session_cookies(driver)
    RUNTIME_PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    for worker_id in range(2, worker_count + 1):
        temporary_profile_path = Path(
            tempfile.mkdtemp(
                prefix=f"insta_worker_{worker_id}_",
                dir=str(RUNTIME_PROFILES_DIR),
            )
        )
        worker_driver = create_driver(headless=headless_mode, profile_path=temporary_profile_path)
        bootstrap_instagram_session(worker_driver, instagram_cookies)
        worker_contexts.append(
            {
                "worker_id": worker_id,
                "driver": worker_driver,
                "headless_mode": headless_mode,
                "profile_path": temporary_profile_path,
                "temporary_profile_path": temporary_profile_path,
            }
        )

    return worker_contexts


def cleanup_instagram_worker_contexts(worker_contexts, preserve_driver=None):
    preserved_driver = preserve_driver or (worker_contexts[0]["driver"] if worker_contexts else None)

    for context in worker_contexts:
        driver = context.get("driver")
        temporary_profile_path = context.get("temporary_profile_path")

        if driver is not None and driver is not preserved_driver:
            try:
                driver.quit()
            except Exception:
                pass

        if temporary_profile_path:
            try:
                for child in sorted(temporary_profile_path.rglob("*"), reverse=True):
                    if child.is_file() or child.is_symlink():
                        child.unlink(missing_ok=True)
                    elif child.is_dir():
                        child.rmdir()
                temporary_profile_path.rmdir()
            except Exception:
                pass


def wait_for_captcha_resolution(driver, headless_mode, profile_path):
    captcha_message_shown = False

    while is_google_captcha_page(driver):
        if headless_mode:
            send_notification(
                "Captcha do Google",
                "Resolva o captcha do Google no navegador para a busca continuar.",
            )
            log_instagram("Captcha do Google detectado em headless. Abrindo navegador visivel para resolucao manual.")
            driver = reopen_driver(
                driver,
                get_driver_current_url(driver, "https://www.google.com/"),
                headless=False,
                profile_path=profile_path,
            )
            headless_mode = False
            captcha_message_shown = False
            continue

        if not captcha_message_shown:
            log_instagram("Captcha do Google detectado. Aguardando resolucao manual no navegador.")
            captcha_message_shown = True

        time.sleep(5)

    return driver, headless_mode


def wait_for_instagram_block_resolution(driver, headless_mode, profile_path):
    message_shown = False
    block = get_instagram_scrape_block(driver)

    while block and block["manual_resolution"]:
        if headless_mode:
            send_notification(
                "Bloqueio no Instagram",
                f"{block['detail']} Resolva no navegador para continuar.",
            )
            log_instagram(
                "Bloqueio manual detectado em headless. "
                f"tipo={block['type']}, erro={block['error']}. Abrindo navegador visivel."
            )
            driver = reopen_driver(
                driver,
                get_driver_current_url(driver, "https://www.instagram.com/"),
                headless=False,
                profile_path=profile_path,
            )
            headless_mode = False
            message_shown = False
            block = get_instagram_scrape_block(driver)
            continue

        if not message_shown:
            log_instagram(
                "Aguardando resolucao manual no Instagram. "
                f"tipo={block['type']}, erro={block['error']}, detalhe={block['detail']}, "
                f"url={get_driver_current_url(driver)}"
            )
            message_shown = True

        time.sleep(5)
        close_instagram_login_popup(driver, timeout=1)
        block = get_instagram_scrape_block(driver)

    return driver, headless_mode


def restore_headless_mode(driver, headless_mode, profile_path, timeout=15):
    if headless_mode:
        return driver, headless_mode

    log_instagram("Captcha resolvido. Retornando ao modo headless.")
    driver = reopen_driver(
        driver,
        get_driver_current_url(driver, "https://www.google.com/"),
        headless=True,
        profile_path=profile_path,
    )
    headless_mode = True

    try:
        WebDriverWait(driver, timeout).until(
            lambda current_driver: extract_instagram_results(current_driver.page_source)
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
            lambda current_driver: extract_instagram_results(current_driver.page_source)
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

    current_url = urlparse(get_driver_current_url(driver))
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


def InstaMain(search_term="", after=None, before=None, output_dir=None):
    google_query = build_google_query(search_term, after=after, before=before)
    url = f"https://www.google.com/search?q={quote_plus(google_query)}"
    collected_records = []
    seen_links = set()
    visited_pages = set()
    page_number = 0
    headless_mode = True
    runtime_profile_path = get_runtime_profile_path()
    driver = create_driver(headless=headless_mode, profile_path=runtime_profile_path)

    try:
        log_instagram(
            "Iniciando busca no Instagram via Google. "
            f"termo={search_term or '<vazio>'}, after={after or '-'}, before={before or '-'}"
        )
        driver.get(url)

        while True:
            try:
                driver, headless_mode = wait_for_results_page(
                    driver,
                    headless_mode,
                    runtime_profile_path,
                )
            except WebDriverException as exc:
                if is_webdriver_window_lost_error(exc):
                    fallback_url = get_driver_current_url(driver, url)
                    driver, headless_mode = recover_instagram_driver(
                        driver,
                        fallback_url,
                        headless_mode,
                        runtime_profile_path,
                    )
                    continue
                raise

            current_page_url = get_driver_current_url(driver)
            if current_page_url in visited_pages:
                log_instagram("Pagina ja visitada detectada. Encerrando paginacao.")
                break

            visited_pages.add(current_page_url)
            page_number += 1
            log_instagram(
                f"[paginas {page_number}] Processando resultados: {current_page_url}"
            )

            page_records = extract_instagram_results(get_driver_page_source(driver))
            new_records_count = 0

            for record in page_records:
                if record["fonte"] in seen_links:
                    continue

                seen_links.add(record["fonte"])
                collected_records.append(record)
                new_records_count += 1

            save_results(collected_records, output_dir=output_dir)
            log_instagram(
                f"[paginas {page_number}] "
                f"links_encontrados={len(page_records)}, novos={new_records_count}, "
                f"total_acumulado={len(collected_records)}, resumo={summarize_record_counts(collected_records)}"
            )

            next_page_url = get_next_page_url(driver)
            if not next_page_url or next_page_url in visited_pages:
                log_instagram("Nao ha proxima pagina nova. Encerrando coleta de resultados.")
                break

            log_instagram(f"[paginas {page_number}] Indo para a proxima pagina do Google.")
            driver.get(next_page_url)

        collected_records, driver, headless_mode = enrich_post_records(
            collected_records,
            driver,
            headless_mode,
            runtime_profile_path,
            output_dir=output_dir,
        )
    finally:
        saved_json_path, saved_xlsx_path = save_results(collected_records, output_dir=output_dir)
        try:
            driver.quit()
        except Exception:
            pass

    log_instagram(
        "Execucao concluida. "
        f"paginas={page_number}, total_registros={len(collected_records)}, "
        f"resumo={summarize_record_counts(collected_records)}"
    )
    print(json.dumps(build_categorized_output(collected_records), ensure_ascii=False, indent=2))
    log_instagram(f"Arquivos salvos em: {saved_json_path} e {saved_xlsx_path}")
    return collected_records


def InstaReview(review_path):
    collected_records, loaded_json_path, output_dir = load_instagram_records_for_review(review_path)
    runtime_profile_path = None
    driver = None
    headless_mode = True
    pending_records = [record for record in collected_records if record.get("status") != 2]
    pending_post_records = [
        record
        for record in collected_records
        if record.get("categoria") in {"post", "reel"} and record.get("status") != 2
    ]

    log_instagram(
        "Iniciando review do Instagram. "
        f"arquivo={loaded_json_path}, total_registros={len(collected_records)}, "
        f"pendentes={len(pending_records)}, midias_pendentes={len(pending_post_records)}"
    )

    try:
        if pending_post_records:
            runtime_profile_path = get_runtime_profile_path()
            driver = create_driver(headless=headless_mode, profile_path=runtime_profile_path)
            collected_records, driver, headless_mode = enrich_post_records(
                collected_records,
                driver,
                headless_mode,
                runtime_profile_path,
                output_dir=output_dir,
            )
        else:
            log_instagram("Nenhum post/reel pendente com status != 2 para reprocessar no review.")
    finally:
        saved_json_path, saved_xlsx_path = save_results(collected_records, output_dir=output_dir)
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    log_instagram(
        "Review concluida. "
        f"total_registros={len(collected_records)}, resumo={summarize_record_counts(collected_records)}"
    )
    print(json.dumps(build_categorized_output(collected_records), ensure_ascii=False, indent=2))
    log_instagram(f"Arquivos salvos em: {saved_json_path} e {saved_xlsx_path}")
    return collected_records


def InstaLogin():
    driver = create_driver(headless=False, profile_path=get_chrome_profile_path())

    try:
        driver.get("https://www.instagram.com/accounts/login/")
        wait_for_login_completion(driver, "Instagram", is_instagram_logged_in)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


