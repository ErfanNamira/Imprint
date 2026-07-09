#!/usr/bin/env python3
"""
Imprint 1.0 - Full-size image downloader for web pages.

Scrapes a web page for images (preferring the largest/full-size version it
can find), saves them into neatly organized subfolders *inside the app's own
working directory*, supports concurrent downloads, batch downloading from a
text file of URLs, a non-interactive CLI mode, real image-dimension checks
(via Pillow when available), retry-with-backoff, duplicate detection by
content hash, and a searchable history.

pip install requests beautifulsoup4 rich pillow
"""

import argparse
import concurrent.futures
import hashlib
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

from rich.console import Console
from rich.text import Text
from rich.panel import Panel
from rich.align import Align
from rich.table import Table
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.progress import (
    Progress,
    BarColumn,
    TextColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
    SpinnerColumn,
)

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# --------------------------------------------------------------------------
# Constants / paths
# --------------------------------------------------------------------------

VERSION = "1.0.0"

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
HISTORY_DB_PATH = APP_DIR / "history.db"
DOWNLOADS_ROOT = APP_DIR / "downloads"

DEFAULT_CONFIG = {
    "download_dir": str(DOWNLOADS_ROOT),
    "min_width": 600,          # skip images smaller than this (likely icons/thumbs)
    "min_height": 600,
    "enforce_real_dimensions": True,   # actually decode header bytes w/ Pillow if available
    "timeout": 15,             # seconds per HTTP request
    "max_retries": 3,
    "retry_backoff": 1.5,      # seconds, multiplied by attempt number
    "max_workers": 6,          # concurrent downloads per page
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Imprint/%s" % VERSION
    ),
    "organize_by": "domain_and_page",  # domain_and_page | domain | flat | date
    "skip_existing": True,
    "dedupe_by_hash": True,     # skip images whose content already exists anywhere in history
    "allowed_extensions": [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".avif"],
}

console = Console()


# --------------------------------------------------------------------------
# Config handling
# --------------------------------------------------------------------------

def ensure_app_dirs():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    Path(load_config_raw().get("download_dir", str(DOWNLOADS_ROOT))).mkdir(parents=True, exist_ok=True)


