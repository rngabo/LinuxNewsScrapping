import faulthandler
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import platform
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin
import warnings

import gi
import requests
from bs4 import BeautifulSoup
from gi.repository import GObject
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Force X11 backend for proper dock behavior on Wayland via XWayland
os.environ['GDK_BACKEND'] = 'x11'


def configure_logging():
    """Create durable diagnostics before GTK or worker threads start."""
    default_state_dir = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    ) / "news-dock"
    log_path = Path(
        os.environ.get("NEWS_DOCK_LOG_FILE", default_state_dir / "news-dock.log")
    ).expanduser()
    crash_path = Path(
        os.environ.get("NEWS_DOCK_CRASH_LOG_FILE", default_state_dir / "crash.log")
    ).expanduser()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        crash_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        fallback_dir = Path("/tmp") / f"news-dock-{os.getuid()}"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        log_path = fallback_dir / "news-dock.log"
        crash_path = fallback_dir / "crash.log"

    logger = logging.getLogger("news_dock")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s "
            "pid=%(process)d thread=%(threadName)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    logging.captureWarnings(True)

    crash_stream = None
    try:
        crash_stream = crash_path.open("a", encoding="utf-8")
        faulthandler.enable(file=crash_stream, all_threads=True)
    except (OSError, RuntimeError):
        logger.exception("diagnostic_crash_log_setup_failed path=%s", crash_path)

    logger.info(
        "diagnostics_ready log=%s crash_log=%s python=%s platform=%s",
        log_path,
        crash_path,
        platform.python_version(),
        platform.platform(),
    )
    return logger, log_path, crash_path, crash_stream


LOGGER, LOG_FILE, CRASH_LOG_FILE, CRASH_LOG_STREAM = configure_logging()


