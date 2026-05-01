import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse
import zipfile

import dotenv
from bs4 import BeautifulSoup
from notifier import send_notification
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

APP_NAME = "ScrapyInfoPolitica"


def get_app_data_dir():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME


def get_app_root_dir():
    if getattr(sys, "frozen", False):
        app_data_dir = get_app_data_dir()
        app_data_dir.mkdir(parents=True, exist_ok=True)
        return app_data_dir

    return Path(__file__).resolve().parents[1]


APP_ROOT_DIR = get_app_root_dir()
ENV_FILE = APP_ROOT_DIR / ".env"
DEFAULT_CHROME_PROFILE = APP_ROOT_DIR / "face" / "profile"
OUTPUT_ROOT_DIR = APP_ROOT_DIR.parent / "output" if not getattr(sys, "frozen", False) else APP_ROOT_DIR / "output"
OUTPUT_SUBDIR_NAME = "face"
FACEBOOK_CATEGORY_ORDER = [
    "Perfil",
    "Videos",
    "Reels",
    "Posts",
    "Stories",
    "Fotos",
    "Permalinks",
]
FACEBOOK_CONTENT_CATEGORIES = set(FACEBOOK_CATEGORY_ORDER) - {"Perfil"}
FACEBOOK_PROFILE_HEADERS = [
    "fonte",
    "status",
    "error",
    "data",
    "perfil",
    "verificado",
]
FACEBOOK_PUBLICATION_HEADERS = [
    "fonte",
    "status",
    "error",
    "data",
    "perfil",
    "verificado",
    "numero_comentarios",
    "descricao_video",
    "numero_visualizacoes",
    "numero_reacoes",
]
FACEBOOK_PROFILE_PATH_EXCLUSIONS = {
    "watch",
    "reel",
    "reels",
    "story.php",
    "permalink.php",
    "photo.php",
    "photos",
    "photo",
}

dotenv.load_dotenv(ENV_FILE)


