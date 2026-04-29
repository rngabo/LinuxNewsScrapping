import gi
import subprocess
import requests
from bs4 import BeautifulSoup
import time
import warnings
import os
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
from gi.repository import Gtk, Gdk, GLib

# Try to import X11 support
try:
    from gi.repository import GdkX11
    X11_AVAILABLE = True
except ImportError:
    X11_AVAILABLE = False
    print("Warning: GdkX11 not available. Dock behavior may not work properly.")

warnings.filterwarnings("ignore", category=DeprecationWarning)

# How many job bullets stay in the middle panel before the rest spill to the right panel
JOBS_SPLIT = 5


# -----------------------------------------------------------------------------
# JobInRwanda scraper (titles only)
# -----------------------------------------------------------------------------
def scrape_jobinrwanda_titles(session, url, limit=12):
    """Returns a list of job titles from JobInRwanda search results page."""
    headers = {"User-Agent": "Mozilla/5.0"}
    r = session.get(url, headers=headers, timeout=(5, 30))
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    titles = []
    for article in soup.find_all("article", class_="node--type-job"):
        title_span = article.find("span", class_="field--name-title")
        if title_span:
            titles.append(title_span.get_text(strip=True))
        if len(titles) >= limit:
            break
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
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.current_monitor = None

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

        self.jobs = []
        self.update_counter = 0

        # Build UI
        self.configure_window()
        self.create_news_layout()
        self.create_system_tray_icon()

        # Kick off refresh cycle and load jobs immediately
        GObject.timeout_add(60 * 1000, self.refresh_news)
        self.start_news_refresh_timer()
        self.refresh_jobs_panel()

    # -------------------------------------------------------------------------
    # Timer
    # -------------------------------------------------------------------------
    def start_news_refresh_timer(self):
        self.update_counter += 1
        self.refresh_news()
        GObject.timeout_add(60 * 1000, self.start_news_refresh_timer)

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

        # Column 1 - middle jobs panel (first JOBS_SPLIT bullets)
        self.jobs_label = Gtk.Label()
        self.jobs_label.set_line_wrap(False)
        self.jobs_label.set_valign(Gtk.Align.START)
        self.jobs_label.set_halign(Gtk.Align.START)
        self.jobs_label.set_margin_start(20)
        self.jobs_label.set_max_width_chars(40)
        self.jobs_label.set_markup("<b>Jobs (JobInRwanda)</b>\nLoading...")

        # Column 2 - right overflow panel (bullets beyond JOBS_SPLIT)
        self.jobs_overflow_label = Gtk.Label()
        self.jobs_overflow_label.set_line_wrap(False)
        self.jobs_overflow_label.set_valign(Gtk.Align.START)
        self.jobs_overflow_label.set_halign(Gtk.Align.START)
        self.jobs_overflow_label.set_margin_start(10)
        self.jobs_overflow_label.set_max_width_chars(40)
        self.jobs_overflow_label.set_markup("")
        self.jobs_overflow_label.set_no_show_all(True)  # controlled manually

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
        self.news = self.fetch_news()

        for key, value in self.news.items():
            title_text = value['title']
            title = Gtk.Label.new(f"{key}: {title_text}")
            title.set_line_wrap(False)
            title.set_halign(Gtk.Align.START)

            # col 0 = news headlines (no buttons)
            self.grid.attach(title, 0, row_number, 1, 1)
            row_number += 1

        # col 1 = middle jobs panel, col 2 = right overflow panel
        self.grid.attach(self.jobs_label, 1, 0, 1, row_number)
        self.grid.attach(self.jobs_overflow_label, 2, 0, 1, row_number)

    # -------------------------------------------------------------------------
    # Jobs panel refresh
    # -------------------------------------------------------------------------
    def refresh_jobs_panel(self):
        try:
            website_titles = scrape_jobinrwanda_titles(
                self.session, self.jobs_url, limit=6
            )
            intl_titles = scrape_jobinrwanda_titles(
                self.session, self.jobs_international_url, limit=6
            )
            econ_titles = scrape_jobinrwanda_titles(
                self.session, self.jobs_economics_url, limit=6
            )

            all_lines = []

            for t in website_titles:
                job_title = t[:55] + "..." if len(t) > 55 else t
                all_lines.append(f"- {GLib.markup_escape_text(job_title)} (Website)")

            for t in intl_titles:
                job_title = t[:55] + "..." if len(t) > 55 else t
                all_lines.append(f"- {GLib.markup_escape_text(job_title)} (International relations)")

            for t in econ_titles:
                job_title = t[:55] + "..." if len(t) > 55 else t
                all_lines.append(f"- {GLib.markup_escape_text(job_title)} (Economics)")

            if not all_lines:
                all_lines.append("- Not yet.")

            # Split at JOBS_SPLIT
            middle_lines   = all_lines[:JOBS_SPLIT]
            overflow_lines = all_lines[JOBS_SPLIT:]

            # Middle panel (col 1) - always shown
            middle_markup = "\n".join(middle_lines)
            self.jobs_label.set_markup(middle_markup)

            # Right overflow panel (col 2) - shown only when there is overflow
            if overflow_lines:
                overflow_markup = "\n".join(overflow_lines)
                self.jobs_overflow_label.set_markup(overflow_markup)
                self.jobs_overflow_label.show()
            else:
                self.jobs_overflow_label.set_markup("")
                self.jobs_overflow_label.hide()

        except Exception as e:
            self.jobs_label.set_markup("<b>Jobs (JobInRwanda)</b>\nFailed to load.")
            self.jobs_overflow_label.set_markup("")
            self.jobs_overflow_label.hide()
            print(f"Error fetching JobInRwanda jobs: {e}")

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
    def update_news_display(self):
        for child in self.grid.get_children():
            self.grid.remove(child)
        self.add_news_to_grid()

    def refresh_news(self):
        for child in self.grid.get_children():
            self.grid.remove(child)
        self.add_news_to_grid()
        self.refresh_jobs_panel()
        self.show_all()
        # Re-hide overflow panel if empty (show_all would un-hide it)
        if not self.jobs_overflow_label.get_text():
            self.jobs_overflow_label.hide()

    def fetch_and_display_news(self):
        self.news = self.fetch_news()
        GLib.idle_add(self.update_news_display)

    # -------------------------------------------------------------------------
    # News fetcher
    # -------------------------------------------------------------------------
    def fetch_news(self):
        parsers = {
            "Rwanda": ("https://www.newtimes.co.rw/",          self.rwanda_parser),
            "Tech":   ("https://techcrunch.com/",               self.tech_parser),
            "World":  ("https://www.bbc.com/news/",             self.world_parser),
            "Africa": ("https://www.bbc.com/news/world/africa", self.africa_parser),
        }
        news = {}
        for key, (url, parser) in parsers.items():
            try:
                response = self.session.get(url, timeout=(5, 30))
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
                time.sleep(5)
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
        self.dock_to_monitor(self.current_monitor)
        return False

    def dock_to_monitor(self, monitor=None):
        monitor = monitor or self.get_monitor_for_window() or self.get_default_monitor()
        if not monitor:
            return

        self.current_monitor = monitor
        work = monitor.get_workarea()
        pref_h = self.get_preferred_height()[1]
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

        win_x, win_y = self.get_position()
        self.drag_in_progress = True
        self.drag_offset_x = int(event.x_root) - win_x
        self.drag_offset_y = int(event.y_root) - win_y
        widget.grab_add()
        return True

    def on_drag_handle_motion(self, widget, event):
        if not self.drag_in_progress:
            return False
        if not (event.state & Gdk.ModifierType.BUTTON1_MASK):
            return False

        self.move(
            int(event.x_root) - self.drag_offset_x,
            int(event.y_root) - self.drag_offset_y,
        )
        return True

    def on_drag_handle_release(self, widget, event):
        if event.button != Gdk.BUTTON_PRIMARY or not self.drag_in_progress:
            return False

        self.drag_in_progress = False
        widget.grab_remove()
        self.dock_to_monitor(
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