def log_unhandled_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    LOGGER.critical(
        "unhandled_main_exception",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


def log_thread_exception(args):
    LOGGER.critical(
        "unhandled_thread_exception thread=%s",
        getattr(args.thread, "name", "unknown"),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def log_unraisable_exception(args):
    LOGGER.critical(
        "unraisable_exception object=%r message=%r",
        args.object,
        args.err_msg,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


sys.excepthook = log_unhandled_exception
if hasattr(threading, "excepthook"):
    threading.excepthook = log_thread_exception
if hasattr(sys, "unraisablehook"):
    sys.unraisablehook = log_unraisable_exception

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Pango

# Try to import X11 support
try:
    from gi.repository import GdkX11
    X11_AVAILABLE = True
except ImportError:
    X11_AVAILABLE = False
    LOGGER.warning("gdk_x11_unavailable dock_behavior_may_be_limited")

warnings.filterwarnings("ignore", category=DeprecationWarning)

# Four one-line entries match the four headline rows which define the dock height.
DEFAULT_JOBS_PER_COLUMN = 4
DEFAULT_JOBS_PER_PAGE_WITH_LINK = (DEFAULT_JOBS_PER_COLUMN * 2) - 1
TECH_JOBS_PER_COLUMN = 4
DEFAULT_JOB_LINE_CHARS = 58
TECH_RIGHT_LINE_CHARS = 46
TECH_MIDDLE_LINE_CHARS = 92


def compact_job_text(text, max_chars):
    """Shorten only the position, preserving trailing parenthetical details."""
    text = " ".join(text.split())
    details_start = len(text)
    cursor = len(text)
    while cursor and text[cursor - 1] == ")":
        depth = 0
        opening = None
        for index in range(cursor - 1, -1, -1):
            if text[index] == ")":
                depth += 1
            elif text[index] == "(":
                depth -= 1
                if depth == 0:
                    opening = index
                    break
        if opening is None or (opening and not text[opening - 1].isspace()):
            break
        details_start = opening
        cursor = opening
        while cursor and text[cursor - 1].isspace():
            cursor -= 1
    title = text[:cursor].rstrip()
    details = text[details_start:].strip() if details_start < len(text) else ""
    available = max(8, max_chars - len(details) - (1 if details else 0))
    if len(title) > available:
        cut = title[:max(1, available - 1)].rstrip()
        word_cut = cut.rsplit(" ", 1)[0]
        if len(word_cut) >= max(8, available // 2):
            cut = word_cut
        title = cut + "…"
    return f"{title} {details}".rstrip()


def job_markup(text, max_chars=None):
    return GLib.markup_escape_text(job_display_text(text, max_chars))


def job_display_text(text, max_chars=None):
    display_text = compact_job_text(text, max_chars) if max_chars else " ".join(text.split())
    return f"- {display_text}"


# -----------------------------------------------------------------------------
# JobInRwanda scraper
# -----------------------------------------------------------------------------
def scrape_jobinrwanda_titles(session, url, limit=12, include_employer=False, category_label=None):
    """Return titles with optional category/employer; limit=None includes every page."""
    headers = {"User-Agent": "Mozilla/5.0"}
    titles = []
    visited = set()
    while url and url not in visited:
        visited.add(url)
        r = session.get(url, headers=headers, timeout=(5, 30))
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for article in soup.find_all("article", class_="node--type-job"):
            title_span = article.find("span", class_="field--name-title")
            if title_span:
                title = title_span.get_text(" ", strip=True)
                if category_label:
                    title += f" ({category_label})"
                if include_employer:
                    employer_link = article.select_one('a[href*="/employer/"]')
                    employer = employer_link.get_text(" ", strip=True) if employer_link else ""
                    if employer:
                        title += f" ({employer})"
                titles.append(title)
            if limit is not None and len(titles) >= limit:
                return titles
        if limit is not None:
            break
        next_page = soup.select_one("li.pager__item--next a[href]")
        url = urljoin(url, next_page["href"]) if next_page else None
    return titles


# -----------------------------------------------------------------------------
# Rwanda articles (selenium)
# -----------------------------------------------------------------------------
def scrape_article_content(url):
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    path_to_chromedriver = '/snap/chromium/2897/usr/lib/chromium-browser/chromedriver'
    service = Service(executable_path=path_to_chromedriver)
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.get(url)
    wait = WebDriverWait(driver, 25)
    article_body_text = ""
    try:
        article_body_element = wait.until(
            EC.presence_of_element_located((By.XPATH, "//div[@class='article-body']"))
        )
        article_body_text = article_body_element.text
    finally:
        driver.quit()
    return article_body_text


def scrape_rwanda_article_content(url):
    return scrape_article_content(url)


# -----------------------------------------------------------------------------
# Main UI
# -----------------------------------------------------------------------------
class NewsDock(Gtk.Window):

    def __init__(self):
        super().__init__()

        LOGGER.info(
            "window_init display=%s desktop=%s session_type=%s gdk_backend=%s",
            os.environ.get("DISPLAY", ""),
            os.environ.get("XDG_CURRENT_DESKTOP", ""),
            os.environ.get("XDG_SESSION_TYPE", ""),
            os.environ.get("GDK_BACKEND", ""),
        )

        self.drag_in_progress = False
        self.drag_grab_widget = None
        self.drag_watch_source = None
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.current_monitor = None
        self.dock_workarea = None
        self.closed = False
        self.news_loading = False
        self.jobs_loading = False
        self.default_job_titles = []
        self.default_jobs_page = 0
        self.full_job_text = False
        self.refresh_timer_source = None
        self.action_serial = 0
        self.pending_full_verification = None
        self.pending_it_show_verification = None
        self.pending_it_hide_verification = None
        self.full_verification_source = None
        self.it_confirmation_source = None
        self.it_verification_timeout_source = None
        self.dock_settle_source = None
        self.dock_layout_generation = 0
        self.dock_settle_attempt = 0
        self.paint_serial = 0
        self.action_frame_counters = {}
        self.action_paint_counters = {}
        self.action_started_at = {}
        self.connect("destroy", self.on_destroy)
        self.connect("unmap", self.on_window_unmap)
        self.connect_after("draw", self.on_window_drawn)

        # HTTP session
        self.session = requests.Session()
        self.session.headers.update({
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        })

        # JobInRwanda URLs
        self.jobs_url = (
            "https://www.jobinrwanda.com/jobs/search-result"
            "?filter_titles_field=website&field_job_category_target_id=All"
        )
        self.jobs_international_url = (
            "https://www.jobinrwanda.com/jobs/search-result"
            "?filter_titles_field=&field_job_category_target_id=29"
        )
        self.jobs_economics_url = (
            "https://www.jobinrwanda.com/jobs/search-result"
            "?filter_titles_field=&field_job_category_target_id=13"
        )
        self.jobs_it_url = (
            "https://www.jobinrwanda.com/jobs/search-result"
            "?filter_titles_field=&field_job_category_target_id=33"
        )
        self.it_jobs_loading = False
        self.it_job_titles = []
        self.it_jobs_message = "Loading..."
        self.it_layout_source = None
        self.it_layout_key = None

        self.jobs = []
        self.update_counter = 0
        self.news = {
            section: {"title": "Loading...", "link": ""}
            for section in ("Rwanda", "Tech", "World", "Africa")
        }

        # Build UI
        self.configure_window()
        self.create_news_layout()
        self.create_system_tray_icon()

        # Build the dock immediately; all feeds load outside the GTK thread.
        self.start_news_refresh_timer()

    # -------------------------------------------------------------------------
    # Timer
    # -------------------------------------------------------------------------
    def start_news_refresh_timer(self):
        if self.closed:
            return
        if self.refresh_timer_source is None:
            self.refresh_timer_source = GLib.timeout_add(60 * 1000, self.refresh_news)
            LOGGER.info("refresh_timer_started interval_seconds=60")
        self.refresh_news()

    def on_destroy(self, widget):
        LOGGER.info(
            "window_destroy full_titles=%s it_visible=%s drag_active=%s",
            self.full_job_text,
            self.jobs_it_label.get_visible(),
            self.drag_in_progress,
        )
        self.closed = True
        self.cancel_drag("window-destroyed")
        for source in (
            self.refresh_timer_source,
            self.it_layout_source,
            self.full_verification_source,
            self.it_confirmation_source,
            self.it_verification_timeout_source,
            self.dock_settle_source,
        ):
            if source:
                GLib.source_remove(source)
        self.refresh_timer_source = None
        self.it_layout_source = None
        self.full_verification_source = None
        self.it_confirmation_source = None
        self.it_verification_timeout_source = None
        self.dock_settle_source = None

    def on_window_drawn(self, widget, cairo_context):
        self.paint_serial += 1
        return False

    # -------------------------------------------------------------------------
    # Summarizer (disabled - kept for API compatibility)
    # -------------------------------------------------------------------------
    def summarize_text(self, text, max_output_length=90, max_tokens=1024, chunk_size=900):
        return "_"

    def chunked_summary(self, article, max_output_length=90, max_tokens=1024, chunk_size=800):
        return self.summarize_text(
            article,
            max_output_length=max_output_length,
            max_tokens=max_tokens,
            chunk_size=chunk_size,
        )

    # -------------------------------------------------------------------------
    # Content fetchers (called from arrow buttons)
    # -------------------------------------------------------------------------
    def fetch_tech_content(self, tech_link):
        tech_content = self.fetch_content(
            tech_link, is_tech=True, is_world=False, is_africa=False, is_rwanda=False
        )
        summary_content = self.chunked_summary(tech_content)
        self.clear_loading_message(summary_content)

    def fetch_world_content(self, world_link):
        world_content = self.fetch_content(
            world_link, is_tech=False, is_world=True, is_africa=False, is_rwanda=False
        )
        summary_content = self.chunked_summary(world_content)
        self.clear_loading_message(summary_content)

    def fetch_rwanda_content(self, rwanda_link):
        rwanda_content = self.fetch_content(
            rwanda_link,
            is_tech=False,
            is_world=False,
            is_africa=False,
            is_rwanda=True,
            rwanda_link=rwanda_link,
        )
        summary_content = self.chunked_summary(rwanda_content)
        self.clear_loading_message(summary_content)

    def fetch_africa_content(self, africa_link):
        if "?" in africa_link:
            africa_link = africa_link.split("?", 1)[0]
        africa_content = self.fetch_content(
            africa_link, is_tech=False, is_world=False, is_africa=True, is_rwanda=False
        )
        summary_content = self.chunked_summary(africa_content)
        self.clear_loading_message(summary_content)

    # -------------------------------------------------------------------------
    # Window configuration
    # -------------------------------------------------------------------------
    def configure_window(self):
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_keep_above(True)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.stick()
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("button-press-event", self.on_dock_button_press)
        self.connect("motion-notify-event", self.on_dock_motion)
        self.connect("button-release-event", self.on_dock_button_release)
        self.connect("grab-broken-event", self.on_drag_interrupted)

        monitor = self.get_default_monitor()
        self.current_monitor = monitor
        if monitor:
            geometry = monitor.get_geometry()
            self.set_default_size(geometry.width, -1)

        self.connect("realize", self.on_realize)

    # -------------------------------------------------------------------------
    # Layout creation
    # -------------------------------------------------------------------------
    def create_news_layout(self):
        # CSS
        css_provider = Gtk.CssProvider()
        css_data = b"""
        window {
            background-color: #2e3440;
        }
        label {
            color: #eceff4;
        }
        button {
            background-color: #242933;
            color: #eceff4;
            border: none;
            padding: 2px 6px;
            margin: 0px;
            min-width: 20px;
            min-height: 20px;
            font-size: 10px;
        }
        button:hover {
            background-color: #1a1f28;
        }
        button.it-jobs-toggle {
            background: #242933;
        }
        button.it-jobs-toggle:hover {
            background: #1a1f28;
        }
        button.jobs-read-more {
            background: transparent;
            padding: 0px;
            min-width: 0px;
            min-height: 0px;
            color: #88c0d0;
            text-decoration: underline;
        }
        button.jobs-read-more:hover {
            background: transparent;
            color: #8fbcbb;
        }
        .it-jobs-panel {
            border-left: 1px solid #485264;
        }
        scrolledwindow.jobs-scroll-panel undershoot {
            background-color: transparent;
            background-image: none;
            box-shadow: none;
        }
        .read-button {
            background: #242933;
            margin-left: 15px;
        }
        .read-button:hover {
            background-color: #2e3440;
        }
        button.action-control {
            min-width: 48px;
            min-height: 24px;
            font-size: 11px;
        }
        """
        css_provider.load_from_data(css_data)
        screen = Gdk.Screen.get_default()
        Gtk.StyleContext.add_provider_for_screen(
            screen, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Grid
        self.grid = Gtk.Grid()
        self.grid.set_column_spacing(20)
        self.grid.set_row_spacing(2)
        self.grid.set_margin_top(5)
        self.grid.set_margin_bottom(5)
        self.grid.set_margin_start(10)
        self.grid.set_margin_end(10)

        self.root_overlay = Gtk.Overlay()
        # This remains a non-windowed layout container. Dragging is handled by
        # the top-level window, so it cannot cover or steal button input.
        self.drag_surface = Gtk.Box()

        # Default jobs use one line per item. Four lines are the hard vertical
        # budget because the four news headlines define the dock's height.
        self.jobs_label = Gtk.Label()
        self.jobs_label.set_line_wrap(False)
        self.jobs_label.set_valign(Gtk.Align.START)
        self.jobs_label.set_halign(Gtk.Align.FILL)
        self.jobs_label.set_xalign(0)
        self.jobs_label.set_margin_start(20)
        self.jobs_label.set_max_width_chars(40)
        self.jobs_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.jobs_label.set_hexpand(True)
        self.jobs_label.set_markup("Loading...")

        # The second default column receives entries only after the first fills.
        self.jobs_overflow_label = Gtk.Label()
        self.jobs_overflow_label.set_line_wrap(False)
        self.jobs_overflow_label.set_valign(Gtk.Align.START)
        self.jobs_overflow_label.set_halign(Gtk.Align.START)
        self.jobs_overflow_label.set_xalign(0)
        self.jobs_overflow_label.set_max_width_chars(40)
        self.jobs_overflow_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.jobs_overflow_label.set_hexpand(True)
        self.jobs_overflow_label.set_markup("")
        self.jobs_overflow_label.set_no_show_all(True)  # controlled manually

        self.jobs_read_more = Gtk.Button()
        self.jobs_read_more_label = Gtk.Label()
        self.jobs_read_more_label.set_markup("<u>(read more)</u>")
        self.jobs_read_more.add(self.jobs_read_more_label)
        self.jobs_read_more.set_relief(Gtk.ReliefStyle.NONE)
        self.jobs_read_more.set_halign(Gtk.Align.START)
        self.jobs_read_more.set_valign(Gtk.Align.START)
        self.jobs_read_more.get_style_context().add_class("jobs-read-more")
        self.jobs_read_more.set_tooltip_text("Show the next job listings")
        self.jobs_read_more.set_no_show_all(True)
        self.jobs_read_more.connect("clicked", self.on_jobs_read_more)
        self.jobs_read_more.hide()

        self.default_jobs_overflow_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.default_jobs_overflow_box.set_margin_start(10)
        self.default_jobs_overflow_box.pack_start(
            self.jobs_overflow_label, False, False, 0
        )
        self.default_jobs_overflow_box.pack_start(
            self.jobs_read_more, False, False, 0
        )

        self.default_jobs_middle_panel = Gtk.ScrolledWindow()
        self.default_jobs_middle_panel.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self.default_jobs_middle_panel.set_propagate_natural_width(False)
        self.default_jobs_middle_panel.set_propagate_natural_height(False)
        self.default_jobs_middle_panel.set_vexpand(True)
        self.default_jobs_middle_panel.set_valign(Gtk.Align.FILL)
        self.default_jobs_middle_panel.get_style_context().add_class("jobs-scroll-panel")
        self.default_jobs_middle_panel.add(self.jobs_label)

        self.default_jobs_overflow_panel = Gtk.ScrolledWindow()
        self.default_jobs_overflow_panel.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self.default_jobs_overflow_panel.set_propagate_natural_width(False)
        self.default_jobs_overflow_panel.set_propagate_natural_height(False)
        self.default_jobs_overflow_panel.set_vexpand(True)
        self.default_jobs_overflow_panel.set_valign(Gtk.Align.FILL)
        self.default_jobs_overflow_panel.get_style_context().add_class("jobs-scroll-panel")
        self.default_jobs_overflow_panel.add(self.default_jobs_overflow_box)

        # Keep the normal middle listings underneath an optional IT view, so
        # switching categories preserves their layout and restores them instantly.
        self.default_jobs_grid = Gtk.Grid()
        self.default_jobs_grid.set_column_spacing(20)
        self.default_jobs_grid.set_column_homogeneous(True)
        self.default_jobs_grid.set_vexpand(True)
        self.default_jobs_grid.set_valign(Gtk.Align.FILL)
        self.default_jobs_grid.attach(self.default_jobs_middle_panel, 0, 0, 1, 1)
        self.default_jobs_grid.attach(self.default_jobs_overflow_panel, 1, 0, 1, 1)
        self.middle_jobs_area = Gtk.Overlay()
        self.middle_jobs_area.set_vexpand(True)
        self.middle_jobs_area.set_valign(Gtk.Align.FILL)
        self.middle_jobs_area.add(self.default_jobs_grid)

        self.it_jobs_middle_panel = Gtk.ScrolledWindow()
        self.it_jobs_middle_panel.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.it_jobs_middle_panel.set_propagate_natural_width(False)
        self.it_jobs_middle_panel.set_propagate_natural_height(False)
        self.it_jobs_middle_panel.set_halign(Gtk.Align.END)
        self.it_jobs_middle_panel.set_hexpand(False)
        self.it_jobs_middle_panel.set_vexpand(True)
        self.it_jobs_middle_panel.set_valign(Gtk.Align.FILL)
        self.it_jobs_middle_panel.get_style_context().add_class("jobs-scroll-panel")
        self.jobs_it_middle_label = Gtk.Label()
        self.jobs_it_middle_label.set_line_wrap(False)
        self.jobs_it_middle_label.set_valign(Gtk.Align.START)
        self.jobs_it_middle_label.set_halign(Gtk.Align.FILL)
        self.jobs_it_middle_label.set_xalign(0)
        self.jobs_it_middle_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.jobs_it_middle_label.set_margin_start(20)
        self.jobs_it_middle_label.set_margin_end(10)
        self.it_jobs_middle_panel.add(self.jobs_it_middle_label)
        self.it_jobs_middle_panel.show_all()
        self.it_jobs_middle_panel.set_no_show_all(True)
        self.it_jobs_middle_panel.hide()

        # A lone END-aligned overlay child can retain a 1x1 allocation after
        # Mutter/XWayland moves the dock between differently sized monitors.
        # Give the IT panel a real homogeneous grid cell instead: the first
        # cell preserves the left normal-job column and the second owns the
        # complete middle IT viewport.
        self.it_jobs_middle_overlay = Gtk.Grid()
        self.it_jobs_middle_overlay.set_column_spacing(20)
        self.it_jobs_middle_overlay.set_column_homogeneous(True)
        self.it_jobs_middle_overlay.set_halign(Gtk.Align.FILL)
        self.it_jobs_middle_overlay.set_valign(Gtk.Align.FILL)
        self.it_jobs_middle_overlay.set_hexpand(True)
        self.it_jobs_middle_overlay.set_vexpand(True)
        self.it_jobs_middle_spacer = Gtk.Box()
        self.it_jobs_middle_overlay.attach(self.it_jobs_middle_spacer, 0, 0, 1, 1)
        self.it_jobs_middle_overlay.attach(self.it_jobs_middle_panel, 1, 0, 1, 1)
        self.middle_jobs_area.add_overlay(self.it_jobs_middle_overlay)
        self.middle_jobs_area.connect("size-allocate", self.queue_it_jobs_layout)

        # Reserve column 3 even while its text is hidden. Scrolling contains
        # longer lists without changing the dock's size or pinned position.
        self.it_jobs_column = Gtk.Overlay()
        self.it_jobs_column.set_size_request(340, -1)
        self.it_jobs_column.set_hexpand(False)
        self.it_jobs_column.set_vexpand(True)
        self.it_jobs_column.connect("size-allocate", self.queue_it_jobs_layout)

        self.it_jobs_panel = Gtk.ScrolledWindow()
        self.it_jobs_panel.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.it_jobs_panel.set_propagate_natural_width(False)
        self.it_jobs_panel.set_propagate_natural_height(False)
        self.it_jobs_panel.set_hexpand(False)
        self.it_jobs_panel.set_vexpand(True)
        self.it_jobs_panel.set_valign(Gtk.Align.FILL)
        self.it_jobs_panel.get_style_context().add_class("it-jobs-panel")
        self.it_jobs_panel.get_style_context().add_class("jobs-scroll-panel")

        self.jobs_it_label = Gtk.Label()
        self.jobs_it_label.set_line_wrap(False)
        self.jobs_it_label.set_valign(Gtk.Align.START)
        self.jobs_it_label.set_halign(Gtk.Align.FILL)
        self.jobs_it_label.set_xalign(0)
        self.jobs_it_label.set_hexpand(True)
        self.jobs_it_label.set_margin_start(10)
        # Leave room beside the text for the floating button inside this column.
        self.jobs_it_label.set_margin_end(60)
        self.jobs_it_label.set_max_width_chars(40)
        self.jobs_it_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.jobs_it_label.set_text("Loading...")
        self.jobs_it_label.set_no_show_all(True)
        self.jobs_it_label.hide()
        self.it_jobs_panel.add(self.jobs_it_label)

        self.it_jobs_toggle = Gtk.Button(label="Show")
        self.it_jobs_toggle.set_halign(Gtk.Align.END)
        self.it_jobs_toggle.set_valign(Gtk.Align.END)
        self.it_jobs_toggle.set_margin_end(20)
        self.it_jobs_toggle.set_margin_bottom(10)
        self.it_jobs_toggle.get_style_context().add_class("it-jobs-toggle")
        self.it_jobs_toggle.get_style_context().add_class("action-control")
        self.it_jobs_toggle.set_tooltip_text("Show or hide IT jobs")
        self.it_jobs_toggle.connect("clicked", self.on_it_jobs_toggle)
        self.connect_control_diagnostics(self.it_jobs_toggle, "it-jobs")

        self.jobs_full_toggle = Gtk.Button(label="Full")
        self.jobs_full_toggle.set_halign(Gtk.Align.END)
        self.jobs_full_toggle.set_valign(Gtk.Align.END)
        self.jobs_full_toggle.set_margin_end(78)
        self.jobs_full_toggle.set_margin_bottom(10)
        self.jobs_full_toggle.get_style_context().add_class("action-control")
        self.jobs_full_toggle.set_tooltip_text("Show full job titles in every job column")
        self.jobs_full_toggle.connect("clicked", self.on_full_jobs_toggle)
        self.connect_control_diagnostics(self.jobs_full_toggle, "full-titles")

        self.connect_control_diagnostics(self.jobs_read_more, "read-more")

        self.it_jobs_column.add(self.it_jobs_panel)
        # Direct overlay children are required here. Wrapping these buttons in
        # a non-windowed box makes them render but drops physical pointer input
        # on Mutter/XWayland.
        self.it_jobs_column.add_overlay(self.jobs_full_toggle)
        self.it_jobs_column.set_overlay_pass_through(self.jobs_full_toggle, False)
        self.it_jobs_column.add_overlay(self.it_jobs_toggle)
        self.it_jobs_column.set_overlay_pass_through(self.it_jobs_toggle, False)

        self.add_news_to_grid()
        self.drag_surface.add(self.grid)
        self.root_overlay.add(self.drag_surface)
        self.add(self.root_overlay)

    def connect_control_diagnostics(self, button, control_name):
        button.connect(
            "button-press-event",
            self.on_control_pointer_event,
            control_name,
            "press",
        )
        button.connect(
            "button-release-event",
            self.on_control_pointer_event,
            control_name,
            "release",
        )

    def on_control_pointer_event(self, widget, event, control_name, phase):
        event_widget = Gtk.get_event_widget(event)
        LOGGER.info(
            "control_pointer phase=%s control=%s button=%s root=(%d,%d) "
            "event_widget=%s label=%r visible=%s sensitive=%s mapped=%s "
            "drag_active=%s gtk_grab=%s",
            phase,
            control_name,
            getattr(event, "button", None),
            int(event.x_root),
            int(event.y_root),
            type(event_widget).__name__ if event_widget else "None",
            widget.get_label(),
            widget.get_visible(),
            widget.get_sensitive(),
            widget.get_mapped(),
            self.drag_in_progress,
            type(Gtk.grab_get_current()).__name__ if Gtk.grab_get_current() else "None",
        )
        return False

    # -------------------------------------------------------------------------
    # Populate grid
    # -------------------------------------------------------------------------
    def add_news_to_grid(self):
        for child in self.grid.get_children():
            self.grid.remove(child)

        row_number = 0
        self.headline_labels = {}

        for key, value in self.news.items():
            title_text = value['title']
            title = Gtk.Label.new(f"{key}: {title_text}")
            title.set_line_wrap(False)
            title.set_halign(Gtk.Align.START)
            title.set_max_width_chars(80)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            title.set_hexpand(True)
            title.set_tooltip_text(f"{key}: {title_text}")

            # col 0 = news headlines (no buttons)
            self.grid.attach(title, 0, row_number, 1, 1)
            self.headline_labels[key] = title
            row_number += 1

        row_number = max(1, row_number)
        self.grid.attach(self.middle_jobs_area, 1, 0, 2, row_number)
        self.grid.attach(self.it_jobs_column, 3, 0, 1, row_number)

    # -------------------------------------------------------------------------
    # Jobs panel refresh
    # -------------------------------------------------------------------------
    def refresh_jobs_panel(self):
        if self.closed:
            LOGGER.debug("default_jobs_refresh_skipped reason=window-closed")
            return
        if self.jobs_loading:
            LOGGER.debug("default_jobs_refresh_skipped reason=already-loading")
            return
        self.jobs_loading = True
        LOGGER.info("default_jobs_refresh_started")
        threading.Thread(
            target=self.fetch_jobs,
            daemon=True,
            name="default-jobs-fetch",
        ).start()

    def fetch_jobs(self):
        started = time.monotonic()
        titles = None
        try:
            with requests.Session() as session:
                session.headers.update(self.session.headers)
                titles = []
                for url, category in (
                    (self.jobs_url, "Website"),
                    (self.jobs_international_url, "International relations"),
                    (self.jobs_economics_url, "Economics"),
                ):
                    titles.extend(scrape_jobinrwanda_titles(
                        session, url, limit=6, include_employer=True, category_label=category
                    ))
            if not titles:
                titles.append("Not yet.")

        except Exception:
            titles = None
            LOGGER.exception("default_jobs_fetch_failed")
        LOGGER.info(
            "default_jobs_fetch_finished result_count=%s elapsed_seconds=%.3f",
            len(titles) if titles is not None else "failed",
            time.monotonic() - started,
        )
        GLib.idle_add(self.update_jobs_panel, titles)

    def update_jobs_panel(self, titles):
        self.jobs_loading = False
        if self.closed:
            return False
        self.default_jobs_page = 0
        if titles is not None:
            self.default_job_titles = titles
        else:
            self.default_job_titles = ["Failed to load."]
        self.render_default_jobs_page()
        LOGGER.info(
            "default_jobs_rendered total=%d page=%d full_titles=%s",
            len(self.default_job_titles),
            self.default_jobs_page,
            self.full_job_text,
        )

        self.queue_it_jobs_layout()
        return False

    def render_default_jobs_page(self):
        start = self.default_jobs_page * DEFAULT_JOBS_PER_PAGE_WITH_LINK
        remaining = self.default_job_titles[start:]
        has_more = len(remaining) > DEFAULT_JOBS_PER_COLUMN * 2
        visible_count = (
            DEFAULT_JOBS_PER_PAGE_WITH_LINK
            if has_more else DEFAULT_JOBS_PER_COLUMN * 2
        )
        visible = remaining[:visible_count]
        middle = visible[:DEFAULT_JOBS_PER_COLUMN]
        overflow = visible[DEFAULT_JOBS_PER_COLUMN:]

        max_chars = None if self.full_job_text else DEFAULT_JOB_LINE_CHARS
        self.jobs_label.set_markup("\n".join(
            job_markup(title, max_chars) for title in middle
        ))
        self.jobs_overflow_label.set_markup("\n".join(
            job_markup(title, max_chars) for title in overflow
        ))
        self.jobs_overflow_label.set_visible(bool(overflow))
        self.jobs_read_more.set_visible(has_more)

    def on_jobs_read_more(self, button):
        next_start = (self.default_jobs_page + 1) * DEFAULT_JOBS_PER_PAGE_WITH_LINK
        if next_start < len(self.default_job_titles):
            self.default_jobs_page += 1
            self.render_default_jobs_page()
            LOGGER.info(
                "action_read_more page=%d total=%d",
                self.default_jobs_page,
                len(self.default_job_titles),
            )
        else:
            LOGGER.warning(
                "action_read_more_ignored page=%d next_start=%d total=%d",
                self.default_jobs_page,
                next_start,
                len(self.default_job_titles),
            )

    def next_action_id(self):
        self.action_serial += 1
        action_id = self.action_serial
        frame_clock = self.get_frame_clock()
        self.action_frame_counters[action_id] = (
            frame_clock.get_frame_counter() if frame_clock else -1
        )
        self.action_paint_counters[action_id] = self.paint_serial
        self.action_started_at[action_id] = time.monotonic()
        return action_id

    def action_latency_ms(self, action_id, finish=False):
        started = self.action_started_at.get(action_id, time.monotonic())
        latency = int((time.monotonic() - started) * 1000)
        if finish:
            self.action_started_at.pop(action_id, None)
            self.action_frame_counters.pop(action_id, None)
            self.action_paint_counters.pop(action_id, None)
        return latency

    def rendered_output_painted(self, action_id):
        requested_paint = self.action_paint_counters.get(action_id, -1)
        rendered_paint = self.paint_serial
        requested_frame = self.action_frame_counters.get(action_id, -1)
        frame_clock = self.get_frame_clock()
        rendered_frame = frame_clock.get_frame_counter() if frame_clock else -1
        return (
            requested_paint < 0 or rendered_paint > requested_paint
        ), requested_paint, rendered_paint, requested_frame, rendered_frame

    def visible_output_checks(self, regions):
        """Verify that expected text has a mapped, usable viewport on screen."""
        checks = {}
        metrics = []
        gdk_window = self.get_window()
        checks["window_mapped"] = self.get_mapped()
        checks["window_viewable"] = bool(gdk_window and gdk_window.is_viewable())

        win_width, win_height = self.get_size()
        minimum_viewport_width = min(80, max(40, win_width // 8))
        minimum_viewport_height = min(20, max(10, win_height // 4))
        expected_x = None
        expected_y = None
        actual_x = None
        actual_y = None
        if gdk_window:
            origin = gdk_window.get_origin()
            if len(origin) == 3:
                origin_valid, actual_x, actual_y = origin
                checks["window_origin_valid"] = bool(origin_valid)
            else:
                actual_x, actual_y = origin
                checks["window_origin_valid"] = True
        else:
            checks["window_origin_valid"] = False

        if self.dock_workarea is not None:
            expected_x = self.dock_workarea.x
            expected_y = (
                self.dock_workarea.y
                + self.dock_workarea.height
                - self.headline_height()
            )
            checks["window_on_target_monitor"] = (
                actual_x is not None
                and abs(actual_x - expected_x) <= 2
                and abs(actual_y - expected_y) <= 2
            )

        for name, panel, label, expected_text in regions:
            if not expected_text:
                continue
            position = panel.translate_coordinates(self, 0, 0)
            panel_width = panel.get_allocated_width()
            panel_height = panel.get_allocated_height()
            if position:
                panel_x, panel_y = position
                visible_width = max(
                    0,
                    min(panel_x + panel_width, win_width) - max(panel_x, 0),
                )
                visible_height = max(
                    0,
                    min(panel_y + panel_height, win_height) - max(panel_y, 0),
                )
            else:
                panel_x = panel_y = visible_width = visible_height = 0
            layout = label.get_layout()
            layout_width, layout_height = layout.get_pixel_size() if layout else (0, 0)
            line_count = layout.get_line_count() if layout else 0
            label_width = label.get_allocated_width()
            label_height = label.get_allocated_height()
            checks[f"{name}_mapped"] = panel.get_mapped() and label.get_mapped()
            checks[f"{name}_viewport"] = (
                visible_width >= minimum_viewport_width
                and visible_height >= minimum_viewport_height
            )
            required_label_width = 1
            if name.startswith("it-"):
                required_label_width = max(
                    1,
                    visible_width
                    - label.get_margin_start()
                    - label.get_margin_end()
                    - 2,
                )
            checks[f"{name}_label_allocation"] = (
                label_width >= required_label_width and label_height > 0
            )
            checks[f"{name}_layout"] = (
                layout_width > 0 and layout_height > 0 and line_count > 0
            )
            metrics.append(
                f"{name}:{visible_width}x{visible_height}@{panel_x},{panel_y}"
                f"/label={label_width}x{label_height}>={required_label_width}"
                f"/layout={layout_width}x{layout_height}/lines={line_count}"
            )

        geometry = (
            f"actual={actual_x},{actual_y},{win_width}x{win_height}"
            f"/expected={expected_x},{expected_y}"
        )
        viewport_requirement = (
            f"minimum={minimum_viewport_width}x{minimum_viewport_height}"
        )
        return (
            checks,
            ";".join(metrics) or "none",
            geometry,
            viewport_requirement,
        )

    def expected_default_column_text(self):
        start = self.default_jobs_page * DEFAULT_JOBS_PER_PAGE_WITH_LINK
        remaining = self.default_job_titles[start:]
        has_more = len(remaining) > DEFAULT_JOBS_PER_COLUMN * 2
        visible_count = (
            DEFAULT_JOBS_PER_PAGE_WITH_LINK
            if has_more else DEFAULT_JOBS_PER_COLUMN * 2
        )
        visible = remaining[:visible_count]
        middle = visible[:DEFAULT_JOBS_PER_COLUMN]
        overflow = visible[DEFAULT_JOBS_PER_COLUMN:]
        max_chars = None if self.full_job_text else DEFAULT_JOB_LINE_CHARS
        return (
            "\n".join(job_display_text(title, max_chars) for title in middle),
            "\n".join(job_display_text(title, max_chars) for title in overflow),
        )

    def expected_it_column_text(self):
        if len(self.it_job_titles) <= TECH_JOBS_PER_COLUMN:
            middle_titles = []
            right_titles = self.it_job_titles
        else:
            middle_count = max(
                TECH_JOBS_PER_COLUMN,
                (len(self.it_job_titles) + 1) // 2,
            )
            middle_titles = self.it_job_titles[:middle_count]
            right_titles = self.it_job_titles[middle_count:]
        right_chars = None if self.full_job_text else TECH_RIGHT_LINE_CHARS
        middle_chars = None if self.full_job_text else TECH_MIDDLE_LINE_CHARS
        right = "\n".join(
            job_display_text(title, right_chars) for title in right_titles
        )
        middle = "\n".join(
            job_display_text(title, middle_chars) for title in middle_titles
        )
        if self.it_jobs_message:
            right += ("\n" if right else "") + self.it_jobs_message
        return middle, right, len(middle_titles), len(right_titles)

    def schedule_full_action_verification(
        self,
        action_id,
        requested_state,
        attempt=0,
        delay_ms=50,
    ):
        if self.full_verification_source is not None:
            GLib.source_remove(self.full_verification_source)
        self.full_verification_source = GLib.timeout_add(
            delay_ms,
            self.verify_full_action,
            action_id,
            requested_state,
            attempt,
        )

    def verify_full_action(self, action_id, requested_state, attempt=0):
        self.full_verification_source = None
        if self.closed or self.pending_full_verification != action_id:
            return False

        waiting_for_it = (
            self.jobs_it_label.get_visible()
            and self.it_jobs_message == "Loading..."
            and not self.it_job_titles
        )
        if waiting_for_it and self.action_latency_ms(action_id) < 35000:
            if attempt == 0:
                LOGGER.info(
                    "action_verification_pending id=%d control=full-titles "
                    "reason=visible-it-results-loading",
                    action_id,
                )
            self.schedule_full_action_verification(
                action_id,
                requested_state,
                attempt,
                delay_ms=250,
            )
            return False

        expected_button = "Short" if requested_state else "Full"
        expected_ellipsize = (
            Pango.EllipsizeMode.NONE
            if requested_state else Pango.EllipsizeMode.MIDDLE
        )
        labels = (
            self.jobs_label,
            self.jobs_overflow_label,
            self.jobs_it_middle_label,
            self.jobs_it_label,
        )
        default_middle, default_overflow = self.expected_default_column_text()
        regions = []
        if self.default_jobs_grid.get_opacity() > 0:
            regions.extend([
                (
                    "default-middle",
                    self.default_jobs_middle_panel,
                    self.jobs_label,
                    default_middle,
                ),
                (
                    "default-overflow",
                    self.default_jobs_overflow_panel,
                    self.jobs_overflow_label,
                    default_overflow,
                ),
            ])
        checks = {
            "state": self.full_job_text == requested_state,
            "button": self.jobs_full_toggle.get_label() == expected_button,
            "wrap": all(label.get_line_wrap() == requested_state for label in labels),
            "ellipsize": all(
                label.get_ellipsize() == expected_ellipsize for label in labels
            ),
            "default_middle_text": self.jobs_label.get_text() == default_middle,
            "default_overflow_text": (
                self.jobs_overflow_label.get_text() == default_overflow
            ),
        }
        it_middle_count = 0
        it_right_count = 0
        if self.jobs_it_label.get_visible():
            expected_middle, expected_right, it_middle_count, it_right_count = (
                self.expected_it_column_text()
            )
            regions.extend([
                (
                    "it-middle",
                    self.it_jobs_middle_panel,
                    self.jobs_it_middle_label,
                    expected_middle,
                ),
                (
                    "it-right",
                    self.it_jobs_panel,
                    self.jobs_it_label,
                    expected_right,
                ),
            ])
            checks.update({
                "it_middle_text": self.jobs_it_middle_label.get_text() == expected_middle,
                "it_right_text": self.jobs_it_label.get_text() == expected_right,
                "it_middle_visible": (
                    self.it_jobs_middle_panel.get_visible() == bool(expected_middle)
                ),
            })
            checks["it_data_available"] = not bool(
                self.it_jobs_message == "Loading..."
                or (
                    self.it_jobs_message
                    and self.it_jobs_message.startswith("Failed to load")
                )
            )

        onscreen_checks, viewport_metrics, window_geometry, viewport_requirement = (
            self.visible_output_checks(regions)
        )
        checks.update(onscreen_checks)
        painted, requested_paint, rendered_paint, requested_frame, rendered_frame = (
            self.rendered_output_painted(action_id)
        )
        checks["painted"] = painted

        failed_checks = [name for name, passed in checks.items() if not passed]
        if failed_checks and attempt < 5:
            self.request_output_repaint(regions)
            self.schedule_full_action_verification(
                action_id,
                requested_state,
                attempt + 1,
            )
            return False

        self.pending_full_verification = None
        latency_ms = self.action_latency_ms(action_id, finish=True)
        if failed_checks:
            LOGGER.error(
                "action_confirmed id=%d control=full-titles result=failure "
                "requested_state=%s failed_checks=%s button_label=%r "
                "default_items=%d it_items=%d latency_ms=%d window=%s "
                "viewports=%s requirement=%s paints=%s->%s frames=%s->%s",
                action_id,
                "full" if requested_state else "short",
                ",".join(failed_checks),
                self.jobs_full_toggle.get_label(),
                len(self.default_job_titles),
                len(self.it_job_titles),
                latency_ms,
                window_geometry,
                viewport_metrics,
                viewport_requirement,
                requested_paint,
                rendered_paint,
                requested_frame,
                rendered_frame,
            )
        else:
            LOGGER.info(
                "action_confirmed id=%d control=full-titles result=success "
                "rendered_state=%s button_label=%r wrap=%s ellipsize=%s "
                "default_items=%d it_items=%d it_split=%d+%d onscreen=True "
                "latency_ms=%d window=%s viewports=%s requirement=%s "
                "paints=%s->%s frames=%s->%s",
                action_id,
                "full" if requested_state else "short",
                self.jobs_full_toggle.get_label(),
                requested_state,
                "none" if requested_state else "middle",
                len(self.default_job_titles),
                len(self.it_job_titles),
                it_middle_count,
                it_right_count,
                latency_ms,
                window_geometry,
                viewport_metrics,
                viewport_requirement,
                requested_paint,
                rendered_paint,
                requested_frame,
                rendered_frame,
            )
        return False

    def cancel_it_show_verification(self, result=None):
        pending = self.pending_it_show_verification
        self.pending_it_show_verification = None
        if self.it_confirmation_source is not None:
            GLib.source_remove(self.it_confirmation_source)
            self.it_confirmation_source = None
        if self.it_verification_timeout_source is not None:
            GLib.source_remove(self.it_verification_timeout_source)
            self.it_verification_timeout_source = None
        if pending is not None and result:
            latency_ms = self.action_latency_ms(pending, finish=True)
            LOGGER.info(
                "action_confirmed id=%d control=it-jobs result=%s "
                "requested_state=shown latency_ms=%d",
                pending,
                result,
                latency_ms,
            )

    def schedule_it_show_confirmation(self, action_id, attempt=0, delay_ms=50):
        if self.pending_it_hide_verification is not None:
            superseded = self.pending_it_hide_verification
            self.pending_it_hide_verification = None
            LOGGER.info(
                "action_confirmed id=%d control=it-jobs result=superseded "
                "requested_state=hidden latency_ms=%d",
                superseded,
                self.action_latency_ms(superseded, finish=True),
            )
        if self.it_confirmation_source is not None:
            GLib.source_remove(self.it_confirmation_source)
        self.it_confirmation_source = GLib.timeout_add(
            delay_ms,
            self.confirm_it_show_layout,
            action_id,
            attempt,
        )

    def verify_it_show_timeout(self, action_id):
        self.it_verification_timeout_source = None
        if self.pending_it_show_verification == action_id:
            self.pending_it_show_verification = None
            latency_ms = self.action_latency_ms(action_id, finish=True)
            LOGGER.error(
                "action_confirmed id=%d control=it-jobs result=failure "
                "requested_state=shown reason=verification-timeout loading=%s "
                "cached_items=%d latency_ms=%d",
                action_id,
                self.it_jobs_loading,
                len(self.it_job_titles),
                latency_ms,
            )
        return False

    def confirm_it_show_layout(self, action_id, attempt=0):
        self.it_confirmation_source = None
        if self.pending_it_show_verification != action_id:
            return False
        if self.it_jobs_message == "Loading...":
            return False

        expected_middle, expected_right, middle_count, right_count = (
            self.expected_it_column_text()
        )
        expects_middle = bool(expected_middle)
        expected_ellipsize = (
            Pango.EllipsizeMode.NONE
            if self.full_job_text else Pango.EllipsizeMode.MIDDLE
        )
        visible_labels = [self.jobs_it_label]
        regions = [
            (
                "it-right",
                self.it_jobs_panel,
                self.jobs_it_label,
                expected_right,
            ),
        ]
        if expects_middle:
            visible_labels.append(self.jobs_it_middle_label)
            regions.append((
                "it-middle",
                self.it_jobs_middle_panel,
                self.jobs_it_middle_label,
                expected_middle,
            ))
        checks = {
            "button": self.it_jobs_toggle.get_label() == "Hide",
            "right_visible": self.jobs_it_label.get_visible(),
            "middle_visible": (
                self.it_jobs_middle_panel.get_visible() == expects_middle
            ),
            "default_opacity": (
                self.default_jobs_grid.get_opacity() == (0 if expects_middle else 1)
            ),
            "default_sensitive": (
                self.default_jobs_grid.get_sensitive() == (not expects_middle)
            ),
            "middle_text": self.jobs_it_middle_label.get_text() == expected_middle,
            "right_text": self.jobs_it_label.get_text() == expected_right,
            "wrap": all(
                label.get_line_wrap() == self.full_job_text
                for label in visible_labels
            ),
            "ellipsize": all(
                label.get_ellipsize() == expected_ellipsize
                for label in visible_labels
            ),
        }
        onscreen_checks, viewport_metrics, window_geometry, viewport_requirement = (
            self.visible_output_checks(regions)
        )
        checks.update(onscreen_checks)
        painted, requested_paint, rendered_paint, requested_frame, rendered_frame = (
            self.rendered_output_painted(action_id)
        )
        checks["painted"] = painted
        load_failed = bool(
            self.it_jobs_message and self.it_jobs_message.startswith("Failed to load")
        )
        failed_checks = [name for name, passed in checks.items() if not passed]
        if failed_checks and not load_failed and attempt < 5:
            self.request_output_repaint(regions)
            self.schedule_it_show_confirmation(action_id, attempt + 1)
            return False

        self.cancel_it_show_verification()
        latency_ms = self.action_latency_ms(action_id, finish=True)
        if failed_checks or load_failed:
            LOGGER.error(
                "action_confirmed id=%d control=it-jobs result=failure "
                "requested_state=shown reason=%s failed_checks=%s items=%d "
                "message=%r latency_ms=%d window=%s viewports=%s "
                "requirement=%s paints=%s->%s frames=%s->%s",
                action_id,
                "load-failed" if load_failed else "render-mismatch",
                ",".join(failed_checks) or "none",
                len(self.it_job_titles),
                self.it_jobs_message,
                latency_ms,
                window_geometry,
                viewport_metrics,
                viewport_requirement,
                requested_paint,
                rendered_paint,
                requested_frame,
                rendered_frame,
            )
        else:
            LOGGER.info(
                "action_confirmed id=%d control=it-jobs result=success "
                "rendered_state=shown button_label=%r items=%d split=%d+%d "
                "full_titles=%s right_visible=%s middle_visible=%s "
                "onscreen=True latency_ms=%d window=%s viewports=%s "
                "requirement=%s paints=%s->%s frames=%s->%s",
                action_id,
                self.it_jobs_toggle.get_label(),
                len(self.it_job_titles),
                middle_count,
                right_count,
                self.full_job_text,
                self.jobs_it_label.get_visible(),
                self.it_jobs_middle_panel.get_visible(),
                latency_ms,
                window_geometry,
                viewport_metrics,
                viewport_requirement,
                requested_paint,
                rendered_paint,
                requested_frame,
                rendered_frame,
            )
        return False

    def schedule_it_hide_confirmation(self, action_id, attempt=0, delay_ms=50):
        self.pending_it_hide_verification = action_id
        if self.it_confirmation_source is not None:
            GLib.source_remove(self.it_confirmation_source)
        self.it_confirmation_source = GLib.timeout_add(
            delay_ms,
            self.confirm_it_hide_action,
            action_id,
            attempt,
        )

    def confirm_it_hide_action(self, action_id, attempt=0):
        self.it_confirmation_source = None
        if self.pending_it_hide_verification != action_id:
            return False
        default_middle, default_overflow = self.expected_default_column_text()
        regions = [
            (
                "default-middle",
                self.default_jobs_middle_panel,
                self.jobs_label,
                default_middle,
            ),
            (
                "default-overflow",
                self.default_jobs_overflow_panel,
                self.jobs_overflow_label,
                default_overflow,
            ),
        ]
        checks = {
            "button": self.it_jobs_toggle.get_label() == "Show",
            "right_hidden": not self.jobs_it_label.get_visible(),
            "middle_hidden": not self.it_jobs_middle_panel.get_visible(),
            "defaults_visible": self.default_jobs_grid.get_opacity() == 1,
            "defaults_sensitive": self.default_jobs_grid.get_sensitive(),
            "default_middle_text": self.jobs_label.get_text() == default_middle,
            "default_overflow_text": (
                self.jobs_overflow_label.get_text() == default_overflow
            ),
        }
        onscreen_checks, viewport_metrics, window_geometry, viewport_requirement = (
            self.visible_output_checks(regions)
        )
        checks.update(onscreen_checks)
        painted, requested_paint, rendered_paint, requested_frame, rendered_frame = (
            self.rendered_output_painted(action_id)
        )
        checks["painted"] = painted
        failed_checks = [name for name, passed in checks.items() if not passed]
        if failed_checks and attempt < 5:
            self.request_output_repaint(regions)
            self.schedule_it_hide_confirmation(action_id, attempt + 1)
            return False

        self.pending_it_hide_verification = None
        latency_ms = self.action_latency_ms(action_id, finish=True)
        if failed_checks:
            LOGGER.error(
                "action_confirmed id=%d control=it-jobs result=failure "
                "requested_state=hidden failed_checks=%s latency_ms=%d "
                "window=%s viewports=%s requirement=%s paints=%s->%s "
                "frames=%s->%s",
                action_id,
                ",".join(failed_checks),
                latency_ms,
                window_geometry,
                viewport_metrics,
                viewport_requirement,
                requested_paint,
                rendered_paint,
                requested_frame,
                rendered_frame,
            )
        else:
            LOGGER.info(
                "action_confirmed id=%d control=it-jobs result=success "
                "rendered_state=hidden button_label=%r cached_items=%d "
                "defaults_opacity=%.1f onscreen=True latency_ms=%d "
                "window=%s viewports=%s requirement=%s paints=%s->%s "
                "frames=%s->%s",
                action_id,
                self.it_jobs_toggle.get_label(),
                len(self.it_job_titles),
                self.default_jobs_grid.get_opacity(),
                latency_ms,
                window_geometry,
                viewport_metrics,
                viewport_requirement,
                requested_paint,
                rendered_paint,
                requested_frame,
                rendered_frame,
            )
        return False

    def on_full_jobs_toggle(self, button):
        if self.pending_full_verification is not None:
            superseded = self.pending_full_verification
            self.pending_full_verification = None
            if self.full_verification_source is not None:
                GLib.source_remove(self.full_verification_source)
                self.full_verification_source = None
            LOGGER.info(
                "action_confirmed id=%d control=full-titles result=superseded "
                "latency_ms=%d",
                superseded,
                self.action_latency_ms(superseded, finish=True),
            )
        action_id = self.next_action_id()
        self.full_job_text = not self.full_job_text
        button.set_label("Short" if self.full_job_text else "Full")
        button.set_tooltip_text(
            "Use compact job titles"
            if self.full_job_text
            else "Show full job titles in every job column"
        )
        LOGGER.info(
            "action_requested id=%d control=full-titles requested_state=%s "
            "button_label=%s it_visible=%s",
            action_id,
            "full" if self.full_job_text else "short",
            button.get_label(),
            self.jobs_it_label.get_visible(),
        )
        for label in (
            self.jobs_label,
            self.jobs_overflow_label,
            self.jobs_it_middle_label,
            self.jobs_it_label,
        ):
            label.set_line_wrap(self.full_job_text)
            if self.full_job_text:
                label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_ellipsize(
                Pango.EllipsizeMode.NONE
                if self.full_job_text else Pango.EllipsizeMode.MIDDLE
            )
        self.render_default_jobs_page()
        self.it_layout_key = None
        self.queue_it_jobs_layout()
        self.pending_full_verification = action_id
        self.queue_draw()
        self.schedule_full_action_verification(action_id, self.full_job_text)

    def on_it_jobs_toggle(self, button):
        if self.pending_it_hide_verification is not None:
            superseded = self.pending_it_hide_verification
            self.pending_it_hide_verification = None
            if self.it_confirmation_source is not None:
                GLib.source_remove(self.it_confirmation_source)
                self.it_confirmation_source = None
            LOGGER.info(
                "action_confirmed id=%d control=it-jobs result=superseded "
                "requested_state=hidden latency_ms=%d",
                superseded,
                self.action_latency_ms(superseded, finish=True),
            )
        action_id = self.next_action_id()
        visible = not self.jobs_it_label.get_visible()
        self.jobs_it_label.set_visible(visible)
        button.set_label("Hide" if visible else "Show")
        button.set_tooltip_text("Hide IT jobs" if visible else "Show IT jobs")
        LOGGER.info(
            "action_requested id=%d control=it-jobs requested_state=%s "
            "cached_count=%d loading=%s",
            action_id,
            "shown" if visible else "hidden",
            len(self.it_job_titles),
            self.it_jobs_loading,
        )
        if visible:
            self.cancel_it_show_verification(result="superseded")
            self.pending_it_show_verification = action_id
            self.it_verification_timeout_source = GLib.timeout_add_seconds(
                35,
                self.verify_it_show_timeout,
                action_id,
            )
            self.queue_it_jobs_layout()
            self.refresh_it_jobs_panel()
        else:
            self.cancel_it_show_verification(result="superseded")
            self.it_layout_key = None
            self.set_it_columns(self.jobs_it_label.get_label())
            self.queue_resize()
            self.queue_draw()
            self.schedule_it_hide_confirmation(action_id)

    def refresh_it_jobs_panel(self):
        if self.closed:
            LOGGER.debug("it_jobs_refresh_skipped reason=window-closed")
            return
        if self.it_jobs_loading:
            LOGGER.info("it_jobs_refresh_skipped reason=already-loading")
            return
        self.it_jobs_loading = True
        LOGGER.info("it_jobs_refresh_started visible=%s", self.jobs_it_label.get_visible())
        threading.Thread(
            target=self.fetch_it_jobs,
            daemon=True,
            name="it-jobs-fetch",
        ).start()

    def fetch_it_jobs(self):
        # Keep the Show/Hide button responsive while this category loads.
        started = time.monotonic()
        titles = []
        message = None
        try:
            with requests.Session() as session:
                titles = scrape_jobinrwanda_titles(
                    session, self.jobs_it_url, limit=None, include_employer=True
                )
            if not titles:
                message = "- No IT jobs found."
        except Exception:
            message = "Failed to load. Hide and show to retry."
            LOGGER.exception("it_jobs_fetch_failed")
        LOGGER.info(
            "it_jobs_fetch_finished result_count=%d message=%r elapsed_seconds=%.3f",
            len(titles),
            message,
            time.monotonic() - started,
        )
        GLib.idle_add(self.update_it_jobs_panel, titles, message)

    def update_it_jobs_panel(self, titles, message=None):
        self.it_jobs_loading = False
        if self.closed:
            return False
        self.it_job_titles = titles
        self.it_jobs_message = message
        LOGGER.info(
            "it_jobs_result_applied count=%d message=%r visible=%s",
            len(titles),
            message,
            self.jobs_it_label.get_visible(),
        )
        self.queue_it_jobs_layout()
        return False

    def queue_it_jobs_layout(self, *args):
        if not self.closed and self.jobs_it_label.get_visible() and self.it_layout_source is None:
            self.it_layout_source = GLib.idle_add(self.layout_it_jobs)

    @staticmethod
    def format_it_jobs(titles, max_chars=None):
        return "\n".join(job_markup(title, max_chars) for title in titles)

    def set_it_columns(self, right_markup, middle_markup=None):
        # Opacity keeps the default listings' size reserved without exposing
        # their text underneath IT. They continue refreshing while covered.
        self.default_jobs_grid.set_opacity(0 if middle_markup else 1)
        self.default_jobs_grid.set_sensitive(not bool(middle_markup))
        self.it_jobs_middle_panel.set_visible(bool(middle_markup))
        for label, markup in (
            (self.jobs_it_label, right_markup),
            (self.jobs_it_middle_label, middle_markup or ""),
        ):
            if label.get_label() != markup:
                label.set_markup(markup)
        self.it_jobs_middle_overlay.queue_resize()
        self.middle_jobs_area.queue_resize()
        self.it_jobs_column.queue_resize()
        self.request_output_repaint()

    def layout_it_jobs(self):
        self.it_layout_source = None
        if self.closed or not self.jobs_it_label.get_visible():
            return False

        layout_key = (
            tuple(self.it_job_titles),
            self.it_jobs_message,
            self.full_job_text,
        )
        if layout_key == self.it_layout_key:
            return False
        self.it_layout_key = layout_key

        # Up to four entries live only in the last column. Once the list needs
        # both columns, lay it out in natural left-to-right reading order and
        # keep at least four entries in the middle before continuing right.
        if len(self.it_job_titles) <= TECH_JOBS_PER_COLUMN:
            middle_titles = []
            right_titles = self.it_job_titles
        else:
            middle_count = max(
                TECH_JOBS_PER_COLUMN,
                (len(self.it_job_titles) + 1) // 2,
            )
            middle_titles = self.it_job_titles[:middle_count]
            right_titles = self.it_job_titles[middle_count:]
        right_chars = None if self.full_job_text else TECH_RIGHT_LINE_CHARS
        middle_chars = None if self.full_job_text else TECH_MIDDLE_LINE_CHARS
        right = self.format_it_jobs(right_titles, right_chars)
        middle = self.format_it_jobs(middle_titles, middle_chars)
        if self.it_jobs_message:
            right += ("\n" if right else "") + GLib.markup_escape_text(self.it_jobs_message)
        self.set_it_columns(right, middle or None)
        LOGGER.info(
            "it_jobs_layout_rendered total=%d middle=%d right=%d full_titles=%s "
            "message=%r",
            len(self.it_job_titles),
            len(middle_titles),
            len(right_titles),
            self.full_job_text,
            self.it_jobs_message,
        )
        if self.pending_it_show_verification is not None:
            self.queue_resize()
            self.queue_draw()
            self.schedule_it_show_confirmation(self.pending_it_show_verification)
        return False

    # -------------------------------------------------------------------------
    # Loading / clear stubs (kept for API compatibility)
    # -------------------------------------------------------------------------
    def on_clear_click(self, button):
        self.loading_label.set_text("")
        self.content_label.set_text("")

    def display_loading_message(self):
        pass

    def clear_loading_message(self, summary_content):
        pass

    # -------------------------------------------------------------------------
    # Arrow button handler
    # -------------------------------------------------------------------------
    def on_arrow_click(self, widget, section_key):
        LOGGER.info("action_article_open section=%s", section_key)
        self.display_loading_message()
        content = self.news.get(section_key, "")
        if not isinstance(content, dict):
            return

        link = content.get("link", "")
        dispatch = {
            "Tech":   self.fetch_tech_content,
            "World":  self.fetch_world_content,
            "Rwanda": self.fetch_rwanda_content,
            "Africa": self.fetch_africa_content,
        }
        handler = dispatch.get(section_key)
        if handler:
            GObject.timeout_add(100, handler, link)

    # -------------------------------------------------------------------------
    # News display helpers
    # -------------------------------------------------------------------------
    def update_news_display(self, headlines):
        self.news_loading = False
        if self.closed or headlines is None:
            LOGGER.info(
                "news_result_discarded closed=%s result_is_none=%s",
                self.closed,
                headlines is None,
            )
            return False
        self.news = headlines
        for key, value in headlines.items():
            text = f"{key}: {value['title']}"
            self.headline_labels[key].set_text(text)
            self.headline_labels[key].set_tooltip_text(text)
        LOGGER.info("news_result_applied sections=%d", len(headlines))
        return False

    def refresh_news(self):
        if self.closed:
            return False
        LOGGER.info(
            "refresh_cycle_requested cycle=%d news_loading=%s jobs_loading=%s "
            "it_visible=%s it_loading=%s",
            self.update_counter + 1,
            self.news_loading,
            self.jobs_loading,
            self.jobs_it_label.get_visible(),
            self.it_jobs_loading,
        )
        if not self.news_loading:
            self.news_loading = True
            self.update_counter += 1
            threading.Thread(
                target=self.fetch_and_display_news,
                daemon=True,
                name="news-fetch",
            ).start()
        self.refresh_jobs_panel()
        if self.jobs_it_label.get_visible():
            self.refresh_it_jobs_panel()
        return True

    def fetch_and_display_news(self):
        started = time.monotonic()
        headlines = None
        try:
            with requests.Session() as session:
                session.headers.update(self.session.headers)
                headlines = self.fetch_news(session)
        except Exception:
            LOGGER.exception("news_refresh_failed")
        LOGGER.info(
            "news_fetch_finished sections=%s elapsed_seconds=%.3f",
            len(headlines) if headlines is not None else "failed",
            time.monotonic() - started,
        )
        GLib.idle_add(self.update_news_display, headlines)

    # -------------------------------------------------------------------------
    # News fetcher
    # -------------------------------------------------------------------------
    def fetch_news(self, session=None):
        session = session or self.session
        parsers = {
            "Rwanda": ("https://www.newtimes.co.rw/",          self.rwanda_parser),
            "Tech":   ("https://techcrunch.com/",               self.tech_parser),
            "World":  ("https://www.bbc.com/news/",             self.world_parser),
            "Africa": ("https://www.bbc.com/news/world/africa", self.africa_parser),
        }
        news = {}
        for key, (url, parser) in parsers.items():
            try:
                response = session.get(url, timeout=(5, 30))
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                content = parser(soup)
                news[key] = {
                    "title": content.get("title", ""),
                    "link":  content.get("link",  ""),
                }
            except requests.exceptions.RequestException:
                LOGGER.exception("news_section_fetch_failed section=%s url=%s", key, url)
                news[key] = {"title": "Failed to fetch news.", "link": ""}
        return news

    # -------------------------------------------------------------------------
    # Parsers
    # -------------------------------------------------------------------------
    def tech_parser(self, soup=None):
        url = "https://techcrunch.com/"
        if soup is None:
            response = requests.get(url)
            if response.status_code == 200:
                soup = BeautifulSoup(response.content, 'html.parser')
            else:
                return {"title": "Failed to retrieve the page.", "link": ""}

        post_picker_div = soup.find('div', class_='wp-block-techcrunch-card')
        if post_picker_div:
            title_element = post_picker_div.find('h3', class_='loop-card__title')
            if title_element and title_element.a:
                title = title_element.get_text(strip=True)
                link  = title_element.a['href']
            else:
                title = "Title not found."
                link  = ""
        else:
            title = "Post picker content not found."
            link  = ""
        return {"title": title, "link": link}

    def world_parser(self, soup):
        try:
            container = soup.find_all('h2', {'data-testid': 'card-headline'})
            if not container:
                return {"title": "BBC container not found.", "link": ""}

            for h2_tag in container:
                headline_text = h2_tag.get_text(strip=True)
                parent_div = h2_tag.find_parent('div')
                while parent_div:
                    link_tag = parent_div.find('a', href=True)
                    if link_tag:
                        link = link_tag['href']
                        if not link.startswith('http'):
                            link = "https://www.bbc.com" + link
                        return {"title": headline_text, "link": link}
                    parent_div = parent_div.find_parent('div')

            return {"title": "Target headline not found.", "link": ""}
        except Exception:
            LOGGER.exception("world_news_parse_failed")
            return {"title": "Error occurred while parsing.", "link": ""}

    def africa_parser(self, soup):
        return self.world_parser(soup)

    def rwanda_parser(self, soup):
        link_element = soup.find('div', class_='article-title')
        if link_element:
            title = link_element.text.strip()
            anchor_tags = soup.select('.nt-home-tabs .article-title a')
            if anchor_tags:
                link = anchor_tags[0]['href']
                return {"title": title, "link": link}
            return {"title": title, "link": "Link not found for the article."}
        return {"title": "Article title not found.", "link": ""}

    # -------------------------------------------------------------------------
    # Article content fetcher
    # -------------------------------------------------------------------------
    def fetch_content(
        self,
        link,
        is_tech=False,
        rwanda_link=None,
        is_world=False,
        is_africa=False,
        is_rwanda=False,
    ):
        url = link
        response = self.session.get(url)
        if response.status_code != 200:
            return "Failed to fetch content."

        soup = BeautifulSoup(response.content, "html.parser")
        time.sleep(2)

        if is_tech:
            container = soup.find(
                'div',
                class_=(
                    'entry-content wp-block-post-content is-layout-flow '
                    'wp-block-post-content-is-layout-flow'
                ),
            )
            if container:
                paragraphs = container.find_all('p')
                content = "\n".join(p.get_text() for p in paragraphs)
            else:
                content = "Elements not found."

        elif is_rwanda:
            content = scrape_rwanda_article_content(link) if rwanda_link else ""

        elif is_world:
            if '/live/' in url:
                container = soup.find('section', class_='qa-summary-points')
                if container:
                    items = container.find_all('li', class_='lx-c-summary-points__item')
                    content = '\n'.join(item.get_text().strip() for item in items)
                else:
                    content = "Elements not found."
            else:
                container = soup.find(id='main-content')
                if container:
                    text_blocks = container.find_all(
                        'div', attrs={'data-component': 'text-block'}
                    )
                    content = '\n'.join(
                        p.get_text()
                        for block in text_blocks
                        for p in block.find_all('p')
                    )
                else:
                    content = "Elements not found."

        elif is_africa:
            if '/live/' in url:
                container = soup.find('section', class_='qa-summary-points')
                if container:
                    items = container.find_all('li', class_='lx-c-summary-points__item')
                    content = '\n'.join(item.get_text().strip() for item in items)
                else:
                    content = "Elements not found."
            else:
                main_content = soup.find('main', id='main-content')
                if main_content:
                    text_blocks = main_content.find_all(
                        'div', attrs={'data-component': 'text-block'}
                    )
                    content = '\n'.join(
                        p.get_text()
                        for block in text_blocks
                        for p in block.find_all('p')
                    )
                else:
                    content = "Elements not found."

        else:
            content = "Invalid category."

        return content

    # -------------------------------------------------------------------------
    # Window positioning / strut
    # -------------------------------------------------------------------------
    def get_default_monitor(self):
        display = Gdk.Display.get_default()
        if not display:
            return None
        return display.get_primary_monitor() or display.get_monitor(0)

    def get_monitor_for_point(self, x_root, y_root):
        display = Gdk.Display.get_default()
        if not display:
            return None

        for monitor_index in range(display.get_n_monitors()):
            monitor = display.get_monitor(monitor_index)
            geometry = monitor.get_geometry()
            if (
                geometry.x <= x_root < geometry.x + geometry.width
                and geometry.y <= y_root < geometry.y + geometry.height
            ):
                return monitor

        return self.get_default_monitor()

    def get_monitor_for_window(self):
        x_pos, y_pos = self.get_position()
        width, height = self.get_size()
        center_x = x_pos + (width // 2)
        center_y = y_pos + (height // 2)
        return self.get_monitor_for_point(center_x, center_y)

    def on_realize(self, widget):
        gdk_window = self.get_window()
        xid = gdk_window.get_xid() if X11_AVAILABLE and gdk_window else None
        LOGGER.info("window_realized xid=%s", xid)
        GLib.idle_add(self.resize_to_fit_content)
        GLib.timeout_add(1000, self.log_control_geometry)

    def log_control_geometry(self):
        if self.closed or not self.get_mapped():
            return False
        win_x, win_y = self.get_position()
        win_width, win_height = self.get_size()
        LOGGER.info(
            "window_geometry root=(%d,%d) size=%dx%d gtk_grab=%s drag_active=%s",
            win_x,
            win_y,
            win_width,
            win_height,
            type(Gtk.grab_get_current()).__name__ if Gtk.grab_get_current() else "None",
            self.drag_in_progress,
        )
        for name, widget in (
            ("Full", self.jobs_full_toggle),
            ("Show", self.it_jobs_toggle),
        ):
            position = widget.translate_coordinates(self, 0, 0)
            if position:
                x_pos, y_pos = position
                LOGGER.info(
                    "control_geometry control=%s root=(%d,%d) size=%dx%d "
                    "visible=%s sensitive=%s mapped=%s",
                    name.lower(),
                    win_x + x_pos,
                    win_y + y_pos,
                    widget.get_allocated_width(),
                    widget.get_allocated_height(),
                    widget.get_visible(),
                    widget.get_sensitive(),
                    widget.get_mapped(),
                )
        return False

    def resize_to_fit_content(self):
        if not self.closed:
            self.dock_to_monitor(self.current_monitor)
        return False

    def headline_height(self):
        labels = list(self.headline_labels.values())
        content_height = sum(label.get_preferred_height()[1] for label in labels)
        spacing = self.grid.get_row_spacing() * max(0, len(labels) - 1)
        return (
            self.grid.get_margin_top()
            + content_height
            + spacing
            + self.grid.get_margin_bottom()
        )

    def size_job_columns(self, work_width):
        margins = self.grid.get_margin_start() + self.grid.get_margin_end()
        spacing = self.grid.get_column_spacing()
        usable_width = max(1, work_width - margins - (spacing * 3))
        column_width = max(120, usable_width // 4)
        middle_width = (column_width * 2) + spacing
        self.middle_jobs_area.set_size_request(middle_width, -1)
        self.it_jobs_middle_overlay.set_size_request(middle_width, -1)
        self.it_jobs_middle_panel.set_size_request(column_width, -1)
        self.it_jobs_column.set_size_request(column_width, -1)
        return column_width, middle_width

    def request_output_repaint(self, regions=()):
        """Force GTK and X11 to allocate and paint after an XWayland move."""
        widgets = (
            self.root_overlay,
            self.drag_surface,
            self.grid,
            self.middle_jobs_area,
            self.it_jobs_middle_overlay,
            self.default_jobs_grid,
            self.it_jobs_column,
        )
        self.queue_resize()
        self.queue_draw()
        for widget in widgets:
            widget.queue_resize()
            widget.queue_draw()
        for _, panel, label, _ in regions:
            panel.queue_resize()
            panel.queue_draw()
            label.queue_resize()
            label.queue_draw()

        frame_clock = self.get_frame_clock()
        if frame_clock:
            frame_clock.request_phase(
                Gdk.FrameClockPhase.UPDATE
                | Gdk.FrameClockPhase.LAYOUT
                | Gdk.FrameClockPhase.PAINT
            )
        gdk_window = self.get_window()
        if gdk_window:
            try:
                gdk_window.invalidate_rect(None, True)
                gdk_window.process_updates(True)
            except Exception:
                LOGGER.exception("window_repaint_recovery_failed")
        Gdk.flush()

    def schedule_dock_layout_settle(self, work, pref_h):
        if self.dock_settle_source is not None:
            GLib.source_remove(self.dock_settle_source)
        self.dock_layout_generation += 1
        self.dock_settle_attempt = 0
        generation = self.dock_layout_generation
        self.dock_settle_source = GLib.timeout_add(
            80,
            self.settle_dock_layout,
            generation,
            work.x,
            work.y,
            work.width,
            work.height,
            pref_h,
        )

    def settle_dock_layout(
        self,
        generation,
        work_x,
        work_y,
        work_width,
        work_height,
        pref_h,
    ):
        if self.closed or generation != self.dock_layout_generation:
            return False

        self.dock_settle_attempt += 1
        target_y = work_y + work_height - pref_h
        column_width, _ = self.size_job_columns(work_width)
        self.set_default_size(work_width, pref_h)
        self.set_size_request(work_width, pref_h)
        self.root_overlay.set_size_request(work_width, pref_h)
        self.drag_surface.set_size_request(work_width, pref_h)
        gdk_window = self.get_window()
        if gdk_window:
            gdk_window.move_resize(work_x, target_y, work_width, pref_h)
        self.request_output_repaint()

        if self.dock_settle_attempt < 3:
            return True

        self.dock_settle_source = None
        origin = gdk_window.get_origin() if gdk_window else (False, None, None)
        actual_x, actual_y = origin[-2], origin[-1]
        actual_width, actual_height = self.get_size()
        LOGGER.info(
            "dock_layout_settled generation=%d attempts=%d "
            "actual=(%s,%s,%dx%d) expected=(%d,%d,%dx%d) "
            "column_width=%d middle_panel=%dx%d right_panel=%dx%d paints=%d",
            generation,
            self.dock_settle_attempt,
            actual_x,
            actual_y,
            actual_width,
            actual_height,
            work_x,
            target_y,
            work_width,
            pref_h,
            column_width,
            self.it_jobs_middle_panel.get_allocated_width(),
            self.it_jobs_middle_panel.get_allocated_height(),
            self.it_jobs_panel.get_allocated_width(),
            self.it_jobs_panel.get_allocated_height(),
            self.paint_serial,
        )
        return False

    def dock_to_monitor(self, monitor=None):
        monitor = monitor or self.get_monitor_for_window() or self.get_default_monitor()
        if not monitor:
            return

        # The dock's strut changes the reported work area. Reuse the original
        # anchor while refreshing on this monitor instead of creeping upward.
        if self.dock_workarea is None or monitor != self.current_monitor:
            self.dock_workarea = monitor.get_workarea()
        self.current_monitor = monitor
        work = self.dock_workarea
        self.size_job_columns(work.width)
        # The four headlines are the sole authority for dock height. Job lists
        # are paged or contained and must never participate in this value.
        pref_h = self.headline_height()
        target_x = work.x
        target_y = work.y + work.height - pref_h

        self.set_default_size(work.width, pref_h)
        self.set_size_request(work.width, pref_h)
        self.root_overlay.set_size_request(work.width, pref_h)
        self.drag_surface.set_size_request(work.width, pref_h)
        self.resize(work.width, pref_h)
        self.move(target_x, target_y)
        LOGGER.info(
            "dock_positioned workarea=(%d,%d,%d,%d) target=(%d,%d) size=%dx%d",
            work.x,
            work.y,
            work.width,
            work.height,
            target_x,
            target_y,
            work.width,
            pref_h,
        )

        window = self.get_window()
        if window:
            try:
                window.move_resize(target_x, target_y, work.width, pref_h)
            except Exception:
                LOGGER.exception("window_move_resize_failed")

        if X11_AVAILABLE and window:
            try:
                self.set_strut(window.get_xid(), monitor, pref_h)
            except Exception:
                LOGGER.exception("window_strut_setup_failed")
        self.schedule_dock_layout_settle(work, pref_h)

    @staticmethod
    def widget_path(widget):
        names = []
        while widget is not None:
            name = type(widget).__name__
            if isinstance(widget, Gtk.Button):
                name += f"[{widget.get_label()!r}]"
            names.append(name)
            widget = widget.get_parent()
        return ">".join(names)

    def is_interactive_target(self, widget):
        while widget is not None and widget is not self:
            if isinstance(
                widget,
                (Gtk.Button, Gtk.Range, Gtk.Entry, Gtk.ComboBox, Gtk.MenuItem, Gtk.Switch),
            ):
                return True
            widget = widget.get_parent()
        return False

    def on_dock_button_press(self, widget, event):
        if event.button != Gdk.BUTTON_PRIMARY:
            return False
        target = Gtk.get_event_widget(event)
        if self.is_interactive_target(target):
            LOGGER.info(
                "dock_press_ignored reason=interactive-target root=(%d,%d) target=%s",
                int(event.x_root),
                int(event.y_root),
                self.widget_path(target),
            )
            return False

        self.cancel_drag("new-drag")
        win_x, win_y = self.get_position()
        self.drag_in_progress = True
        self.drag_grab_widget = self
        self.drag_offset_x = int(event.x_root) - win_x
        self.drag_offset_y = int(event.y_root) - win_y
        self.grab_add()
        # A release can be lost when the compositor interrupts a drag. Keep a
        # grab only while the physical button is down, so controls stay clickable.
        self.drag_watch_source = GLib.timeout_add(100, self.check_drag_pointer_state)
        LOGGER.info(
            "drag_started root=(%d,%d) window=(%d,%d) offset=(%d,%d) target=%s",
            int(event.x_root),
            int(event.y_root),
            win_x,
            win_y,
            self.drag_offset_x,
            self.drag_offset_y,
            self.widget_path(target),
        )
        return True

    def on_window_unmap(self, *args):
        self.cancel_drag("window-unmapped")
        return False

    def cancel_drag(self, reason="cancelled"):
        was_active = self.drag_in_progress
        self.drag_in_progress = False
        widget = self.drag_grab_widget
        self.drag_grab_widget = None
        if widget is not None and widget.has_grab():
            widget.grab_remove()
        if self.drag_watch_source is not None:
            GLib.source_remove(self.drag_watch_source)
            self.drag_watch_source = None
        if was_active:
            LOGGER.info("drag_cancelled reason=%s", reason)
        return False

    def finish_drag(self, monitor=None, reason="released"):
        if not self.drag_in_progress:
            return
        current_position = self.get_position()
        self.cancel_drag(reason)
        if not self.closed and self.get_mapped():
            self.dock_to_monitor(monitor or self.get_monitor_for_window())
            GLib.idle_add(self.log_control_geometry)
        LOGGER.info(
            "drag_finished reason=%s before_dock=(%d,%d) after_dock=(%d,%d)",
            reason,
            current_position[0],
            current_position[1],
            self.get_position()[0],
            self.get_position()[1],
        )

    def on_drag_interrupted(self, widget, event):
        LOGGER.warning("drag_grab_interrupted")
        self.finish_drag(reason="grab-interrupted")
        return False

    def check_drag_pointer_state(self):
        pointer = self.get_display().get_default_seat().get_pointer()
        state = self.get_screen().get_root_window().get_device_position(pointer)[3]
        if not (state & Gdk.ModifierType.BUTTON1_MASK):
            self.drag_watch_source = None
            LOGGER.warning("drag_release_recovered_by_watchdog")
            self.finish_drag(reason="watchdog-release")
            return False
        return True

    def on_dock_motion(self, widget, event):
        if not self.drag_in_progress:
            return False
        if not (event.state & Gdk.ModifierType.BUTTON1_MASK):
            LOGGER.warning("drag_release_recovered_by_motion")
            self.finish_drag(reason="motion-without-button")
            return False

        self.move(
            int(event.x_root) - self.drag_offset_x,
            int(event.y_root) - self.drag_offset_y,
        )
        return True

    def on_dock_button_release(self, widget, event):
        if event.button != Gdk.BUTTON_PRIMARY or not self.drag_in_progress:
            return False

        self.finish_drag(
            self.get_monitor_for_point(int(event.x_root), int(event.y_root)),
            reason="button-release",
        )
        return True

    def set_strut(self, xid, monitor, preferred_height=None):
        if not monitor:
            return

        geometry = monitor.get_geometry()
        preferred_height = preferred_height or self.get_preferred_height()[1]
        bottom_end_x = geometry.x + geometry.width - 1

        # _NET_WM_STRUT_PARTIAL: left, right, top, bottom + 8 range values
        data = [
            0, 0, 0, preferred_height,
            0, 0,
            0, 0,
            0, 0,
            geometry.x, bottom_end_x,
        ]
        try:
            subprocess.run(
                [
                    "xprop", "-id", str(xid),
                    "-f", "_NET_WM_STRUT", "32c",
                    "-set", "_NET_WM_STRUT",
                    ",".join(map(str, data[:4])),
                ],
                check=False,
            )
            subprocess.run(
                [
                    "xprop", "-id", str(xid),
                    "-f", "_NET_WM_STRUT_PARTIAL", "32c",
                    "-set", "_NET_WM_STRUT_PARTIAL",
                    ",".join(map(str, data)),
                ],
                check=False,
            )
        except Exception:
            LOGGER.exception("x11_strut_command_failed xid=%s", xid)

    # -------------------------------------------------------------------------
    # System tray
    # -------------------------------------------------------------------------
    def create_system_tray_icon(self):
        self.tray = Gtk.StatusIcon()
        self.tray.set_from_icon_name("applications-internet")
        self.tray.connect('popup-menu', self.on_tray_popup)
        self.tray.connect('activate',   self.on_tray_click)
        self.tray.set_tooltip_text("NewsDock")
        self.tray.set_visible(True)

    def on_tray_popup(self, icon, button, time):
        LOGGER.info("action_tray_menu_open")
        self.menu = Gtk.Menu()

        show_item = Gtk.MenuItem(label="Show")
        show_item.connect('activate', self.on_show_click)
        self.menu.append(show_item)

        exit_item = Gtk.MenuItem(label="Exit")
        exit_item.connect('activate', self.on_exit_click)
        self.menu.append(exit_item)

        self.menu.show_all()
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, time)

    def on_show_click(self, source):
        LOGGER.info("action_tray_show")
        self.show()
        self.tray.set_visible(False)

    def on_exit_click(self, source):
        LOGGER.info("action_tray_exit")
        Gtk.main_quit()

    def on_tray_click(self, source):
        if self.get_visible():
            LOGGER.info("action_tray_toggle state=hidden")
            self.hide()
            self.tray.set_visible(True)
        else:
            LOGGER.info("action_tray_toggle state=shown")
            self.show()
            self.tray.set_visible(False)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    LOGGER.info("application_start log=%s crash_log=%s", LOG_FILE, CRASH_LOG_FILE)
    win = NewsDock()
    win.connect("destroy", Gtk.main_quit)

    def stop_from_signal(signum, frame):
        LOGGER.info("application_signal_received signal=%s", signum)
        GLib.idle_add(Gtk.main_quit)

    signal.signal(signal.SIGTERM, stop_from_signal)
    signal.signal(signal.SIGINT, stop_from_signal)
    win.show_all()
    try:
        Gtk.main()
    except BaseException:
        LOGGER.exception("gtk_main_crashed")
        raise
    finally:
        LOGGER.info("application_stop")
