"""Read RSS/Atom feeds and save new items as drafts in SQLite."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


PROJECT_DIR = Path(__file__).resolve().parent
FEEDS_FILE = PROJECT_DIR / "feeds.txt"
EXCLUDED_TITLES_FILE = PROJECT_DIR / "excluded_titles.txt"
FORUM_SECTIONS_FILE = PROJECT_DIR / "forum_sections.json"
DATABASE_FILE = PROJECT_DIR / "sanatkarbot.db"
ALLOWED_FORUM_SECTION_IDS = set(range(4, 55)) | {59}
FORUM_BASE_URL_ENV = "SANATKARBOT_FORUM_BASE_URL"
FORUM_USERNAME_ENV = "SANATKARBOT_FORUM_USERNAME"
FORUM_PASSWORD_ENV = "SANATKARBOT_FORUM_PASSWORD"


class _TextExtractor(HTMLParser):
    """Collect readable text from an HTML feed summary."""

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def clean_summary(value):
    """Remove HTML tags from a summary so it is readable in the database."""
    if not value:
        return ""

    extractor = _TextExtractor()
    extractor.feed(value)
    return " ".join(" ".join(extractor.parts).split())


def local_name(tag):
    """Return an XML tag name without its namespace."""
    return tag.rsplit("}", 1)[-1].lower()


def child_text(element, names):
    """Find the text of the first direct child whose name is in names."""
    for child in element:
        if local_name(child.tag) in names:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def entry_link(element):
    """Read an RSS link or an Atom alternate link."""
    for child in element:
        if local_name(child.tag) != "link":
            continue

        href = child.attrib.get("href", "").strip()
        relation = child.attrib.get("rel", "alternate").lower()
        if href and relation == "alternate":
            return href

        text = (child.text or "").strip()
        if text:
            return text

    return ""


def parse_feed(xml_data):
    """Parse RSS items or Atom entries into simple dictionaries."""
    root = ET.fromstring(xml_data)
    items = []

    for element in root.iter():
        if local_name(element.tag) not in {"item", "entry"}:
            continue

        title = child_text(element, {"title"}) or "Untitled item"
        link = entry_link(element)
        guid = child_text(element, {"guid", "id"})
        summary = child_text(
            element, {"description", "summary", "content", "encoded"}
        )
        published = child_text(
            element, {"pubdate", "published", "updated", "date"}
        )

        items.append(
            {
                "title": title,
                "link": link,
                "guid": guid,
                "summary": clean_summary(summary),
                "published": published,
            }
        )

    return items


def item_key(item, feed_url):
    """Make a stable key used by SQLite to prevent duplicate drafts."""
    if item["link"]:
        return "link:" + item["link"]

    if item["guid"]:
        return "guid:" + feed_url + ":" + item["guid"]

    fallback = "|".join(
        (feed_url, item["title"], item["published"], item["summary"])
    )
    return "hash:" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def read_feed_urls():
    """Load feed URLs, ignoring blank lines and comments."""
    if not FEEDS_FILE.exists():
        raise FileNotFoundError(f"Feed list not found: {FEEDS_FILE}")

    urls = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if url and not url.startswith("#"):
            urls.append(url)
    return urls


def read_excluded_titles():
    """Load exact titles to exclude, ignoring blank lines and comments."""
    if not EXCLUDED_TITLES_FILE.exists():
        raise FileNotFoundError(
            f"Excluded titles file not found: {EXCLUDED_TITLES_FILE}"
        )

    titles = set()
    for line in EXCLUDED_TITLES_FILE.read_text(encoding="utf-8").splitlines():
        title = line.strip()
        if title and not title.startswith("#"):
            titles.add(title)
    return titles


def read_forum_sections():
    """Load and validate section IDs and matching keywords from JSON."""
    if not FORUM_SECTIONS_FILE.exists():
        raise FileNotFoundError(f"Forum sections file not found: {FORUM_SECTIONS_FILE}")

    try:
        config = json.loads(FORUM_SECTIONS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in {FORUM_SECTIONS_FILE}: {error}") from error

    if not isinstance(config, dict) or not isinstance(config.get("sections"), list):
        raise ValueError("forum_sections.json must contain a 'sections' list.")

    sections = []
    section_ids = set()

    for position, section in enumerate(config["sections"], start=1):
        if not isinstance(section, dict):
            raise ValueError(f"Section {position} must be a JSON object.")

        section_id = section.get("id")
        if (
            isinstance(section_id, bool)
            or not isinstance(section_id, int)
            or section_id < 1
        ):
            raise ValueError(f"Section {position} needs a positive numeric 'id'.")
        if section_id not in ALLOWED_FORUM_SECTION_IDS:
            raise ValueError(
                f"Section ID {section_id} is excluded or is not in the "
                "allowed Sanatkar forum list."
            )
        if section_id in section_ids:
            raise ValueError(f"Section ID {section_id} is listed more than once.")

        name = section.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Section {section_id} needs a non-empty 'name'.")

        keywords = section.get("keywords")
        if (
            not isinstance(keywords, list)
            or not keywords
            or any(not isinstance(word, str) or not word.strip() for word in keywords)
        ):
            raise ValueError(
                f"Section {section_id} needs a 'keywords' list of non-empty strings."
            )

        section_ids.add(section_id)
        sections.append(
            {
                "id": section_id,
                "name": name.strip(),
                "keywords": [word.strip() for word in keywords],
            }
        )

    return sections


def normalize_match_text(value):
    """Normalize Turkish dotted and dotless I for case-insensitive matching."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return normalized.replace("i\u0307", "i").replace("ı", "i")


