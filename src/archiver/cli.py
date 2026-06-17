import asyncio
import logging
import re
import signal
import sys
from datetime import date, timedelta
from pathlib import Path

import click
from rich.console import Console
from rich.live import Live
from rich.table import Table

from archiver.client import HttpClient
from archiver.config import Config
from archiver.db import Database

console = Console()
_shutdown = False


def setup_logging(config: Config, verbose: bool) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.log_path.parent.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(config.log_path),
            logging.StreamHandler(),
        ],
    )


def handle_signal(sig, frame):
    global _shutdown
    if _shutdown:
        console.print("[red]Force quit[/red]")
        sys.exit(1)
    _shutdown = True
    console.print("\n[yellow]Shutting down gracefully (Ctrl+C again to force)...[/yellow]")


@click.group()
def cli():
    """Prince.org forum archiver."""
    pass


@cli.group()
def crawl():
    """Start or resume crawling."""
    pass


def common_options(f):
    f = click.option("--data-dir", type=click.Path(), default="data", help="Output directory")(f)
    f = click.option("--rate", type=float, default=0.5, help="Requests per second")(f)
    f = click.option("--concurrency", type=int, default=3, help="Max concurrent requests")(f)
    f = click.option("--max-requests", type=int, default=None, help="Stop after N requests")(f)
    f = click.option("--max-duration", type=str, default=None, help="Stop after duration (e.g. 8h)")(f)
    f = click.option("--retry-errors", is_flag=True, help="Retry previously errored items")(f)
    f = click.option("-v", "--verbose", is_flag=True, help="Verbose output")(f)
    return f


def parse_duration(s: str | None) -> int | None:
    if not s:
        return None
    s = s.strip().lower()
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    if s.endswith("m"):
        return int(float(s[:-1]) * 60)
    if s.endswith("s"):
        return int(float(s[:-1]))
    return int(s)


def parse_since_date(s: str | None) -> str | None:
    """Accept either an ISO date (`2026-03-19`) or a relative offset
    (`60d`, `8w`, `3m`, `1y`) and return an ISO date string suitable
    for `last_post_date >= ?` comparison."""
    if not s:
        return None
    s = s.strip().lower()
    # ISO date pass-through
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    m = re.fullmatch(r"(\d+)([dwmy])", s)
    if not m:
        raise click.BadParameter(
            f"--refresh-since: expected ISO date (YYYY-MM-DD) or "
            f"relative offset like 60d/8w/3m/1y, got {s!r}"
        )
    n = int(m.group(1))
    unit_days = {"d": 1, "w": 7, "m": 30, "y": 365}[m.group(2)]
    return (date.today() - timedelta(days=n * unit_days)).isoformat()


def make_config(data_dir, rate, concurrency, max_requests, max_duration, retry_errors, **kwargs) -> Config:
    return Config(
        data_dir=Path(data_dir),
        rate=rate,
        concurrency=concurrency,
        max_requests=max_requests,
        max_duration_seconds=parse_duration(max_duration),
        retry_errors=retry_errors,
        **kwargs,
    )


@crawl.command("threads")
@common_options
@click.option("--start-id", type=int, default=None, help="Start from this thread ID")
@click.option("--end-id", type=int, default=None, help="End at this thread ID")
@click.option("--forum", type=int, default=None, help="Only crawl threads from this forum")
@click.option("--priority-forums", type=str, default="7,100,8", help="Comma-separated priority forum IDs")
@click.option("--pages/--no-pages", default=True, help="Also download remaining pages of multi-page threads")
@click.option(
    "--refresh-since",
    type=str,
    default=None,
    help="Before enumerating new IDs, re-pull threads with activity since "
         "this point. Accepts ISO date (2026-03-19) or relative (60d/8w/3m/1y).",
)
def crawl_threads(data_dir, rate, concurrency, max_requests, max_duration, retry_errors, verbose,
                  start_id, end_id, forum, priority_forums, pages, refresh_since):
    """Discover threads and download pages."""
    extra = {}
    if start_id:
        extra["start_id"] = start_id
    if end_id:
        extra["end_id"] = end_id
    if forum:
        extra["forum_filter"] = forum
    extra["priority_forums"] = [int(x) for x in priority_forums.split(",")]

    refresh_since_date = parse_since_date(refresh_since)

    config = make_config(data_dir, rate, concurrency, max_requests, max_duration, retry_errors, **extra)
    setup_logging(config, verbose)
    config.ensure_dirs()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    asyncio.run(_run_thread_crawl(config, pages, refresh_since_date))