def load_config_raw():
    """Read config without side effects (used before full ensure_app_dirs)."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return dict(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def load_config():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        cfg = dict(DEFAULT_CONFIG)
    else:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            changed = False
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
                    changed = True
            if changed:
                save_config(cfg)
        except (json.JSONDecodeError, OSError):
            console.print("[bold red]Config file was corrupt. Restoring defaults.[/]")
            save_config(DEFAULT_CONFIG)
            cfg = dict(DEFAULT_CONFIG)

    Path(cfg["download_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def save_config(cfg):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# --------------------------------------------------------------------------
# History (SQLite)
# --------------------------------------------------------------------------

_DB_LOCK = threading.Lock()


def get_db():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: this connection is shared across the
    # ThreadPoolExecutor worker threads used for concurrent downloads.
    # All access to it is serialized via _DB_LOCK (sqlite3 connections
    # are not safe to use concurrently from multiple threads without it).
    conn = sqlite3.connect(HISTORY_DB_PATH, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_url TEXT,
            image_url TEXT,
            saved_path TEXT,
            filesize INTEGER,
            content_hash TEXT,
            status TEXT,
            timestamp TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON history(content_hash)")
    conn.commit()
    return conn


def log_history(conn, page_url, image_url, saved_path, filesize, status, content_hash=""):
    with _DB_LOCK:
        conn.execute(
            "INSERT INTO history (page_url, image_url, saved_path, filesize, content_hash, status, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (page_url, image_url, saved_path, filesize, content_hash, status,
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()


def hash_already_downloaded(conn, content_hash):
    with _DB_LOCK:
        row = conn.execute(
            "SELECT saved_path FROM history WHERE content_hash = ? AND status = 'success' LIMIT 1",
            (content_hash,),
        ).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------
# Banner
# --------------------------------------------------------------------------

def banner():
    text = Text(justify="center")
    text.append("🖼️  ", style="bold magenta")
    text.append("IMPRINT", style="bold yellow")
    text.append("  🖼\n", style="bold magenta")
    text.append("📸  ", style="bold magenta")
    text.append("Leave a mark of every image you find", style="italic cyan")
    text.append("  📸\n", style="bold magenta")
    text.append("⬇ ", style="bold magenta")
    text.append("Full-Size Image Downloader", style="bold white")
    text.append(" ⬇", style="bold magenta")
    panel = Panel(
        Align.center(text, vertical="middle"),
        title="[bold cyan]❖[/]",
        border_style="bright_blue",
        subtitle=f"[dim]v{VERSION}[/]",
        width=60,
        padding=(1, 2),
    )
    console.print(panel)
    console.print(f"[dim]Working directory:[/] {APP_DIR}")
    console.print()


# --------------------------------------------------------------------------
# Helpers: naming / organizing
# --------------------------------------------------------------------------

def safe_slug(text, max_len=60):
    text = re.sub(r"[^\w\-. ]", "_", text)
    text = re.sub(r"\s+", "_", text).strip("_")
    return text[:max_len] if text else "page"


def get_target_dir(cfg, page_url):
    """Every download always lands in a subfolder under cfg['download_dir'],
    which itself defaults to a 'downloads' subfolder of the app's own
    working directory (see DOWNLOADS_ROOT)."""
    base = Path(cfg["download_dir"])
    parsed = urlparse(page_url)
    domain = parsed.netloc.replace(":", "_") or "unknown_site"
    mode = cfg.get("organize_by", "domain_and_page")

    if mode == "flat":
        target = base
    elif mode == "domain":
        target = base / safe_slug(domain)
    elif mode == "date":
        target = base / datetime.now().strftime("%Y-%m-%d")
    else:  # domain_and_page
        page_slug = safe_slug(parsed.path.strip("/").replace("/", "_") or "home")
        target = base / safe_slug(domain) / page_slug

    target.mkdir(parents=True, exist_ok=True)
    return target


def filename_from_url(url, resp=None):
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name)
    if not name or "." not in name:
        ext = ".jpg"
        if resp is not None:
            ctype = resp.headers.get("Content-Type", "")
            guessed = mimetypes.guess_extension(ctype.split(";")[0].strip())
            if guessed:
                ext = guessed
        digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
        name = f"image_{digest}{ext}"
    return name


def unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    stem, ext = os.path.splitext(filename)
    i = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{i}{ext}"
        i += 1
    return candidate


# --------------------------------------------------------------------------
# Core scraping / downloading logic
# --------------------------------------------------------------------------

def build_session(cfg):
    s = requests.Session()
    s.headers.update({"User-Agent": cfg["user_agent"]})
    return s


def _parse_srcset_best(srcset):
    """Return the URL with the largest width/density descriptor in a srcset string."""
    entries = [e.strip() for e in srcset.split(",") if e.strip()]
    best_url, best_score = None, -1.0
    for entry in entries:
        parts = entry.split()
        url_part = parts[0]
        score = 0.0
        if len(parts) > 1:
            desc = parts[1].lower()
            try:
                if desc.endswith("w"):
                    score = float(desc[:-1])
                elif desc.endswith("x"):
                    score = float(desc[:-1]) * 1000  # density descriptors, normalize roughly
            except ValueError:
                score = 0.0
        if score >= best_score:
            best_score, best_url = score, url_part
    return best_url


def extract_image_urls(page_url, html, cfg):
    """Return a de-duplicated list of candidate full-size image URLs from a page."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = set()

    def add(u):
        if not u:
            return
        u = u.strip()
        if u.startswith("data:"):
            return
        candidates.add(urljoin(page_url, u))

    for img in soup.find_all("img"):
        srcset = img.get("srcset") or img.get("data-srcset")
        if srcset:
            add(_parse_srcset_best(srcset))
        for attr in ("data-src", "data-original", "data-lazy-src", "src"):
            val = img.get(attr)
            if val:
                add(val)
                break

    # <a> tags that link directly to an image file (common "click to enlarge" pattern)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(href.lower().split("?")[0].endswith(ext) for ext in cfg["allowed_extensions"]):
            add(href)

    # <picture><source> elements
    for source in soup.find_all("source"):
        srcset = source.get("srcset")
        if srcset:
            add(_parse_srcset_best(srcset))

    # CSS background-image: url(...) on inline style attributes
    for tag in soup.find_all(style=True):
        for m in re.finditer(r"background(?:-image)?\s*:\s*url\((['\"]?)(.*?)\1\)", tag["style"], re.I):
            add(m.group(2))

    # og:image / twitter:image meta (often the highest quality single image for the page)
    for prop in ("og:image", "og:image:secure_url", "twitter:image"):
        meta = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if meta and meta.get("content"):
            add(meta["content"])

    filtered = []
    for u in candidates:
        path = urlparse(u).path.lower()
        if any(path.endswith(ext) for ext in cfg["allowed_extensions"]):
            filtered.append(u)
        elif "." not in Path(path).name:
            filtered.append(u)

    return sorted(set(filtered))


