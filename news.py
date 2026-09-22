import gi
import subprocess
import requests
from bs4 import BeautifulSoup
import time
import threading
import warnings
import os
from urllib.parse import urljoin
from gi.repository import GObject
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Force X11 backend for proper dock behavior on Wayland via XWayland
os.environ['GDK_BACKEND'] = 'x11'

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Pango

# Try to import X11 support
try:
    from gi.repository import GdkX11
    X11_AVAILABLE = True
except ImportError:
    X11_AVAILABLE = False
    print("Warning: GdkX11 not available. Dock behavior may not work properly.")

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


def job_markup(text, max_chars):
    return f"- {GLib.markup_escape_text(compact_job_text(text, max_chars))}"


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
        self.refresh_timer_source = None
        self.connect("destroy", self.on_destroy)
        self.connect("unmap", self.cancel_drag)

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
        self.refresh_news()

    def on_destroy(self, widget):
        self.closed = True
        self.cancel_drag()
        for source in (self.refresh_timer_source, self.it_layout_source):
            if source:
                GLib.source_remove(source)
        self.refresh_timer_source = None
        self.it_layout_source = None

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
        .read-button {
            background: #242933;
            margin-left: 15px;
        }
        .read-button:hover {
            background-color: #2e3440;
        }
        button.drag-handle {
            background-color: transparent;
            color: #2f3643;
            min-width: 12px;
            min-height: 12px;
            padding: 0;
        }
        button.drag-handle:hover {
            background-color: transparent;
            color: #485264;
        }
        label.drag-handle-text {
            color: #2f3643;
            font-size: 8px;
            font-weight: bold;
        }
        button.drag-handle:hover label.drag-handle-text {
            color: #485264;
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
        self.drag_surface = Gtk.EventBox()
        self.drag_surface.set_visible_window(False)
        self.drag_surface.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.drag_surface.connect("button-press-event", self.on_drag_handle_press)
        self.drag_surface.connect("motion-notify-event", self.on_drag_handle_motion)
        self.drag_surface.connect("button-release-event", self.on_drag_handle_release)
        self.drag_surface.connect("grab-broken-event", self.on_drag_interrupted)

        self.drag_button = Gtk.Button()
        self.drag_button.set_relief(Gtk.ReliefStyle.NONE)
        self.drag_button.set_tooltip_text("Drag dock to another screen")
        self.drag_button.set_halign(Gtk.Align.END)
        self.drag_button.set_valign(Gtk.Align.START)
        self.drag_button.set_margin_top(0)
        self.drag_button.set_margin_end(2)
        self.drag_button.get_style_context().add_class("drag-handle")
        self.drag_label = Gtk.Label(label="::")
        self.drag_label.get_style_context().add_class("drag-handle-text")
        self.drag_button.add(self.drag_label)
        self.drag_button.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.drag_button.connect("button-press-event", self.on_drag_handle_press)
        self.drag_button.connect("motion-notify-event", self.on_drag_handle_motion)
        self.drag_button.connect("button-release-event", self.on_drag_handle_release)
        self.drag_button.connect("grab-broken-event", self.on_drag_interrupted)

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

        # Keep the normal middle listings underneath an optional IT view, so
        # switching categories preserves their layout and restores them instantly.
        self.default_jobs_grid = Gtk.Grid()
        self.default_jobs_grid.set_column_spacing(20)
        self.default_jobs_grid.set_column_homogeneous(True)
        self.default_jobs_grid.attach(self.jobs_label, 0, 0, 1, 1)
        self.default_jobs_grid.attach(self.default_jobs_overflow_box, 1, 0, 1, 1)
        self.middle_jobs_area = Gtk.Overlay()
        self.middle_jobs_area.add(self.default_jobs_grid)

        self.it_jobs_middle_panel = Gtk.ScrolledWindow()
        self.it_jobs_middle_panel.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.it_jobs_middle_panel.set_propagate_natural_width(False)
        self.it_jobs_middle_panel.set_propagate_natural_height(False)
        self.it_jobs_middle_panel.set_halign(Gtk.Align.END)
        self.it_jobs_middle_panel.set_hexpand(False)
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
        self.middle_jobs_area.add_overlay(self.it_jobs_middle_panel)
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
        self.it_jobs_panel.get_style_context().add_class("it-jobs-panel")

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
        # Keep the control inside the IT column's own overlay. This gives it a
        # stable input area even when the dock is resized or moved.
        self.it_jobs_toggle.set_margin_end(20)
        self.it_jobs_toggle.set_margin_bottom(10)
        self.it_jobs_toggle.get_style_context().add_class("it-jobs-toggle")
        self.it_jobs_toggle.set_tooltip_text("Show or hide IT jobs")
        self.it_jobs_toggle.connect("clicked", self.on_it_jobs_toggle)
        self.it_jobs_column.add(self.it_jobs_panel)
        self.it_jobs_column.add_overlay(self.it_jobs_toggle)
        self.it_jobs_column.set_overlay_pass_through(self.it_jobs_toggle, False)

        self.add_news_to_grid()
        self.drag_surface.add(self.grid)
        self.root_overlay.add(self.drag_surface)
        self.root_overlay.add_overlay(self.drag_button)
        self.add(self.root_overlay)

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
        if self.closed or self.jobs_loading:
            return
        self.jobs_loading = True
        threading.Thread(target=self.fetch_jobs, daemon=True).start()

    def fetch_jobs(self):
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

        except Exception as e:
            titles = None
            print(f"Error fetching JobInRwanda jobs: {e}")
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

        self.jobs_label.set_markup("\n".join(
            job_markup(title, DEFAULT_JOB_LINE_CHARS) for title in middle
        ))
        self.jobs_overflow_label.set_markup("\n".join(
            job_markup(title, DEFAULT_JOB_LINE_CHARS) for title in overflow
        ))
        self.jobs_overflow_label.set_visible(bool(overflow))
        self.jobs_read_more.set_visible(has_more)

    def on_jobs_read_more(self, button):
        next_start = (self.default_jobs_page + 1) * DEFAULT_JOBS_PER_PAGE_WITH_LINK
        if next_start < len(self.default_job_titles):
            self.default_jobs_page += 1
            self.render_default_jobs_page()

    def on_it_jobs_toggle(self, button):
        visible = not self.jobs_it_label.get_visible()
        self.jobs_it_label.set_visible(visible)
        button.set_label("Hide" if visible else "Show")
        if visible:
            self.queue_it_jobs_layout()
            self.refresh_it_jobs_panel()
        else:
            self.it_layout_key = None
            self.set_it_columns(self.jobs_it_label.get_label())

    def refresh_it_jobs_panel(self):
        if self.closed or self.it_jobs_loading:
            return
        self.it_jobs_loading = True
        threading.Thread(target=self.fetch_it_jobs, daemon=True).start()

    def fetch_it_jobs(self):
        # Keep the Show/Hide button responsive while this category loads.
        titles = []
        message = None
        try:
            with requests.Session() as session:
                titles = scrape_jobinrwanda_titles(
                    session, self.jobs_it_url, limit=None, include_employer=True
                )
            if not titles:
                message = "- No IT jobs found."
        except Exception as e:
            message = "Failed to load. Hide and show to retry."
            print(f"Error fetching IT jobs: {e}")
        GLib.idle_add(self.update_it_jobs_panel, titles, message)

    def update_it_jobs_panel(self, titles, message=None):
        self.it_jobs_loading = False
        if self.closed:
            return False
        self.it_job_titles = titles
        self.it_jobs_message = message
        self.queue_it_jobs_layout()
        return False

    def queue_it_jobs_layout(self, *args):
        if not self.closed and self.jobs_it_label.get_visible() and self.it_layout_source is None:
            self.it_layout_source = GLib.idle_add(self.layout_it_jobs)

    @staticmethod
    def format_it_jobs(titles, max_chars):
        return "\n".join(job_markup(title, max_chars) for title in titles)

    def set_it_columns(self, right_markup, middle_markup=None):
        # Opacity keeps the default listings' size reserved without exposing
        # their text underneath IT. They continue refreshing while covered.
        self.default_jobs_grid.set_opacity(0 if middle_markup else 1)
        self.it_jobs_middle_panel.set_visible(bool(middle_markup))
        for label, markup in (
            (self.jobs_it_label, right_markup),
            (self.jobs_it_middle_label, middle_markup or ""),
        ):
            if label.get_label() != markup:
                label.set_markup(markup)

    def layout_it_jobs(self):
        self.it_layout_source = None
        if self.closed or not self.jobs_it_label.get_visible():
            return False

        layout_key = (tuple(self.it_job_titles), self.it_jobs_message)
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
        right = self.format_it_jobs(right_titles, TECH_RIGHT_LINE_CHARS)
        middle = self.format_it_jobs(middle_titles, TECH_MIDDLE_LINE_CHARS)
        if self.it_jobs_message:
            right += ("\n" if right else "") + GLib.markup_escape_text(self.it_jobs_message)
        self.set_it_columns(right, middle or None)
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
            return False
        self.news = headlines
        for key, value in headlines.items():
            text = f"{key}: {value['title']}"
            self.headline_labels[key].set_text(text)
            self.headline_labels[key].set_tooltip_text(text)
        return False

    def refresh_news(self):
        if self.closed:
            return False
        if not self.news_loading:
            self.news_loading = True
            self.update_counter += 1
            threading.Thread(target=self.fetch_and_display_news, daemon=True).start()
        self.refresh_jobs_panel()
        if self.jobs_it_label.get_visible():
            self.refresh_it_jobs_panel()
        return True

    def fetch_and_display_news(self):
        headlines = None
        try:
            with requests.Session() as session:
                session.headers.update(self.session.headers)
                headlines = self.fetch_news(session)
        except Exception as e:
            print(f"Error refreshing news: {e}")
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
            except requests.exceptions.RequestException as e:
                print(f"Error fetching news for '{key}': {e}")
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
        except Exception as e:
            print(f"An error occurred: {e}")
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
        GLib.idle_add(self.resize_to_fit_content)

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
        self.it_jobs_middle_panel.set_size_request(column_width, -1)
        self.it_jobs_column.set_size_request(column_width, -1)

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

        window = self.get_window()
        if window:
            try:
                window.move_resize(target_x, target_y, work.width, pref_h)
            except Exception as e:
                print(f"Failed to force window geometry: {e}")

        if X11_AVAILABLE and window:
            try:
                self.set_strut(window.get_xid(), monitor, pref_h)
            except Exception as e:
                print(f"Failed to get XID / set strut: {e}")

    def on_drag_handle_press(self, widget, event):
        if event.button != Gdk.BUTTON_PRIMARY:
            return False
        if widget is self.drag_surface:
            target = Gtk.get_event_widget(event)
            while target is not None and target is not widget:
                if isinstance(target, (Gtk.Button, Gtk.Range)):
                    return False
                target = target.get_parent()

        self.cancel_drag()
        win_x, win_y = self.get_position()
        self.drag_in_progress = True
        self.drag_grab_widget = widget
        self.drag_offset_x = int(event.x_root) - win_x
        self.drag_offset_y = int(event.y_root) - win_y
        widget.grab_add()
        # A release can be lost when the compositor interrupts a drag. Keep a
        # grab only while the physical button is down, so Show stays clickable.
        self.drag_watch_source = GLib.timeout_add(100, self.check_drag_button)
        return True

    def cancel_drag(self, *args):
        self.drag_in_progress = False
        widget = self.drag_grab_widget
        self.drag_grab_widget = None
        if widget is not None and widget.has_grab():
            widget.grab_remove()
        if self.drag_watch_source is not None:
            GLib.source_remove(self.drag_watch_source)
            self.drag_watch_source = None
        return False

    def finish_drag(self, monitor=None):
        if not self.drag_in_progress:
            return
        self.cancel_drag()
        if not self.closed and self.get_mapped():
            self.dock_to_monitor(monitor or self.get_monitor_for_window())

    def on_drag_interrupted(self, widget, event):
        self.finish_drag()
        return False

    def check_drag_button(self):
        pointer = self.get_display().get_default_seat().get_pointer()
        state = self.get_screen().get_root_window().get_device_position(pointer)[3]
        if not (state & Gdk.ModifierType.BUTTON1_MASK):
            self.drag_watch_source = None
            self.finish_drag()
            return False
        return True

    def on_drag_handle_motion(self, widget, event):
        if not self.drag_in_progress:
            return False
        if not (event.state & Gdk.ModifierType.BUTTON1_MASK):
            self.finish_drag()
            return False

        self.move(
            int(event.x_root) - self.drag_offset_x,
            int(event.y_root) - self.drag_offset_y,
        )
        return True

    def on_drag_handle_release(self, widget, event):
        if event.button != Gdk.BUTTON_PRIMARY or not self.drag_in_progress:
            return False

        self.finish_drag(
            self.get_monitor_for_point(int(event.x_root), int(event.y_root))
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
        except Exception as e:
            print(f"Failed to set strut: {e}")

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
        self.show()
        self.tray.set_visible(False)

    def on_exit_click(self, source):
        Gtk.main_quit()

    def on_tray_click(self, source):
        if self.get_visible():
            self.hide()
            self.tray.set_visible(True)
        else:
            self.show()
            self.tray.set_visible(False)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    win = NewsDock()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