async def _run_thread_crawl(
    config: Config,
    download_pages: bool,
    refresh_since_date: str | None = None,
):
    from archiver.crawlers.threads import (
        crawl_remaining_pages,
        crawl_thread_ids,
        refresh_recent_threads,
    )

    db = Database(config.db_path)
    await db.connect()
    await db.recover_from_crash()

    async with HttpClient(config) as client:
        # Phase 0 (optional): refresh recently-active threads
        if refresh_since_date and not _shutdown:
            forum_filter = (
                [config.forum_filter] if config.forum_filter else None
            )
            console.print(
                f"[bold]Phase 0: Refresh threads with activity since "
                f"{refresh_since_date}[/bold]"
                + (f" (forum {config.forum_filter})" if forum_filter else "")
            )

            def on_refresh_progress(stats):
                if _shutdown:
                    raise KeyboardInterrupt
                if stats["checked"] % 100 == 0:
                    console.print(
                        f"  Refreshed {stats['checked']:,} "
                        f"(grew={stats['grew']:,} unchanged={stats['unchanged']:,} "
                        f"errors={stats['errors']:,}) "
                        f"| pages queued={stats['pages_queued']:,} "
                        f"| {client.request_count:,} requests"
                    )

            try:
                rstats = await refresh_recent_threads(
                    config, db, client,
                    since_date=refresh_since_date,
                    forum_ids=forum_filter,
                    progress_callback=on_refresh_progress,
                )
                console.print(f"[green]Refresh complete:[/green] {rstats}")
            except KeyboardInterrupt:
                console.print("[yellow]Interrupted. Progress saved.[/yellow]")
                await db.close()
                return

        # Phase 1: Thread discovery
        console.print(f"[bold]Phase 1: Thread discovery[/bold] (IDs {config.start_id} - {config.end_id})")

        def on_progress(last_id, stats):
            if _shutdown:
                raise KeyboardInterrupt
            total = config.end_id - config.start_id + 1
            done = last_id - config.start_id + 1
            pct = done / total * 100
            console.print(
                f"  ID {last_id:,} ({pct:.1f}%) | "
                f"found={stats['found']:,} not_found={stats['not_found']:,} "
                f"closed={stats['closed']:,} errors={stats['errors']:,} "
                f"| {client.request_count:,} requests"
            )

        try:
            stats = await crawl_thread_ids(config, db, client, progress_callback=on_progress)
        except KeyboardInterrupt:
            console.print("[yellow]Interrupted. Progress saved.[/yellow]")
            await db.close()
            return

        console.print(f"\n[green]Discovery complete:[/green] {stats}")

        # Phase 2: Remaining pages
        if download_pages and not _shutdown:
            console.print("\n[bold]Phase 2: Multi-page thread downloads[/bold]")

            def on_page_progress(thread_id, page_num, stats):
                if _shutdown:
                    raise KeyboardInterrupt
                if stats["downloaded"] % 100 == 0:
                    console.print(
                        f"  Pages: {stats['downloaded']:,} downloaded, "
                        f"{stats['errors']:,} errors | {client.request_count:,} requests"
                    )

            try:
                page_stats = await crawl_remaining_pages(
                    config, db, client, progress_callback=on_page_progress
                )
                console.print(f"\n[green]Pages complete:[/green] {page_stats}")
            except KeyboardInterrupt:
                console.print("[yellow]Interrupted. Progress saved.[/yellow]")

    await db.close()


@crawl.command("media")
@common_options
@click.option("--type", "media_type", type=click.Choice(["avatar", "emoticon", "post_image", "all"]),
              default="all", help="Media type to download")
def crawl_media_cmd(data_dir, rate, concurrency, max_requests, max_duration, retry_errors, verbose, media_type):
    """Download media files from archived threads."""
    config = make_config(data_dir, rate, concurrency, max_requests, max_duration, retry_errors)
    setup_logging(config, verbose)
    config.ensure_dirs()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    asyncio.run(_run_media_crawl(config, media_type))