def _meets_dimension_requirements(chunk_bytes, cfg):
    """Best-effort check using just the bytes downloaded so far via Pillow."""
    if not HAS_PIL or not cfg.get("enforce_real_dimensions", True):
        return True
    try:
        with Image.open(io.BytesIO(chunk_bytes)) as im:
            w, h = im.size
            return w >= cfg["min_width"] and h >= cfg["min_height"]
    except Exception:
        # Not enough data yet / unreadable header -> can't judge, don't block
        return None


def download_one_image(session, cfg, page_url, image_url, target_dir, conn):
    """Download a single image with retries + backoff. Returns (status, url, detail)."""
    last_error = None
    for attempt in range(1, cfg.get("max_retries", 3) + 1):
        try:
            resp = session.get(image_url, timeout=cfg["timeout"], stream=True)
            resp.raise_for_status()

            ctype = resp.headers.get("Content-Type", "")
            if ctype and not ctype.startswith("image/"):
                log_history(conn, page_url, image_url, "", 0, "skipped_not_image")
                return "skipped", image_url, "not an image"

            buf = io.BytesIO()
            dim_checked = False
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                buf.write(chunk)
                if not dim_checked and buf.tell() > 32_000:
                    verdict = _meets_dimension_requirements(buf.getvalue(), cfg)
                    if verdict is False:
                        log_history(conn, page_url, image_url, "", 0, "skipped_too_small")
                        return "skipped", image_url, "below min dimensions"
                    if verdict is True:
                        dim_checked = True

            data = buf.getvalue()
            if not dim_checked:
                verdict = _meets_dimension_requirements(data, cfg)
                if verdict is False:
                    log_history(conn, page_url, image_url, "", 0, "skipped_too_small")
                    return "skipped", image_url, "below min dimensions"

            content_hash = hashlib.sha256(data).hexdigest()
            if cfg.get("dedupe_by_hash", True):
                existing = hash_already_downloaded(conn, content_hash)
                if existing:
                    log_history(conn, page_url, image_url, existing, len(data),
                                "skipped_duplicate", content_hash)
                    return "skipped", image_url, f"duplicate of {existing}"

            filename = filename_from_url(image_url, resp)
            dest = unique_path(target_dir, filename)
            if cfg.get("skip_existing") and (target_dir / filename).exists():
                existing_path = target_dir / filename
                log_history(conn, page_url, image_url, str(existing_path),
                            existing_path.stat().st_size, "skipped_existing", content_hash)
                return "skipped", image_url, str(existing_path)

            with open(dest, "wb") as f:
                f.write(data)

            log_history(conn, page_url, image_url, str(dest), len(data), "success", content_hash)
            return "success", image_url, dest

        except requests.RequestException as e:
            last_error = e
            if attempt < cfg.get("max_retries", 3):
                time.sleep(cfg.get("retry_backoff", 1.5) * attempt)
                continue
            log_history(conn, page_url, image_url, "", 0, f"error: {e}")
            return "error", image_url, str(e)

    log_history(conn, page_url, image_url, "", 0, f"error: {last_error}")
    return "error", image_url, str(last_error)