def contains_keyword(text, keyword):
    """Match a whole word or phrase, not a partial word."""
    pattern = rf"(?<!\w){re.escape(keyword)}(?!\w)"
    return re.search(pattern, text) is not None


def match_forum_section(title, description, sections):
    """Return one unique best section, or mark the item ambiguous/unmatched."""
    normalized_title = normalize_match_text(title)
    normalized_description = normalize_match_text(description or "")
    scores = []

    for section in sections:
        score = 0
        for word in section["keywords"]:
            keyword = normalize_match_text(word)
            if contains_keyword(normalized_title, keyword):
                score += 2
            if contains_keyword(normalized_description, keyword):
                score += 1
        scores.append((section["id"], score))

    highest_score = max((score for _, score in scores), default=0)
    if highest_score == 0:
        return None, "unmatched"

    winning_ids = [
        section_id for section_id, score in scores if score == highest_score
    ]
    if len(winning_ids) != 1:
        return None, "ambiguous"

    return winning_ids[0], "matched"


def initialize_database(connection):
    """Create the drafts table if it does not already exist."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_key TEXT NOT NULL UNIQUE,
            source_feed TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT,
            summary TEXT,
            published_at TEXT,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            target_section_id INTEGER,
            section_match_status TEXT NOT NULL DEFAULT 'unmatched'
        )
        """
    )

    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(drafts)").fetchall()
    }
    if "target_section_id" not in columns:
        connection.execute("ALTER TABLE drafts ADD COLUMN target_section_id INTEGER")
    if "section_match_status" not in columns:
        connection.execute(
            "ALTER TABLE drafts ADD COLUMN "
            "section_match_status TEXT NOT NULL DEFAULT 'unmatched'"
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS forum_publications (
            item_key TEXT PRIMARY KEY,
            draft_id INTEGER NOT NULL UNIQUE,
            title_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL
                CHECK (state IN ('submitting', 'published', 'uncertain')),
            thread_url TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()


def refresh_section_matches(connection, sections):
    """Update section matches for saved drafts using their stored text."""
    drafts = connection.execute(
        "SELECT id, title, summary FROM drafts WHERE status = 'draft'"
    ).fetchall()
    counts = {"matched": 0, "ambiguous": 0, "unmatched": 0}

    for draft_id, title, description in drafts:
        section_id, match_status = match_forum_section(title, description, sections)
        connection.execute(
            """
            UPDATE drafts
            SET target_section_id = ?, section_match_status = ?
            WHERE id = ?
            """,
            (section_id, match_status, draft_id),
        )
        counts[match_status] += 1

    connection.commit()
    return counts


def fetch_feed(feed_url):
    """Download and parse one RSS or Atom feed."""
    parsed_url = urlparse(feed_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("feed URL must start with http:// or https://")

    request = urllib.request.Request(
        feed_url,
        headers={"User-Agent": "SanatkarBot/1.0 RSS reader"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return parse_feed(response.read())


def save_item(connection, item, feed_url):
    """Save an item as a draft, returning True only if it is new."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO drafts (
            item_key, source_feed, title, link, summary,
            published_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_key(item, feed_url),
            feed_url,
            item["title"],
            item["link"],
            item["summary"],
            item["published"],
            now,
        ),
    )
    return cursor.rowcount == 1


def fetch_all_feeds():
    """Read each configured feed and save newly discovered items."""
    try:
        feed_urls = read_feed_urls()
        excluded_titles = read_excluded_titles()
        forum_sections = read_forum_sections()
    except (FileNotFoundError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if not feed_urls:
        print("No feed URLs found. Add RSS or Atom URLs to feeds.txt.")
        return 0

    new_count = 0
    duplicate_count = 0
    excluded_count = 0
    error_count = 0

    with sqlite3.connect(DATABASE_FILE) as connection:
        initialize_database(connection)

        for feed_url in feed_urls:
            try:
                items = fetch_feed(feed_url)
                feed_new_count = 0
                feed_duplicate_count = 0
                feed_excluded_count = 0

                for item in items:
                    if item["title"] in excluded_titles:
                        excluded_count += 1
                        feed_excluded_count += 1
                        continue

                    if save_item(connection, item, feed_url):
                        new_count += 1
                        feed_new_count += 1
                    else:
                        duplicate_count += 1
                        feed_duplicate_count += 1

                print(
                    f"{feed_url}: {feed_new_count} new draft(s), "
                    f"{feed_duplicate_count} already saved, "
                    f"{feed_excluded_count} excluded by title."
                )
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                ET.ParseError,
                ValueError,
            ) as error:
                error_count += 1
                print(f"Could not read {feed_url}: {error}", file=sys.stderr)

        section_counts = refresh_section_matches(connection, forum_sections)

    print(
        f"Done: {new_count} new draft(s), "
        f"{duplicate_count} duplicate item(s) skipped, "
        f"{excluded_count} item(s) excluded by title."
    )
    print(
        "Section matching: "
        f"{section_counts['matched']} matched, "
        f"{section_counts['ambiguous']} ambiguous, "
        f"{section_counts['unmatched']} unmatched; all remain drafts."
    )
    
    # Otomatik Taslakları Yayınlama Adımı
    try:
        auto_publish_matched_drafts()
    except Exception as err:
        print(f"Otomatik yayınlama sırasında hata: {err}", file=sys.stderr)

    return 1 if error_count else 0


def list_drafts(limit):
    """Show the most recently discovered drafts."""
    excluded_titles = read_excluded_titles()
    with sqlite3.connect(DATABASE_FILE) as connection:
        initialize_database(connection)
        query = """
            SELECT title, link, published_at, created_at,
                   target_section_id, section_match_status
            FROM drafts
            WHERE status = 'draft'
        """
        values = []
        if excluded_titles:
            placeholders = ", ".join("?" for _ in excluded_titles)
            query += f" AND title NOT IN ({placeholders})"
            values.extend(sorted(excluded_titles))
        query += " ORDER BY id DESC LIMIT ?"
        values.append(limit)
        drafts = connection.execute(query, values).fetchall()

    if not drafts:
        if excluded_titles:
            print("No visible drafts. Excluded titles remain saved in SQLite.")
        else:
            print("No drafts saved yet. Run `python main.py fetch` first.")
        return

    for title, link, published, created, section_id, match_status in drafts:
        print(f"\n{title}")
        print(f"  Link: {link or 'No link provided'}")
        print(f"  Published: {published or 'Unknown'}")
        print(f"  Saved: {created}")
        if match_status == "matched":
            print(f"  Target section ID: {section_id} (matched; still a draft)")
        elif match_status == "ambiguous":
            print("  Target section: ambiguous; remains a draft for manual review")
        else:
            print("  Target section: no match; remains a draft")


def publication_title_key(title):
    """Normalize an exact title for cross-feed duplicate protection."""
    normalized = unicodedata.normalize("NFKC", title).casefold()
    return " ".join(normalized.split())


def publication_already_reserved(connection, item_key, title):
    """Check whether this RSS item or exact normalized title was submitted."""
    row = connection.execute(
        """
        SELECT 1
        FROM forum_publications
        WHERE item_key = ? OR title_key = ?
        LIMIT 1
        """,
        (item_key, publication_title_key(title)),
    ).fetchone()
    return row is not None


def reserve_forum_publication(connection, draft_id, item_key, title):
    """Record a unique submission attempt before sending a forum POST."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        connection.execute(
            """
            INSERT INTO forum_publications (
                item_key, draft_id, title_key, state, created_at, updated_at
            )
            VALUES (?, ?, ?, 'submitting', ?, ?)
            """,
            (item_key, draft_id, publication_title_key(title), now, now),
        )
        connection.commit()
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise ValueError(
            "This draft or a draft with the same title already has a "
            "forum submission record; it will not be sent again."
        ) from error


def update_forum_publication(connection, item_key, state, thread_url=None):
    """Save the result while leaving uncertain submissions blocked from retry."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    connection.execute(
        """
        UPDATE forum_publications
        SET state = ?, thread_url = ?, updated_at = ?
        WHERE item_key = ?
        """,
        (state, thread_url, now, item_key),
    )
    if state == "published":
        connection.execute(
            "UPDATE drafts SET status = 'published' WHERE item_key = ?",
            (item_key,),
        )
    connection.commit()


def check_forum_connection():
    """Check forum connection and login without posting."""
    from forum_client import ForumClientError, MyBBClient

    base_url = os.environ.get(
        FORUM_BASE_URL_ENV, "http://sanatkar.10tl.net"
    )
    try:
        client = MyBBClient(base_url)
        form_status = client.check_connection()
        print(
            "Forum bağlantısı ve MyBB giriş formu başarıyla doğrulandı. "
            "Hiçbir forum içeriği gönderilmedi."
        )
        username = os.environ.get(FORUM_USERNAME_ENV)
        password = os.environ.get(FORUM_PASSWORD_ENV)
        if username or password:
            if not username or not password:
                raise ForumClientError(
                    f"Replit Secrets kısmına hem {FORUM_USERNAME_ENV} "
                    f"hem de {FORUM_PASSWORD_ENV} bilgilerini ekleyin."
                )
            client.login(username, password)
            print(
                "Giriş testi başarılı! Oturum bellekte tutuldu, "
                "hiçbir konu oluşturulmadı."
            )
        else:
            print(
                f"Giriş denenmedi. Bot hesabı için {FORUM_USERNAME_ENV} ve "
                f"{FORUM_PASSWORD_ENV} değerlerini Replit Secrets bölümüne ekleyebilirsiniz."
            )
        return 0
    except ForumClientError as error:
        print(f"Forum bağlantı kontrolü durduruldu: {error}", file=sys.stderr)
        return 1


def publish_draft(draft_id):
    """Manually publish one matched draft after explicit CLI confirmation."""
    from forum_client import ForumClientError, MyBBClient

    try:
        sections = read_forum_sections()
        excluded_titles = read_excluded_titles()
        allowed_section_ids = {section["id"] for section in sections}
        with sqlite3.connect(DATABASE_FILE) as connection:
            initialize_database(connection)
            draft = connection.execute(
                """
                SELECT item_key, title, link, summary, status,
                       target_section_id, section_match_status
                FROM drafts
                WHERE id = ?
                """,
                (draft_id,),
            ).fetchone()
            if draft is None:
                raise ValueError(f"Draft {draft_id} was not found.")

            item_key_value, title, link, summary, status, section_id, match_status = draft
            if status != "draft":
                raise ValueError("Only a saved draft can be published.")
            if title in excluded_titles:
                raise ValueError(
                    "This title is in excluded_titles.txt and cannot be published."
                )
            if match_status != "matched" or section_id not in allowed_section_ids:
                raise ValueError(
                    "This draft has no verified, permitted target section."
                )
            if publication_already_reserved(connection, item_key_value, title):
                raise ValueError(
                    "This draft or an exact same-title draft already has a "
                    "forum submission record; it will not be sent again."
                )

        message = (summary or "").strip()
        if link:
            message = f"{message}\n\nKaynak: {link}".strip()
        if not message:
            raise ValueError("The draft has no content or source link to publish.")

        base_url = os.environ.get(
            FORUM_BASE_URL_ENV, "http://sanatkar.10tl.net"
        )
        client = MyBBClient(base_url)
        username = os.environ.get(FORUM_USERNAME_ENV)
        password = os.environ.get(FORUM_PASSWORD_ENV)
        if not username or not password:
            raise ForumClientError(
                f"Yayımlamadan önce Replit Secrets bölümüne {FORUM_USERNAME_ENV} "
                f"ve {FORUM_PASSWORD_ENV} ekleyin."
            )
        client.login(username, password)
        prepared = client.prepare_new_thread(section_id, title, message)

        with sqlite3.connect(DATABASE_FILE) as connection:
            initialize_database(connection)
            reserve_forum_publication(
                connection, draft_id, item_key_value, title
            )

        try:
            thread_url = client.submit_new_thread(prepared)
        except Exception:
            with sqlite3.connect(DATABASE_FILE) as connection:
                update_forum_publication(
                    connection, item_key_value, "uncertain"
                )
            raise

        with sqlite3.connect(DATABASE_FILE) as connection:
            update_forum_publication(
                connection, item_key_value, "published", thread_url
            )
        print(f"Forum confirmed the thread: {thread_url}")
        return 0
    except (ForumClientError, FileNotFoundError, OSError, ValueError, sqlite3.Error) as error:
        print(f"Draft was not published: {error}", file=sys.stderr)
        return 1


def auto_publish_matched_drafts():
    """Automatically publishes matching drafts if credentials are available."""
    from forum_client import ForumClientError, MyBBClient

    username = os.environ.get(FORUM_USERNAME_ENV)
    password = os.environ.get(FORUM_PASSWORD_ENV)
    if not username or not password:
        return  # Şifreler girilmemişse otomatik yayınlamayı atla, sadece taslak toplar

    sections = read_forum_sections()
    excluded_titles = read_excluded_titles()
    allowed_section_ids = {section["id"] for section in sections}

    with sqlite3.connect(DATABASE_FILE) as connection:
        initialize_database(connection)
        matched_drafts = connection.execute(
            """
            SELECT id, item_key, title, link, summary, target_section_id
            FROM drafts
            WHERE status = 'draft' AND section_match_status = 'matched'
            LIMIT 5
            """
        ).fetchall()

    if not matched_drafts:
        return

    base_url = os.environ.get(FORUM_BASE_URL_ENV, "http://sanatkar.10tl.net")
    client = MyBBClient(base_url)
    try:
        client.login(username, password)
    except Exception as e:
        print(f"Otomatik forum girişi başarısız oldu: {e}", file=sys.stderr)
        return

    for draft_id, item_key_value, title, link, summary, section_id in matched_drafts:
        if title in excluded_titles or section_id not in allowed_section_ids:
            continue
        
        with sqlite3.connect(DATABASE_FILE) as connection:
            if publication_already_reserved(connection, item_key_value, title):
                continue

        message = (summary or "").strip()
        if link:
            message = f"{message}\n\nKaynak: {link}".strip()
        if not message:
            continue

        try:
            prepared = client.prepare_new_thread(section_id, title, message)
            with sqlite3.connect(DATABASE_FILE) as connection:
                reserve_forum_publication(connection, draft_id, item_key_value, title)
            
            thread_url = client.submit_new_thread(prepared)
            with sqlite3.connect(DATABASE_FILE) as connection:
                update_forum_publication(connection, item_key_value, "published", thread_url)
            print(f"Otomatik konu açıldı: {title} -> {thread_url}")
            time.sleep(5)  # Sunucuyu yormamak için kısa bekleme
        except Exception as err:
            print(f"Konu açılırken hata oluştu ({title}): {err}", file=sys.stderr)
            with sqlite3.connect(DATABASE_FILE) as connection:
                update_forum_publication(connection, item_key_value, "uncertain")


def run_bot_logic(args):
    """Executes the specific command requested by arguments."""
    if args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 1

    if args.command == "list":
        if args.draft_id is not None or args.confirm_publish:
            print("list does not accept a draft ID or --confirm-publish", file=sys.stderr)
            return 1
        list_drafts(args.limit)
        return 0
    if args.command == "forum-check":
        if args.draft_id is not None or args.confirm_publish:
            print("forum-check does not accept a draft ID or --confirm-publish", file=sys.stderr)
            return 1
        return check_forum_connection()
    if args.command == "publish":
        if args.draft_id is None:
            print("publish requires a draft ID", file=sys.stderr)
            return 1
        if not args.confirm_publish:
            print("publish requires the explicit --confirm-publish flag", file=sys.stderr)
            return 1
        return publish_draft(args.draft_id)
    if args.draft_id is not None or args.confirm_publish:
        print("fetch does not accept a draft ID or --confirm-publish", file=sys.stderr)
        return 1

    return fetch_all_feeds()


def main():
    parser = argparse.ArgumentParser(
        description="Read RSS feeds, manage drafts, and safely connect to MyBB."
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("fetch", "list", "forum-check", "publish"),
        default="fetch",
        help="fetch (default), list drafts, check the forum, or publish one draft",
    )
    parser.add_argument(
        "draft_id",
        nargs="?",
        type=int,
        help="draft ID required by the publish command",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="maximum drafts to show with the list command (default: 20)",
    )
    parser.add_argument(
        "--confirm-publish",
        action="store_true",
        help="required explicit confirmation before sending one topic",
    )
    args = parser.parse_args()

    # Eğer özel bir komut (list, forum-check, publish) verilmişse normal çalışır
    if args.command != "fetch" or args.draft_id is not None or args.confirm_publish:
        return run_bot_logic(args)

    # Varsayılan 'fetch' modunda botu sonsuz döngüye alıp sürekli çalıştırıyoruz
    print("--- SanatkarBot Kesintisiz Otomatik Çalışma Döngüsü Başlatıldı ---")
    while True:
        try:
            print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] RSS taraması ve otomatik yayın döngüsü başlıyor...")
            run_bot_logic(args)
        except Exception as error:
            print(f"Çalışma sırasında bir hata yakalandı, bot devam ediyor: {error}", file=sys.stderr)
        
        print("15 dakika bekleniyor...")
        time.sleep(900)


if __name__ == "__main__":
    raise SystemExit(main())