async def _run_media_crawl(config: Config, media_type_str: str):
    from archiver.crawlers.media import crawl_media
    from archiver.models import MediaType

    mt = None if media_type_str == "all" else MediaType(media_type_str)

    db = Database(config.db_path)
    await db.connect()

    async with HttpClient(config) as client:
        console.print(f"[bold]Downloading media[/bold] (type={media_type_str})")

        def on_progress(stats):
            if _shutdown:
                raise KeyboardInterrupt
            console.print(
                f"  Media: {stats['downloaded']:,} downloaded, "
                f"{stats['errors']:,} errors | {client.request_count:,} requests"
            )

        try:
            stats = await crawl_media(config, db, client, media_type=mt, progress_callback=on_progress)
            console.print(f"\n[green]Media complete:[/green] {stats}")
        except KeyboardInterrupt:
            console.print("[yellow]Interrupted. Progress saved.[/yellow]")

    await db.close()


@crawl.command("events")
@common_options
def crawl_events_cmd(data_dir, rate, concurrency, max_requests, max_duration, retry_errors, verbose):
    """Archive events calendar."""
    config = make_config(data_dir, rate, concurrency, max_requests, max_duration, retry_errors)
    setup_logging(config, verbose)
    config.ensure_dirs()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    asyncio.run(_run_events_crawl(config))


async def _run_events_crawl(config: Config):
    from archiver.crawlers.events import crawl_events

    db = Database(config.db_path)
    await db.connect()

    async with HttpClient(config) as client:
        console.print("[bold]Archiving events calendar[/bold] (1998-2026)")

        def on_progress(year, month, stats):
            if _shutdown:
                raise KeyboardInterrupt
            console.print(
                f"  {year}/{month:02d}: {stats['downloaded']:,} downloaded, "
                f"{stats['errors']:,} errors"
            )

        try:
            stats = await crawl_events(config, db, client, progress_callback=on_progress)
            console.print(f"\n[green]Events complete:[/green] {stats}")
        except KeyboardInterrupt:
            console.print("[yellow]Interrupted. Progress saved.[/yellow]")

    await db.close()


@crawl.command("backfill")
@common_options
def crawl_backfill(data_dir, rate, concurrency, max_requests, max_duration, retry_errors, verbose):
    """Backfill forum IDs for closed threads."""
    config = make_config(data_dir, rate, concurrency, max_requests, max_duration, retry_errors)
    setup_logging(config, verbose)
    config.ensure_dirs()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    asyncio.run(_run_backfill(config))


async def _run_backfill(config: Config):
    from archiver.crawlers.backfill import backfill_closed_forum_ids

    db = Database(config.db_path)
    await db.connect()

    # Count how many need backfilling
    from archiver.crawlers.backfill import MAX_BACKFILL_ATTEMPTS

    rows = await db.db.execute_fetchall(
        "SELECT COUNT(*) FROM threads WHERE status = 'closed' "
        "AND forum_id IS NULL AND retry_count < ?",
        (MAX_BACKFILL_ATTEMPTS,),
    )
    total = rows[0][0]
    console.print(f"[bold]Backfilling forum IDs for {total:,} closed threads[/bold]")

    if total == 0:
        console.print("[green]Nothing to backfill.[/green]")
        await db.close()
        return

    async with HttpClient(config) as client:
        def on_progress(stats):
            if _shutdown:
                raise KeyboardInterrupt
            console.print(
                f"  Updated: {stats['updated']:,}, errors: {stats['errors']:,} "
                f"| {client.request_count:,} requests"
            )

        try:
            stats = await backfill_closed_forum_ids(
                config, db, client, progress_callback=on_progress
            )
            console.print(f"\n[green]Backfill complete:[/green] {stats}")
        except KeyboardInterrupt:
            console.print("[yellow]Interrupted. Progress saved.[/yellow]")

    await db.close()


@crawl.command("metadata-backfill")
@click.option("--data-dir", type=click.Path(), default="data", help="Output directory")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.option("--forum/--no-forum", default=True, help="Backfill missing forum_id")
@click.option("--dates/--no-dates", default=True, help="Backfill post dates")
def crawl_metadata_backfill(data_dir, verbose, forum, dates):
    """Backfill forum_id / post dates from already-downloaded HTML (offline)."""
    config = Config(data_dir=Path(data_dir))
    setup_logging(config, verbose)
    config.ensure_dirs()

    signal.signal(signal.SIGINT, handle_signal)
    asyncio.run(_run_metadata_backfill(config, forum, dates))