def process_page(cfg, session, conn, page_url, verbose=True):
    """Fetch a page, find images, download them concurrently.
    Returns (success, skipped, failed) counts."""
    if verbose:
        console.print(f"\n[bold cyan]➜ Fetching page:[/] {page_url}")

    try:
        resp = session.get(page_url, timeout=cfg["timeout"])
        resp.raise_for_status()
    except requests.RequestException as e:
        console.print(f"[bold red]  Failed to load page:[/] {e}")
        log_history(conn, page_url, "", "", 0, f"page_error: {e}")
        return 0, 0, 0

    image_urls = extract_image_urls(page_url, resp.text, cfg)
    if not image_urls:
        console.print("[yellow]  No images found on this page.[/]")
        return 0, 0, 0

    target_dir = get_target_dir(cfg, page_url)
    if verbose:
        console.print(f"[dim]  Found {len(image_urls)} candidate image(s). Saving to:[/] {target_dir}")

    success = skipped = failed = 0
    max_workers = max(1, int(cfg.get("max_workers", 6)))

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.fields[name]}", justify="right"),
        BarColumn(bar_width=24),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
        transient=False,
    ) as progress:
        overall = progress.add_task("overall", name="Overall", total=len(image_urls))

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(download_one_image, session, cfg, page_url, url, target_dir, conn): url
                for url in image_urls
            }
            for future in concurrent.futures.as_completed(futures):
                url = futures[future]
                try:
                    status, _, result = future.result()
                except Exception as e:
                    status, result = "error", str(e)

                if status == "success":
                    success += 1
                    console.print(f"  [green]✓[/] {Path(result).name}")
                elif status == "skipped":
                    skipped += 1
                    console.print(f"  [yellow]↷ skipped[/] ({url.rsplit('/', 1)[-1]}) — {result}")
                else:
                    failed += 1
                    console.print(f"  [red]✗ failed[/] {url} — {result}")

                progress.update(overall, advance=1)

    return success, skipped, failed


# --------------------------------------------------------------------------
# Menu actions
# --------------------------------------------------------------------------

def action_single_url(cfg):
    url = Prompt.ask("[bold cyan]Enter the page URL to scan for images[/]").strip()
    if not url:
        console.print("[yellow]No URL entered.[/]")
        return
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    session = build_session(cfg)
    conn = get_db()
    t0 = time.time()
    s, sk, f = process_page(cfg, session, conn, url)
    elapsed = time.time() - t0
    conn.close()

    console.print(
        Panel(
            f"[green]Downloaded: {s}[/]   [yellow]Skipped: {sk}[/]   [red]Failed: {f}[/]\n"
            f"[dim]Finished in {elapsed:.1f}s[/]",
            title="Summary",
            border_style="green",
        )
    )