def log_facebook(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[facebook {timestamp}] {message}", flush=True)


def summarize_facebook_record_counts(records):
    counts = {category: 0 for category in FACEBOOK_CATEGORY_ORDER}

    for record in records:
        category = record.get("categoria", "Perfil")
        counts[category] = counts.get(category, 0) + 1

    return ", ".join(f"{category}={count}" for category, count in counts.items() if count)


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


def get_headers_for_category(category):
    if category in FACEBOOK_CONTENT_CATEGORIES:
        return FACEBOOK_PUBLICATION_HEADERS

    return FACEBOOK_PROFILE_HEADERS


def build_categorized_output(records):
    categorized_records = {category: [] for category in FACEBOOK_CATEGORY_ORDER}

    for record in records:
        category = record.get("categoria", "Perfil")
        if category not in categorized_records:
            category = "Perfil"

        categorized_records[category].append(record)

    return categorized_records


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
    row_data = [headers, *[[record.get(header, "") for header in headers] for record in records]]

    for row_index, row_values in enumerate(row_data, start=1):
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


def write_facebook_workbook(path, categorized_records):
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

    for sheet_index, category in enumerate(FACEBOOK_CATEGORY_ORDER, start=1):
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
        workbook_file.writestr("[Content_Types].xml", "".join(content_types_xml))
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

        for sheet_index, category in enumerate(FACEBOOK_CATEGORY_ORDER, start=1):
            workbook_file.writestr(
                f"xl/worksheets/sheet{sheet_index}.xml",
                build_excel_sheet_xml(
                    categorized_records.get(category, []),
                    get_headers_for_category(category),
                ),
            )


def save_results(records, output_dir=None):
    execution_output_dir = get_execution_output_dir(output_dir)
    execution_output_dir.mkdir(parents=True, exist_ok=True)
    json_output_path = resolve_writable_output_path(execution_output_dir / "facebook.json")
    xlsx_output_path = resolve_writable_output_path(execution_output_dir / "facebook.xlsx")
    categorized_records = build_categorized_output(records)

    json_output_path.write_text(
        json.dumps(categorized_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    write_facebook_workbook(xlsx_output_path, categorized_records)

    return json_output_path, xlsx_output_path


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


def classify_facebook_url(url):
    parsed_url = urlparse(url)
    path = parsed_url.path.lower()
    path_parts = [part for part in path.split("/") if part]
    query = parse_qs(parsed_url.query)

    if len(path_parts) >= 2 and path_parts[1] == "posts":
        return "Posts"

    if "/posts/" in path:
        return "Posts"

    if "/story.php" in path or "story_fbid" in query:
        return "Stories"

    if "/reel/" in path or "/reels/" in path:
        return "Reels"

    if "/photo/" in path or "/photos/" in path or "/photo.php" in path:
        return "Fotos"

    if "fbid" in query:
        return "Fotos"

    if "/permalink.php" in path:
        return "Permalinks"

    if "/videos/" in path or "/watch/" in path or "v" in query:
        return "Videos"

    return "Perfil"


def get_profile_name_from_url(url):
    parsed_url = urlparse(url)
    path_parts = [part for part in parsed_url.path.split("/") if part]

    if path_parts and path_parts[0].lower() not in FACEBOOK_PROFILE_PATH_EXCLUSIONS:
        return path_parts[0]

    return ""


def get_facebook_content_id_from_url(url):
    parsed_url = urlparse(url)
    path_parts = [part for part in parsed_url.path.split("/") if part]

    for index, path_part in enumerate(path_parts):
        if path_part.lower() in {"reel", "reels", "videos", "watch", "posts", "photos", "photo"} and index + 1 < len(path_parts):
            if path_parts[index + 1].isdigit():
                return path_parts[index + 1]

        if path_part.isdigit():
            return path_part

    query = parse_qs(parsed_url.query)
    for key in ("v", "fbid", "story_fbid"):
        values = query.get(key)
        if values:
            return values[0]

    return ""


def extract_facebook_results(page_source):
    soup = BeautifulSoup(page_source, "html.parser")
    records = []
    seen_links = set()

    for anchor in soup.select("a[href]"):
        link = normalize_google_result_url(anchor.get("href", ""))

        if not link or link in seen_links:
            continue

        seen_links.add(link)
        category = classify_facebook_url(link)
        records.append(
            {
                "fonte": link,
                "categoria": category,
                "status": 0,
                "error": "",
                "data": "",
                "perfil": get_profile_name_from_url(link),
                "verificado": "",
            }
        )

    return records


def iter_json_objects_from_scripts(page_source):
    soup = BeautifulSoup(page_source, "html.parser")

    for script in soup.select('script[type="application/json"], script[type="application/ld+json"]'):
        script_content = script.get_text(strip=True)

        if not script_content:
            continue

        try:
            loaded_json = json.loads(script_content)
        except json.JSONDecodeError:
            continue

        yield loaded_json


def walk_json_objects(value):
    if isinstance(value, dict):
        yield value
        for child_value in value.values():
            yield from walk_json_objects(child_value)
    elif isinstance(value, list):
        for item in value:
            yield from walk_json_objects(item)


def get_text_value(value):
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, dict):
        text = value.get("text") or value.get("__html")
        if isinstance(text, str):
            return text.strip()

    return ""


def get_nested_value(value, path):
    current_value = value

    for key in path:
        if not isinstance(current_value, dict):
            return None

        current_value = current_value.get(key)

    return current_value


def first_filled(*values):
    for value in values:
        if value not in (None, ""):
            return value

    return ""


def parse_count(value):
    if value in (None, ""):
        return ""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)

    normalized_value = str(value).strip().lower().replace("\xa0", " ")
    multiplier = 1

    if "mil" in normalized_value or normalized_value.endswith("k"):
        multiplier = 1000
    elif "mi" in normalized_value or "m" in normalized_value:
        multiplier = 1000000

    number_match = re.search(r"\d+(?:[.,]\d+)?", normalized_value)
    if not number_match:
        return ""

    number_text = number_match.group(0)
    if "," in number_text and "." in number_text:
        number_text = number_text.replace(".", "").replace(",", ".")
    elif "," in number_text:
        number_text = number_text.replace(",", ".")

    try:
        return int(float(number_text) * multiplier)
    except ValueError:
        return ""


def format_timestamp(value):
    if value in (None, ""):
        return ""

    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return str(value)

    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def is_facebook_publication_data(data):
    if not isinstance(data, dict):
        return False

    feedback = data.get("feedback")
    return bool(
        isinstance(feedback, dict)
        and (
            "reaction_count" in feedback
            or "total_comment_count" in feedback
            or "comment_rendering_instance" in feedback
            or "comment_rendering_instance_for_feed_location" in feedback
            or "comments_count_summary_renderer" in feedback
            or "video_view_count" in feedback
            or "video_view_count_renderer" in feedback
        )
    )


def json_object_contains_value(value, expected_value):
    if not expected_value:
        return True

    if isinstance(value, dict):
        return any(json_object_contains_value(child_value, expected_value) for child_value in value.values())

    if isinstance(value, list):
        return any(json_object_contains_value(item, expected_value) for item in value)

    return str(value) == expected_value or expected_value in str(value)


def extract_actor_from_story(story):
    actors = story.get("actors") if isinstance(story, dict) else None

    if isinstance(actors, list) and actors:
        return actors[0]

    return {}


def extract_actor_from_comet_sections(comet_sections):
    if not isinstance(comet_sections, dict):
        return {}

    for section_name in ("title", "actor_photo", "metadata", "message"):
        section = comet_sections.get(section_name)
        section_items = section if isinstance(section, list) else [section]

        for section_item in section_items:
            story = section_item.get("story") if isinstance(section_item, dict) else None
            actor = extract_actor_from_story(story)

            if actor.get("name") or actor.get("is_verified") not in (None, ""):
                return actor

    return {}


def extract_verified_from_comet_sections(comet_sections):
    if not isinstance(comet_sections, dict):
        return ""

    for section_name in ("title", "actor_photo", "metadata", "message"):
        section = comet_sections.get(section_name)
        section_items = section if isinstance(section, list) else [section]

        for section_item in section_items:
            story = section_item.get("story") if isinstance(section_item, dict) else None
            actor = extract_actor_from_story(story)
            if actor.get("is_verified") not in (None, ""):
                return actor.get("is_verified")

            story_sections = story.get("comet_sections") if isinstance(story, dict) else None
            if isinstance(story_sections, dict) and story_sections.get("badge"):
                return True

    return ""


def extract_story_from_comet_sections(comet_sections):
    if not isinstance(comet_sections, dict):
        return {}

    for section_name in ("metadata", "title", "actor_photo", "message"):
        section = comet_sections.get(section_name)
        if isinstance(section, list):
            section_items = section
        else:
            section_items = [section]

        for section_item in section_items:
            if isinstance(section_item, dict) and isinstance(section_item.get("story"), dict):
                return section_item["story"]

    return {}


def extract_message_from_comet_sections(comet_sections):
    message_section = comet_sections.get("message") if isinstance(comet_sections, dict) else None
    story = message_section.get("story") if isinstance(message_section, dict) else None
    message = story.get("message") if isinstance(story, dict) else None
    return get_text_value(message)


def is_facebook_login_required_page(page_source, current_url=""):
    parsed_url = urlparse(current_url or "")
    if parsed_url.path.lower().rstrip("/") == "/login":
        return True

    soup = BeautifulSoup(page_source, "html.parser")
    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical:
        canonical_url = urlparse(canonical.get("href", ""))
        if canonical_url.path.lower().rstrip("/") == "/login":
            return True

    page_text = soup.get_text(" ", strip=True).lower()
    has_login_form = bool(
        soup.select_one('input[name="email"], input[type="email"], input[name="pass"], input[type="password"]')
    )
    has_login_preloader = "CAAFBLoginHomepageRootQueryRelayPreloader" in page_source
    has_login_copy = (
        "entrar no facebook" in page_text
        or "entre no facebook" in page_text
        or "log in to facebook" in page_text
    )

    return has_login_form and (has_login_preloader or has_login_copy)


def merge_publication_data(record, data):
    feedback = data.get("feedback") if isinstance(data.get("feedback"), dict) else {}
    feedback_owner = feedback.get("owning_profile") if isinstance(feedback.get("owning_profile"), dict) else {}
    reaction_count = feedback.get("reaction_count")
    if isinstance(reaction_count, dict):
        reaction_count = reaction_count.get("count")

    comment_rendering_comments = get_nested_value(
        feedback,
        ("comment_rendering_instance", "comments", "total_count"),
    )
    feed_location_comments = get_nested_value(
        feedback,
        ("comment_rendering_instance_for_feed_location", "comments", "total_count"),
    )
    comments_summary = get_nested_value(
        feedback,
        ("comments_count_summary_renderer", "feedback", "comment_rendering_instance", "comments", "total_count"),
    )
    video_view_feedback = get_nested_value(
        feedback,
        ("video_view_count_renderer", "feedback"),
    )
    if not isinstance(video_view_feedback, dict):
        video_view_feedback = {}

    comet_sections = data.get("comet_sections")
    story = extract_story_from_comet_sections(comet_sections)
    actor = extract_actor_from_story(story)
    direct_actor = extract_actor_from_story(data)
    if direct_actor:
        actor = {**actor, **direct_actor}
    section_actor = extract_actor_from_comet_sections(comet_sections)
    if section_actor:
        actor = {**actor, **section_actor}
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    title = get_text_value(data.get("title"))

    record["data"] = first_filled(
        record.get("data"),
        format_timestamp(story.get("creation_time") if isinstance(story, dict) else None),
        format_timestamp(data.get("creation_time")),
        format_timestamp(data.get("created_time")),
    )
    record["perfil"] = first_filled(
        actor.get("name") if isinstance(actor, dict) else "",
        owner.get("name"),
        feedback_owner.get("name"),
        record.get("perfil"),
    )
    record["verificado"] = first_filled(
        record.get("verificado"),
        actor.get("is_verified") if isinstance(actor, dict) else "",
        owner.get("is_verified"),
        extract_verified_from_comet_sections(comet_sections),
        "badge" in story.get("comet_sections", {}) if isinstance(story.get("comet_sections"), dict) else "",
    )
    record["numero_comentarios"] = first_filled(
        record.get("numero_comentarios"),
        parse_count(feedback.get("total_comment_count")),
        parse_count(comment_rendering_comments),
        parse_count(feed_location_comments),
        parse_count(comments_summary),
        parse_count(feedback.get("comment_count_reduced")),
    )
    record["descricao_video"] = first_filled(
        record.get("descricao_video"),
        extract_message_from_comet_sections(comet_sections),
        title,
        data.get("name"),
    )
    record["numero_visualizacoes"] = first_filled(
        record.get("numero_visualizacoes"),
        parse_count(video_view_feedback.get("video_view_count")),
        parse_count(video_view_feedback.get("video_view_count_reduced")),
        parse_count(feedback.get("video_view_count")),
        parse_count(feedback.get("video_view_count_reduced")),
        parse_count(feedback.get("play_count")),
        parse_count(feedback.get("play_count_reduced")),
        parse_count(data.get("video_view_count")),
    )
    record["numero_reacoes"] = first_filled(
        record.get("numero_reacoes"),
        parse_count(reaction_count),
        parse_count(feedback.get("i18n_reaction_count")),
    )


def extract_facebook_publication_data(page_source, source_url, category=None, current_url=""):
    if is_facebook_login_required_page(page_source, current_url=current_url):
        record = build_facebook_publication_record(source_url, category=category)
        record["status"] = 2
        record["error"] = "login_obrigatorio"
        return record

    content_id = get_facebook_content_id_from_url(source_url)

    def scan_publication_data(require_content_match=True, scanned_record=None):
        if scanned_record is None:
            scanned_record = build_facebook_publication_record(source_url, category=category)

        for json_object in iter_json_objects_from_scripts(page_source):
            for data in walk_json_objects(json_object):
                if require_content_match and content_id and not json_object_contains_value(data, content_id):
                    continue

                if is_facebook_publication_data(data) or isinstance(data.get("comet_sections"), dict):
                    merge_publication_data(scanned_record, data)

        return scanned_record

    record = scan_publication_data(require_content_match=True)

    if content_id and not has_complete_facebook_publication_details(record):
        relaxed_record = scan_publication_data(require_content_match=False)
        if count_facebook_publication_details(relaxed_record) > count_facebook_publication_details(record):
            record = relaxed_record
        else:
            record = scan_publication_data(require_content_match=False, scanned_record=record)

    if not has_facebook_publication_details(record):
        record["status"] = 2
        record["error"] = "dados_publicacao_nao_encontrados"

    return record


def has_facebook_publication_details(record):
    detail_fields = [
        "data",
        "numero_comentarios",
        "descricao_video",
        "numero_visualizacoes",
        "numero_reacoes",
    ]

    return any(record.get(field) not in ("", None) for field in detail_fields)


def has_complete_facebook_publication_details(record):
    detail_fields = [
        "data",
        "perfil",
        "numero_comentarios",
        "descricao_video",
        "numero_visualizacoes",
        "numero_reacoes",
    ]

    return all(record.get(field) not in ("", None) for field in detail_fields)


def count_facebook_publication_details(record):
    detail_fields = [
        "data",
        "perfil",
        "verificado",
        "numero_comentarios",
        "descricao_video",
        "numero_visualizacoes",
        "numero_reacoes",
    ]

    return sum(1 for field in detail_fields if record.get(field) not in ("", None))


def build_facebook_publication_record(source_url, category=None):
    return {
        "fonte": source_url,
        "categoria": category or classify_facebook_url(source_url),
        "status": 1,
        "error": "",
        "data": "",
        "perfil": get_profile_name_from_url(source_url),
        "verificado": "",
        "numero_comentarios": "",
        "descricao_video": "",
        "numero_visualizacoes": "",
        "numero_reacoes": "",
    }


def is_facebook_logged_in(driver):
    return driver.get_cookie("c_user") is not None or driver.get_cookie("xs") is not None


def wait_for_login_completion(driver, platform_name, login_check, poll_interval=5):
    print(f"Aguardando login no {platform_name}...")

    while not login_check(driver):
        time.sleep(poll_interval)

    print(f"Login no {platform_name} concluido.")


def close_facebook_login_popup(driver, timeout=3):
    end_time = time.time() + timeout
    close_xpaths = [
        "//div[@aria-label='Fechar' or @aria-label='Close']",
        "//div[@role='button' and (@aria-label='Fechar' or @aria-label='Close')]",
        "//span[normalize-space()='Agora não']/ancestor::*[@role='button'][1]",
        "//span[normalize-space()='Not now']/ancestor::*[@role='button'][1]",
        "//span[normalize-space()='Entrar depois']/ancestor::*[@role='button'][1]",
    ]

    while time.time() < end_time:
        for close_xpath in close_xpaths:
            try:
                close_buttons = driver.find_elements(By.XPATH, close_xpath)
            except WebDriverException:
                continue

            for close_button in close_buttons:
                try:
                    if not close_button.is_displayed():
                        continue

                    driver.execute_script("arguments[0].click();", close_button)
                    time.sleep(0.5)
                    return True
                except WebDriverException:
                    continue

        try:
            popup_closed = driver.execute_script(
                """
                const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
                for (const dialog of dialogs) {
                  const closeButton = dialog.querySelector('[aria-label="Fechar"], [aria-label="Close"]');
                  if (closeButton) {
                    closeButton.click();
                    return true;
                  }
                }
                return false;
                """
            )
            if popup_closed:
                time.sleep(0.5)
                return True
        except WebDriverException:
            pass

        time.sleep(0.5)

    return False


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


def enrich_facebook_publication_record(driver, record, timeout=15, progress_label=""):
    if progress_label:
        log_facebook(f"Extraindo dados, resultado {progress_label}: {record['fonte']}")

    driver.get(record["fonte"])

    try:
        WebDriverWait(driver, timeout).until(
            lambda current_driver: current_driver.find_elements(By.TAG_NAME, "body")
        )
    except TimeoutException:
        pass

    close_facebook_login_popup(driver)
    time.sleep(1)
    close_facebook_login_popup(driver, timeout=1)

    enriched_record = extract_facebook_publication_data(
        driver.page_source,
        record["fonte"],
        category=record.get("categoria"),
        current_url=driver.current_url,
    )
    for key, value in record.items():
        if enriched_record.get(key) in ("", None):
            enriched_record[key] = value

    if progress_label:
        log_facebook(
            f"Dados extraidos, resultado {progress_label}. "
            f"status={enriched_record.get('status')}, erro={enriched_record.get('error') or '-'}"
        )

    return enriched_record


def split_records_for_workers(records, worker_count):
    chunks = [[] for _ in range(worker_count)]

    for chunk_index, indexed_record in enumerate(records):
        chunks[chunk_index % worker_count].append(indexed_record)

    return [chunk for chunk in chunks if chunk]


def enrich_facebook_publication_records_chunk(indexed_records):
    profile_path = Path(tempfile.mkdtemp(prefix="face_worker_"))
    driver = create_driver(headless=True, profile_path=profile_path)
    enriched_records = []

    try:
        for record_index, record, progress_index, progress_total in indexed_records:
            progress_label = f"{progress_index}/{progress_total}"
            try:
                enriched_record = enrich_facebook_publication_record(
                    driver,
                    record,
                    progress_label=progress_label,
                )
            except Exception as exc:
                enriched_record = {**record, "status": 2, "error": str(exc)}
                log_facebook(
                    f"Falha ao extrair dados, resultado {progress_label}. "
                    f"erro={exc}"
                )

            enriched_records.append((record_index, enriched_record))
    finally:
        try:
            driver.quit()
        except Exception:
            pass

        shutil.rmtree(profile_path, ignore_errors=True)

    return enriched_records


def enrich_facebook_publication_records(driver, collected_records, workers=1, output_dir=None):
    publication_records_without_progress = [
        (record_index, record)
        for record_index, record in enumerate(collected_records)
        if record.get("categoria") in FACEBOOK_CONTENT_CATEGORIES
    ]
    total_publication_records = len(publication_records_without_progress)
    publication_records = [
        (record_index, record, progress_index, total_publication_records)
        for progress_index, (record_index, record) in enumerate(publication_records_without_progress, start=1)
    ]

    if not publication_records:
        log_facebook("Nenhum resultado de publicacao para extrair dados.")
        return collected_records

    worker_count = max(1, min(int(workers or 1), len(publication_records)))
    log_facebook(
        "Iniciando extracao de dados das publicacoes. "
        f"resultados={len(publication_records)}, workers={worker_count}"
    )

    if worker_count == 1:
        for record_index, record, progress_index, progress_total in publication_records:
            progress_label = f"{progress_index}/{progress_total}"
            try:
                collected_records[record_index] = enrich_facebook_publication_record(
                    driver,
                    record,
                    progress_label=progress_label,
                )
            except Exception as exc:
                record["status"] = 2
                record["error"] = str(exc)
                log_facebook(
                    f"Falha ao extrair dados, resultado {progress_label}. "
                    f"erro={exc}"
                )

            save_results(collected_records, output_dir=output_dir)

        return collected_records

    chunks = split_records_for_workers(publication_records, worker_count)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        completed_records = 0
        futures = [
            executor.submit(enrich_facebook_publication_records_chunk, chunk)
            for chunk in chunks
        ]

        for future in as_completed(futures):
            chunk_results = future.result()
            for record_index, enriched_record in chunk_results:
                collected_records[record_index] = enriched_record

            completed_records += len(chunk_results)
            log_facebook(
                "Progresso da extracao de dados. "
                f"concluidos={completed_records}/{len(publication_records)}"
            )
            save_results(collected_records, output_dir=output_dir)

    return collected_records


def FaceMain(search_term="", after=None, before=None, output_dir=None, workers=1):
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
        log_facebook(
            "Iniciando busca no Facebook via Google. "
            f"termo={search_term or '<vazio>'}, after={after or '-'}, before={before or '-'}, workers={workers}"
        )
        driver.get(url)

        while True:
            driver, headless_mode = wait_for_results_page(driver, headless_mode, runtime_profile_path)

            current_page_url = driver.current_url
            if current_page_url in visited_pages:
                log_facebook("Pagina ja visitada detectada. Encerrando paginacao.")
                break

            visited_pages.add(current_page_url)
            page_number += 1
            log_facebook(f"Extraindo resultados, pagina {page_number}: {current_page_url}")

            page_records = extract_facebook_results(driver.page_source)
            new_records_count = 0
            for record in page_records:
                if record["fonte"] in seen_links:
                    continue

                seen_links.add(record["fonte"])
                collected_records.append(record)
                new_records_count += 1

            save_results(collected_records, output_dir=output_dir)
            log_facebook(
                f"Resultados extraidos, pagina {page_number}. "
                f"links_encontrados={len(page_records)}, novos={new_records_count}, "
                f"total_acumulado={len(collected_records)}, resumo={summarize_facebook_record_counts(collected_records)}"
            )

            next_page_url = get_next_page_url(driver)
            if not next_page_url or next_page_url in visited_pages:
                log_facebook("Nao ha proxima pagina nova. Encerrando coleta de resultados.")
                break

            log_facebook(f"Indo para a proxima pagina do Google apos pagina {page_number}.")
            driver.get(next_page_url)

        enrich_facebook_publication_records(
            driver,
            collected_records,
            workers=workers,
            output_dir=output_dir,
        )
    finally:
        saved_json_path, saved_xlsx_path = save_results(collected_records, output_dir=output_dir)
        try:
            driver.quit()
        except Exception:
            pass

    log_facebook(
        "Execucao concluida. "
        f"paginas={page_number}, total_registros={len(collected_records)}, "
        f"resumo={summarize_facebook_record_counts(collected_records)}"
    )
    print(json.dumps(build_categorized_output(collected_records), ensure_ascii=False, indent=2), flush=True)
    log_facebook(f"Arquivos salvos em: {saved_json_path} e {saved_xlsx_path}")
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
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers deve ser maior ou igual a 1.")

    return args


if __name__ == "__main__":
    args = parse_args()
    FaceMain(args.termo, after=args.after, before=args.before, workers=args.workers)
