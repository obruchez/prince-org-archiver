from bs4 import BeautifulSoup


def parse_events_page(html_bytes: bytes) -> dict:
    """Parse an events calendar page, returning basic metadata."""
    html = html_bytes.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")

    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)

    # Count event entries (links within calendar cells)
    event_links = soup.find_all("a", href=lambda h: h and "/events/" in h and "edit" not in h)
    has_content = len(html_bytes) > 1000

    return {
        "title": title,
        "event_link_count": len(event_links),
        "has_content": has_content,
    }