def action_batch(cfg):
    path_str = Prompt.ask("[bold cyan]Path to text file with URLs (one per line)[/]").strip()
    path = Path(path_str).expanduser()
    if not path.exists():
        console.print(f"[bold red]File not found:[/] {path}")
        return

    urls = [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    urls = [u if u.startswith(("http://", "https://")) else f"https://{u}" for u in urls]

    if not urls:
        console.print("[yellow]No valid URLs found in that file.[/]")
        return

    console.print(f"[cyan]Loaded {len(urls)} URL(s) from[/] {path}")
    if not Confirm.ask("Proceed with batch download?", default=True):
        return

    run_batch(cfg, urls)


def run_batch(cfg, urls):
    session = build_session(cfg)
    conn = get_db()
    total_success = total_skipped = total_failed = 0
    t0 = time.time()

    for i, url in enumerate(urls, 1):
        console.rule(f"[bold]{i}/{len(urls)}[/]")
        s, sk, f = process_page(cfg, session, conn, url)
        total_success += s
        total_skipped += sk
        total_failed += f

    conn.close()
    elapsed = time.time() - t0

    console.print(
        Panel(
            f"Pages processed: {len(urls)}\n"
            f"[green]Downloaded: {total_success}[/]   "
            f"[yellow]Skipped: {total_skipped}[/]   "
            f"[red]Failed: {total_failed}[/]\n"
            f"[dim]Finished in {elapsed:.1f}s[/]",
            title="Batch Summary",
            border_style="green",
        )
    )
    return total_success, total_skipped, total_failed


def action_view_history(cfg):
    conn = get_db()
    cur = conn.execute(
        "SELECT id, page_url, saved_path, filesize, status, timestamp "
        "FROM history ORDER BY id DESC LIMIT 50"
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        console.print("[yellow]No download history yet.[/]")
        return

    table = Table(title="Download History (most recent 50)", show_lines=False)
    table.add_column("#", style="dim", width=5)
    table.add_column("Timestamp", style="cyan", no_wrap=True)
    table.add_column("Page", style="white", overflow="fold")
    table.add_column("Saved As", style="green", overflow="fold")
    table.add_column("Size", justify="right")
    table.add_column("Status")

    for row_id, page_url, saved_path, filesize, status, ts in rows:
        size_str = f"{filesize/1024:.1f} KB" if filesize else "-"
        status_style = "green" if status == "success" else ("yellow" if "skip" in status else "red")
        table.add_row(
            str(row_id),
            ts,
            page_url or "-",
            Path(saved_path).name if saved_path else "-",
            size_str,
            f"[{status_style}]{status}[/]",
        )

    console.print(table)

    if Confirm.ask("Clear all history?", default=False):
        conn = get_db()
        conn.execute("DELETE FROM history")
        conn.commit()
        conn.close()
        console.print("[green]History cleared.[/]")


def action_settings(cfg):
    while True:
        console.print()
        table = Table(title="Current Settings", show_header=True, header_style="bold magenta")
        table.add_column("#", width=3)
        table.add_column("Setting")
        table.add_column("Value", style="cyan")

        keys = list(cfg.keys())
        for i, k in enumerate(keys, 1):
            table.add_row(str(i), k, str(cfg[k]))
        console.print(table)

        console.print("\n[dim]Enter a number to edit that setting, or 'b' to go back.[/]")
        choice = Prompt.ask("Choice", default="b")
        if choice.lower() in ("b", "back", ""):
            break
        try:
            idx = int(choice) - 1
            key = keys[idx]
        except (ValueError, IndexError):
            console.print("[red]Invalid choice.[/]")
            continue

        current = cfg[key]
        if isinstance(current, bool):
            cfg[key] = Confirm.ask(f"{key}", default=current)
        elif isinstance(current, int):
            cfg[key] = IntPrompt.ask(f"{key}", default=current)
        elif isinstance(current, float):
            raw = Prompt.ask(f"{key}", default=str(current))
            try:
                cfg[key] = float(raw)
            except ValueError:
                console.print("[red]Invalid number.[/]")
        elif isinstance(current, list):
            raw = Prompt.ask(f"{key} (comma-separated)", default=", ".join(current))
            cfg[key] = [x.strip() for x in raw.split(",") if x.strip()]
        else:
            cfg[key] = Prompt.ask(f"{key}", default=str(current))

        if key == "download_dir":
            Path(cfg[key]).mkdir(parents=True, exist_ok=True)

        save_config(cfg)
        console.print("[green]Saved.[/]")


def action_open_download_folder(cfg):
    folder = Path(cfg["download_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    console.print(f"[cyan]Download folder:[/] {folder}")
    console.print("[dim](This is a subfolder of Imprint's own working directory.)[/]")

    if folder.exists():
        subdirs = sorted([p for p in folder.iterdir() if p.is_dir()])
        if subdirs:
            table = Table(title="Subfolders", show_header=False)
            for p in subdirs[:30]:
                count = sum(1 for _ in p.rglob("*") if _.is_file())
                table.add_row(str(p.relative_to(folder)), f"{count} file(s)")
            console.print(table)


def action_stats(cfg):
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    success = conn.execute("SELECT COUNT(*) FROM history WHERE status='success'").fetchone()[0]
    dup = conn.execute("SELECT COUNT(*) FROM history WHERE status='skipped_duplicate'").fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM history WHERE status LIKE 'error%'").fetchone()[0]
    total_bytes = conn.execute(
        "SELECT COALESCE(SUM(filesize),0) FROM history WHERE status='success'"
    ).fetchone()[0]
    conn.close()

    mb = total_bytes / (1024 * 1024)
    console.print(
        Panel(
            f"Total attempts logged: {total}\n"
            f"[green]Successful downloads:[/] {success}\n"
            f"[cyan]Duplicates skipped:[/] {dup}\n"
            f"[red]Failed:[/] {failed}\n"
            f"[cyan]Total data saved:[/] {mb:.2f} MB\n"
            f"[dim]Download folder:[/] {cfg['download_dir']}\n"
            f"[dim]Pillow dimension checks:[/] {'enabled' if HAS_PIL else 'unavailable (pip install pillow)'}",
            title="Statistics",
            border_style="blue",
        )
    )


# --------------------------------------------------------------------------
# Main menu
# --------------------------------------------------------------------------

MENU_OPTIONS = [
    ("1", "Download images from a single page", action_single_url),
    ("2", "Batch download from a text file (one URL per line)", action_batch),
    ("3", "View download history", action_view_history),
    ("4", "View statistics", action_stats),
    ("5", "Settings", action_settings),
    ("6", "Show download folder location", action_open_download_folder),
    ("0", "Exit", None),
]


def print_menu():
    table = Table(show_header=False, box=None, padding=(0, 2))
    for key, label, _ in MENU_OPTIONS:
        style = "bold red" if key == "0" else "bold yellow"
        table.add_row(f"[{style}]{key}[/]", label)
    console.print(Panel(table, title="[bold cyan]Main Menu[/]", border_style="bright_blue", width=60))


def interactive_main():
    ensure_app_dirs()
    cfg = load_config()
    banner()

    while True:
        print_menu()
        choice = Prompt.ask(
            "\n[bold green]Select an option[/]",
            choices=[opt[0] for opt in MENU_OPTIONS],
            show_choices=False,
        )
        if choice == "0":
            console.print("\n[bold cyan]Goodbye![/] 🖼️\n")
            sys.exit(0)

        for key, _, func in MENU_OPTIONS:
            if key == choice and func:
                try:
                    func(cfg)
                except KeyboardInterrupt:
                    console.print("\n[yellow]Cancelled.[/]")
                except Exception as e:
                    console.print(f"[bold red]Unexpected error:[/] {e}")
                break


# --------------------------------------------------------------------------
# CLI (non-interactive) mode
# --------------------------------------------------------------------------

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="imprint",
        description="Imprint - Full-size image downloader. Run with no arguments for the interactive menu.",
    )
    p.add_argument("url", nargs="?", help="A single page URL to scan and download images from")
    p.add_argument("-b", "--batch", metavar="FILE", help="Text file with one page URL per line")
    p.add_argument("-o", "--output", metavar="DIR", help="Override the download directory for this run")
    p.add_argument("-w", "--workers", type=int, help="Number of concurrent downloads per page")
    p.add_argument("--min-width", type=int, help="Minimum image width to keep")
    p.add_argument("--min-height", type=int, help="Minimum image height to keep")
    p.add_argument("--history", action="store_true", help="Print recent history and exit")
    p.add_argument("--stats", action="store_true", help="Print statistics and exit")
    return p


def main():
    parser = build_arg_parser()
    args = parser.parse_args()

    if not any([args.url, args.batch, args.history, args.stats]):
        interactive_main()
        return

    ensure_app_dirs()
    cfg = load_config()

    if args.output:
        cfg["download_dir"] = str(Path(args.output).expanduser())
        Path(cfg["download_dir"]).mkdir(parents=True, exist_ok=True)
    if args.workers:
        cfg["max_workers"] = args.workers
    if args.min_width:
        cfg["min_width"] = args.min_width
    if args.min_height:
        cfg["min_height"] = args.min_height

    banner()

    if args.history:
        action_view_history(cfg)
        return
    if args.stats:
        action_stats(cfg)
        return

    if args.batch:
        path = Path(args.batch).expanduser()
        if not path.exists():
            console.print(f"[bold red]File not found:[/] {path}")
            sys.exit(1)
        urls = [
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        urls = [u if u.startswith(("http://", "https://")) else f"https://{u}" for u in urls]
        if not urls:
            console.print("[yellow]No valid URLs found in that file.[/]")
            sys.exit(1)
        run_batch(cfg, urls)
        return

    if args.url:
        url = args.url if args.url.startswith(("http://", "https://")) else f"https://{args.url}"
        session = build_session(cfg)
        conn = get_db()
        t0 = time.time()
        s, sk, f = process_page(cfg, session, conn, url)
        elapsed = time.time() - t0
        conn.close()
        console.print(
            Panel(
                f"[green]Downloaded: {s}[/]   [yellow]Skipped: {sk}[/]   [red]Failed: {f}[/]\n"
                f"[dim]Finished in {elapsed:.1f}s[/]",
                title="Summary",
                border_style="green",
            )
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted. Bye![/]")
        sys.exit(0)