async def _run_metadata_backfill(config: Config, do_forum: bool, do_dates: bool):
    from archiver.crawlers.metadata_backfill import backfill_metadata_from_html

    db = Database(config.db_path)
    await db.connect()

    console.print(
        f"[bold]Offline metadata backfill[/bold] "
        f"(forum_id={do_forum}, dates={do_dates})"
    )

    def on_progress(done, total, stats):
        console.print(
            f"  {done:,}/{total:,} | forum_id+={stats['forum_id']:,} "
            f"dates+={stats['dates']:,} no_match={stats['no_match']:,} "
            f"missing_files={stats['missing_files']:,}"
        )

    stats = await backfill_metadata_from_html(
        config, db, do_forum=do_forum, do_dates=do_dates,
        progress_callback=on_progress,
    )
    console.print(f"\n[green]Metadata backfill complete:[/green] {stats}")
    await db.close()


@crawl.command("wayback")
@click.option("--data-dir", type=click.Path(), default="data", help="Output directory")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.option("--target", "targets", multiple=True,
              type=click.Choice(["gated", "closed", "index"]),
              default=["gated", "closed", "index"],
              help="What to recover (repeatable)")
@click.option("--rate", type=float, default=0.2,
              help="Requests/sec against web.archive.org (default 0.2 = 1 req/5s; "
                   "stays under the ~15/min unauthenticated CDX limit)")
@click.option("--limit", type=int, default=None,
              help="Stop after N targets (for testing)")
def crawl_wayback(data_dir, verbose, targets, rate, limit):
    """Recover gone content from the Wayback Machine (archive.org)."""
    config = Config(data_dir=Path(data_dir), wayback_rate=rate)
    setup_logging(config, verbose)
    config.ensure_dirs()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    asyncio.run(_run_wayback(config, list(targets), limit))


async def _run_wayback(config: Config, targets: list[str], limit):
    from archiver.crawlers.wayback import recover_via_wayback

    db = Database(config.db_path)
    await db.connect()

    console.print(
        f"[bold]Wayback recovery[/bold] targets={targets} "
        f"rate={config.wayback_rate}/s"
        + (f" limit={limit}" if limit else "")
    )

    def on_progress(stats):
        if _shutdown:
            raise KeyboardInterrupt
        console.print(
            f"  done={stats['done']:,} | recovered={stats['recovered']:,} "
            f"no_capture={stats['no_capture']:,} "
            f"no_forum={stats.get('no_forum', 0):,} "
            f"throttled={stats.get('throttled', 0):,} "
            f"errors={stats['error']:,}"
        )

    try:
        stats = await recover_via_wayback(
            config, db, targets=targets, limit=limit,
            progress_callback=on_progress,
        )
        console.print(f"\n[green]Wayback recovery complete:[/green] {stats}")
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted. Progress saved.[/yellow]")

    await db.close()


@crawl.command("integrate-wayback")
@click.option("--data-dir", type=click.Path(), default="data", help="Output directory")
@click.option("--limit", type=int, default=None,
              help="Stop after N recovered threads (for testing)")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
def crawl_integrate_wayback(data_dir, limit, verbose):
    """Fold recovered Wayback HTML into the main threads/thread_pages
    schema (parses titles/authors/page counts; registers media URLs)."""
    config = Config(data_dir=Path(data_dir))
    setup_logging(config, verbose)
    config.ensure_dirs()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    asyncio.run(_run_integrate_wayback(config, limit))


async def _run_integrate_wayback(config: Config, limit):
    from archiver.crawlers.wayback_integrate import integrate_recovered_wayback

    db = Database(config.db_path)
    await db.connect()

    console.print(
        "[bold]Integrating recovered Wayback threads[/bold]"
        + (f" limit={limit}" if limit else "")
    )

    def on_progress(stats):
        if _shutdown:
            raise KeyboardInterrupt
        console.print(
            f"  scanned={stats['scanned']:,} | "
            f"enriched={stats['enriched']:,} "
            f"unparseable={stats['unparseable']:,} "
            f"missing_file={stats['missing_file']:,} "
            f"media_added={stats['media_added']:,}"
        )

    try:
        stats = await integrate_recovered_wayback(
            config, db, limit=limit, progress_callback=on_progress,
        )
        console.print(f"\n[green]Integration complete:[/green] {stats}")
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted. Progress saved.[/yellow]")

    await db.close()


@crawl.command("all")
@common_options
@click.option("--priority-forums", type=str, default="7,100,8")
def crawl_all(data_dir, rate, concurrency, max_requests, max_duration, retry_errors, verbose, priority_forums):
    """Run all crawlers in priority order."""
    pf = [int(x) for x in priority_forums.split(",")]
    config = make_config(
        data_dir, rate, concurrency, max_requests, max_duration, retry_errors,
        priority_forums=pf,
    )
    setup_logging(config, verbose)
    config.ensure_dirs()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    asyncio.run(_run_all(config))


