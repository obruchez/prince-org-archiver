import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from archiver.models import ParsedThreadPage, ResponseType, ThreadMetadata

logger = logging.getLogger(__name__)

NOT_FOUND_MARKERS = [
    "Thread not found",
    "Something went wrong",
    "thread does not exist",
]
CLOSED_MARKERS = [
    "this forum is currently closed",
    "Sorry, this forum is currently closed",
]


def classify_response(html: str) -> ResponseType:
    html_lower = html.lower()
    for marker in NOT_FOUND_MARKERS:
        if marker.lower() in html_lower:
            return ResponseType.NOT_FOUND
    for marker in CLOSED_MARKERS:
        if marker.lower() in html_lower:
            return ResponseType.FORUM_CLOSED
    # Check if it looks like a thread page (has post content)
    if '<div class="msg_body">' in html or '<td class="msg_body">' in html:
        return ResponseType.THREAD_FOUND
    # Fallback: check for breadcrumb with /msg/
    if "/msg/" in html and ("reply" in html_lower or "post" in html_lower):
        return ResponseType.THREAD_FOUND
    return ResponseType.NOT_FOUND


def parse_thread_page(
    thread_id: int, page_num: int, html_bytes: bytes
) -> ParsedThreadPage:
    html = html_bytes.decode("utf-8", errors="replace")
    response_type = classify_response(html)

    if response_type != ResponseType.THREAD_FOUND:
        return ParsedThreadPage(
            thread_id=thread_id,
            page_num=page_num,
            response_type=response_type,
            raw_html=html_bytes,
        )

    soup = BeautifulSoup(html, "lxml")

    # Extract title
    title = None
    title_tag = soup.find("title")
    if title_tag:
        title_text = title_tag.get_text(strip=True)
        # Title often includes " - prince.org" suffix
        title = re.sub(r"\s*[-–]\s*prince\.org.*$", "", title_text, flags=re.IGNORECASE)

    # Extract forum info from breadcrumb
    forum_id = None
    forum_name = None
    breadcrumb_links = soup.find_all("a", href=re.compile(r"/msg/\d+$"))
    for link in breadcrumb_links:
        href = link.get("href", "")
        match = re.search(r"/msg/(\d+)$", href)
        if match:
            forum_id = int(match.group(1))
            forum_name = link.get_text(strip=True)
            break

    # Extract page count from pagination
    page_count = 1
    # Look for "Page X of Y" pattern
    page_text = soup.find(string=re.compile(r"Page\s+\d+\s+of\s+\d+", re.IGNORECASE))
    if page_text:
        match = re.search(r"Page\s+\d+\s+of\s+(\d+)", str(page_text), re.IGNORECASE)
        if match:
            page_count = int(match.group(1))
    else:
        # Count pagination links
        page_links = soup.find_all("a", href=re.compile(r"\?.*pg=\d+"))
        if page_links:
            max_page = 1
            for link in page_links:
                href = link.get("href", "")
                match = re.search(r"pg=(\d+)", href)
                if match:
                    max_page = max(max_page, int(match.group(1)))
            page_count = max_page

    # Extract author (first post's author)
    author = None
    author_tags = soup.find_all("a", href=re.compile(r"/profile/"))
    if author_tags:
        author = author_tags[0].get_text(strip=True)

    # Count posts on this page
    post_count = 0
    # Try different selectors for post bodies
    for selector in [
        "div.msg_body",
        "td.msg_body",
        'div[class*="post"]',
        'table[class*="post"]',
    ]:
        posts = soup.select(selector)
        if posts:
            post_count = len(posts)
            break
    if post_count == 0:
        # Fallback: count reply links or post anchors
        reply_links = soup.find_all("a", href=re.compile(r"reply\.php"))
        post_count = max(len(reply_links), 1)

    # Extract media URLs
    media_urls = extract_media_urls(soup)

    metadata = ThreadMetadata(
        thread_id=thread_id,
        forum_id=forum_id,
        forum_name=forum_name,
        title=title,
        author=author,
        page_count=page_count,
        media_urls=media_urls,
    )

    return ParsedThreadPage(
        thread_id=thread_id,
        page_num=page_num,
        response_type=response_type,
        metadata=metadata,
        post_count=post_count,
        media_urls=media_urls,
        raw_html=html_bytes,
    )


SKIP_ICONS = {
    "b_new.png", "onnow.gif", "print.gif", "up.gif", "b_reply.gif",
    "b_replyq.gif", "b_email.gif", "b_note.gif", "b_report.gif",
    "b_post.gif", "b_newtopic.gif", "msg_sticky.gif", "spacer.gif",
    "arrow_right.gif", "arrow_left.gif",
}


def extract_media_urls(soup: BeautifulSoup) -> list[str]:
    urls = set()
    base_url = "https://prince.org"

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue

        filename = src.rsplit("/", 1)[-1].split("?")[0]

        # Skip known navigation/UI icons
        if filename in SKIP_ICONS:
            continue

        # Avatars
        if "avatars/" in src or ":444/" in src:
            urls.add(src if src.startswith("http") else urljoin(base_url, src))
        # Emoticons (in /i/s/ directory)
        elif "/i/s/" in src:
            urls.add(urljoin(base_url, src))
        # Other prince.org hosted images (not in /i/ which is UI chrome)
        elif ("prince.org" in src or src.startswith("/")) and "/i/" not in src:
            urls.add(src if src.startswith("http") else urljoin(base_url, src))

    return sorted(urls)