async def _run_all(config: Config):
    from archiver.crawlers.events import crawl_events
    from archiver.crawlers.media import crawl_media
    from archiver.crawlers.threads import crawl_remaining_pages, crawl_thread_ids

    db = Database(config.db_path)
    await db.connect()
    await db.recover_from_crash()

    async with HttpClient(config) as client:
        if not _shutdown:
            console.print("[bold]Phase 1: Thread discovery[/bold]")
            try:
                await crawl_thread_ids(config, db, client)
            except KeyboardInterrupt:
                pass

        if not _shutdown:
            console.print("\n[bold]Phase 2: Multi-page downloads[/bold]")
            try:
                await crawl_remaining_pages(config, db, client)
            except KeyboardInterrupt:
                pass

        if not _shutdown:
            console.print("\n[bold]Phase 3: Media downloads[/bold]")
            try:
                await crawl_media(config, db, client)
            except KeyboardInterrupt:
                pass

        if not _shutdown:
            console.print("\n[bold]Phase 4: Events calendar[/bold]")
            try:
                await crawl_events(config, db, client)
            except KeyboardInterrupt:
                pass

    await db.close()
    console.print("\n[bold green]All phases complete.[/bold green]")


@cli.command()
@click.option("--data-dir", type=click.Path(), default="data")
def status(data_dir):
    """Show crawl progress and statistics."""
    asyncio.run(_show_status(Path(data_dir)))


async def _show_status(data_dir: Path):
    db_path = data_dir / "archive.db"
    if not db_path.exists():
        console.print("[red]No database found. Run 'crawl' first.[/red]")
        return

    db = Database(db_path)
    await db.connect()

    stats = await db.get_stats()
    last_id = await db.get_state("last_enumerated_id")

    table = Table(title="Prince.org Archiver Status")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Last enumerated ID", last_id or "N/A")
    table.add_row("", "")
    table.add_row("Threads found", f"{stats['threads_complete']:,}")
    table.add_row("Threads not found", f"{stats['threads_not_found']:,}")
    table.add_row("Threads closed", f"{stats['threads_closed']:,}")
    table.add_row("Threads errored", f"{stats['threads_error']:,}")
    table.add_row("Threads pending", f"{stats['threads_pending']:,}")
    table.add_row("Threads total", f"{stats['threads_total']:,}")
    table.add_row("", "")
    table.add_row("Pages downloaded", f"{stats['pages_downloaded']:,}")
    table.add_row("Pages pending", f"{stats['pages_pending']:,}")
    table.add_row("", "")
    table.add_row("Media downloaded", f"{stats['media_downloaded']:,}")
    table.add_row("Media pending", f"{stats['media_pending']:,}")

    console.print(table)

    if stats["forums"]:
        forum_table = Table(title="Per-Forum Breakdown")
        forum_table.add_column("Forum ID")
        forum_table.add_column("Threads", justify="right")
        forum_table.add_column("Complete", justify="right")
        for f in stats["forums"]:
            forum_table.add_row(
                str(f["forum_id"]),
                f"{f['cnt']:,}",
                f"{f['done']:,}",
            )
        console.print(forum_table)

    await db.close()


@cli.command()
@click.option("--data-dir", type=click.Path(), default="data")
def verify(data_dir):
    """Verify integrity of downloaded files."""
    asyncio.run(_verify(Path(data_dir)))


async def _verify(data_dir: Path):
    db_path = data_dir / "archive.db"
    if not db_path.exists():
        console.print("[red]No database found.[/red]")
        return

    db = Database(db_path)
    await db.connect()

    # Check that downloaded pages have files on disk
    rows = await db.db.execute_fetchall(
        "SELECT thread_id, page_num, html_path FROM thread_pages WHERE status = 'downloaded'"
    )
    missing = 0
    for row in rows:
        path = row[2]
        if path and not Path(path).exists():
            missing += 1
            console.print(f"[red]Missing: thread {row[0]} page {row[1]}: {path}[/red]")

    total = len(rows)
    console.print(f"\nVerified {total:,} pages: {total - missing:,} OK, {missing:,} missing")

    await db.close()


if __name__ == "__main__":
    cli()
