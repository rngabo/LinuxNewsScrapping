import gi
import subprocess
import requests
from bs4 import BeautifulSoup
import time
import warnings

from gi.repository import GObject
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GdkX11, GLib

warnings.filterwarnings("ignore", category=DeprecationWarning)


# -----------------------------------------------------------------------------
# JobInRwanda scraper (titles only)
# -----------------------------------------------------------------------------
def scrape_jobinrwanda_titles(session, url, limit=12):
    """
    Returns a list of job titles from JobInRwanda search results page.
    """
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

        # HTTP session
        self.session = requests.Session()
        self.session.headers.update({
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        })

        # JobInRwanda jobs panel (right side)
        self.jobs_url = "https://www.jobinrwanda.com/jobs/search-result?filter_titles_field=website&field_job_category_target_id=All"
        self.jobs_international_url = "https://www.jobinrwanda.com/jobs/search-result?filter_titles_field=&field_job_category_target_id=29"
        self.jobs = []

        # Window + layout
        self.configure_window()
        self.create_news_layout()
        self.create_system_tray_icon()

        self.update_counter = 0

        # Refresh every minute
        GObject.timeout_add(60 * 1000, self.refresh_news)
        self.start_news_refresh_timer()

        # Show jobs immediately at startup
        self.refresh_jobs_panel()

    def start_news_refresh_timer(self):
        self.update_counter += 1
        self.refresh_news()
        GObject.timeout_add(60 * 1000, self.start_news_refresh_timer)

    def summarize_text(self, text, max_output_length=90, max_tokens=1024, chunk_size=900):
        # Your summarizer is disabled in this version (keeps your old behavior)
        return "_"

    def chunked_summary(self, article, max_output_length=90, max_tokens=1024, chunk_size=800):
        return self.summarize_text(article, max_output_length=max_output_length, max_tokens=max_tokens, chunk_size=chunk_size)

    def fetch_tech_content(self, tech_link):
        tech_content = self.fetch_content(tech_link, is_tech=True, is_world=False, is_africa=False, is_rwanda=False)
        summary_content = self.chunked_summary(tech_content)
        self.clear_loading_message(summary_content)

    def fetch_world_content(self, world_link):
        world_content = self.fetch_content(world_link, is_tech=False, is_world=True, is_africa=False, is_rwanda=False)
        summary_content = self.chunked_summary(world_content)
        self.clear_loading_message(summary_content)

    def fetch_rwanda_content(self, rwanda_link):
        rwanda_content = self.fetch_content(rwanda_link, is_tech=False, is_world=False, is_africa=False, is_rwanda=True, rwanda_link=rwanda_link)
        summary_content = self.chunked_summary(rwanda_content)
        self.clear_loading_message(summary_content)

    def fetch_africa_content(self, africa_link):
        if "?" in africa_link:
            africa_link = africa_link.split("?", 1)[0]
        africa_content = self.fetch_content(africa_link, is_tech=False, is_world=False, is_africa=True, is_rwanda=False)
        summary_content = self.chunked_summary(africa_content)
        self.clear_loading_message(summary_content)

    def configure_window(self):
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_keep_above(True)
        self.set_decorated(False)

        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor()
        geometry = monitor.get_geometry()
        self.set_default_size(geometry.width, -1)
        self.connect("realize", self.on_realize)

    def create_news_layout(self):
        self.grid = Gtk.Grid()
        self.grid.set_column_spacing(20)

        # Middle column: summary/loading
        self.loading_label = Gtk.Label("Loading...")
        self.loading_label.set_line_wrap(True)
        self.loading_label.set_valign(Gtk.Align.START)
        self.loading_label.set_halign(Gtk.Align.START)
        self.loading_label.set_margin_start(20)
        self.loading_label.set_markup("<span size='large'></span>")

        # Optional content label (kept from your original)
        self.content_label = Gtk.Label()
        self.content_label.set_line_wrap(True)
        self.content_label.set_valign(Gtk.Align.START)
        self.content_label.set_halign(Gtk.Align.START)
        self.content_label.set_margin_start(20)
        self.content_label.set_markup("<span size='large'></span>")

        # Right column: Jobs panel
        self.jobs_label = Gtk.Label()
        self.jobs_label.set_line_wrap(True)
        self.jobs_label.set_valign(Gtk.Align.START)
        self.jobs_label.set_halign(Gtk.Align.START)
        self.jobs_label.set_margin_start(20)
        self.jobs_label.set_markup("<span size='large'><b>Jobs (JobInRwanda)</b>\nLoading...</span>")

        self.add_news_to_grid()
        self.add(self.grid)
        self.jobs_label.set_line_wrap(False)     # prevents tall wrap (main cause of height growth)


    def add_news_to_grid(self):
        for child in self.grid.get_children():
            self.grid.remove(child)

        row_number = 0
        self.news = self.fetch_news()

        for key, value in self.news.items():
            hbox = Gtk.HBox(False, 5)
            title = Gtk.Label.new(f"{key}: {value['title']}")
            button_read = Gtk.Button.new_with_label("→")
            button_clear = Gtk.Button.new_with_label("Clear")

            button_read.connect("clicked", self.on_arrow_click, key)
            button_clear.connect("clicked", self.on_clear_click)

            hbox.pack_start(title, False, False, 0)
            hbox.pack_start(button_read, False, False, 0)
            hbox.pack_start(button_clear, False, False, 0)

            self.grid.attach(hbox, 0, row_number, 1, 1)
            row_number += 1

        # column 1 (middle): summary/loading
        self.grid.attach(self.loading_label, 1, 0, 1, row_number)

        # column 2 (right): jobs
        self.grid.attach(self.jobs_label, 2, 0, 1, row_number)

    def refresh_jobs_panel(self):
        try:
            # Website-related jobs
            website_titles = scrape_jobinrwanda_titles(self.session, self.jobs_url, limit=8)

            # International Relations jobs
            intl_titles = scrape_jobinrwanda_titles(self.session, self.jobs_international_url, limit=8)

            lines = []

            # Website section
            if website_titles:
                lines.append("Other")   # keep/remove this label as you want
                for t in website_titles:
                    lines.append(f"• {GLib.markup_escape_text(t)}")
            else:
                lines.append("Other")
                lines.append("• Not yet.")

            # Spacer + International section
            lines.append("")  # blank line between groups
            if intl_titles:
                for t in intl_titles:
                    lines.append(f"• {GLib.markup_escape_text(t)}")
            else:
                lines.append("• Not yet.")

            text = "<span>" + "\n".join(lines) + "</span>"
            self.jobs_label.set_markup(text)

        except Exception as e:
            self.jobs_label.set_markup("<span>Failed to load.</span>")
            print(f"Error fetching JobInRwanda jobs: {e}")


    def on_clear_click(self, button):
        self.loading_label.set_text("")
        self.content_label.set_text("")

    def display_loading_message(self):
        self.loading_label.set_text("Loading...")

    def clear_loading_message(self, summary_content):
        self.loading_label.set_text(summary_content)

    def on_arrow_click(self, widget, section_key):
        self.display_loading_message()
        content = self.news.get(section_key, "")

        if section_key == "Tech" and isinstance(content, dict):
            link = content["link"]
            GObject.timeout_add(100, self.fetch_tech_content, link)

        elif section_key == "World" and isinstance(content, dict):
            link = content["link"]
            GObject.timeout_add(100, self.fetch_world_content, link)

        elif section_key == "Rwanda" and isinstance(content, dict):
            link = content["link"]
            GObject.timeout_add(100, self.fetch_rwanda_content, link)

        elif section_key == "Africa" and isinstance(content, dict):
            link = content["link"]
            GObject.timeout_add(100, self.fetch_africa_content, link)

    def update_news_display(self):
        for child in self.grid.get_children():
            self.grid.remove(child)
        self.add_news_to_grid()

    def fetch_news(self):
        parsers = {
            "Rwanda": ("https://www.newtimes.co.rw/", self.rwanda_parser),
            "Tech": ("https://techcrunch.com/", self.tech_parser),
            "World": ("https://www.bbc.com/news/", self.world_parser),
            "Africa": ("https://www.bbc.com/news/world/africa", self.africa_parser)
        }

        news = {}
        for key, (url, parser) in parsers.items():
            try:
                response = self.session.get(url, timeout=(5, 30))
                response.raise_for_status()

                soup = BeautifulSoup(response.text, "html.parser")
                content = parser(soup)

                news[key] = {"title": content.get("title", ""), "link": content.get("link", "")}

            except requests.exceptions.RequestException as e:
                print(f"Error fetching news for '{key}': {e}")
                news[key] = {"title": "Failed to fetch news.", "link": ""}
                time.sleep(5)

        return news

    def refresh_news(self):
        for child in self.grid.get_children():
            self.grid.remove(child)

        self.add_news_to_grid()

        # Update right-side jobs panel
        self.refresh_jobs_panel()

        self.show_all()

    def fetch_and_display_news(self):
        self.news = self.fetch_news()
        GLib.idle_add(self.update_news_display)

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
                link = title_element.a['href']
            else:
                title = "Title not found."
                link = ""
        else:
            title = "Post picker content not found."
            link = ""

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
        # Same logic as world_parser
        return self.world_parser(soup)

    def rwanda_parser(self, soup):
        link_element = soup.find('div', class_='article-title')
        if link_element:
            title = link_element.text.strip()
            anchor_tags = soup.select('.nt-home-tabs .article-title a')
            if anchor_tags:
                first_anchor_tag = anchor_tags[0]
                link = first_anchor_tag['href']
                return {"title": title, "link": link}
            return {"title": title, "link": "Link not found for the article."}

        return {"title": "Article title not found.", "link": ""}

    def fetch_content(self, link, is_tech=False, rwanda_link=None, is_world=False, is_africa=False, is_rwanda=False):
        url = link
        response = self.session.get(url)

        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            time.sleep(2)

            if is_tech:
                container = soup.find('div', class_='entry-content wp-block-post-content is-layout-flow wp-block-post-content-is-layout-flow')
                if container:
                    paragraphs = container.find_all('p')
                    content = "\n".join(paragraph.get_text() for paragraph in paragraphs)
                else:
                    content = "Elements not found."

            elif is_rwanda:
                content = ""
                if rwanda_link:
                    content = scrape_rwanda_article_content(link)

            elif is_world:
                if '/live/' in url:
                    container = soup.find('section', class_='qa-summary-points')
                    if container:
                        list_items = container.find_all('li', class_='lx-c-summary-points__item')
                        content = '\n'.join([item.get_text().strip() for item in list_items])
                    else:
                        content = "Elements not found."
                else:
                    container = soup.find(id='main-content')
                    if container:
                        text_blocks = container.find_all('div', attrs={'data-component': 'text-block'})
                        content = []
                        for block in text_blocks:
                            paragraphs = block.find_all('p')
                            for paragraph in paragraphs:
                                content.append(paragraph.get_text())
                        content = '\n'.join(content)
                    else:
                        content = "Elements not found."

            elif is_africa:
                if '/live/' in url:
                    container = soup.find('section', class_='qa-summary-points')
                    if container:
                        list_items = container.find_all('li', class_='lx-c-summary-points__item')
                        content = '\n'.join([item.get_text().strip() for item in list_items])
                    else:
                        content = "Elements not found."
                else:
                    main_content = soup.find('main', id='main-content')
                    if main_content:
                        text_blocks = main_content.find_all('div', attrs={'data-component': 'text-block'})
                        content = []
                        for block in text_blocks:
                            paragraphs = block.find_all('p')
                            for paragraph in paragraphs:
                                content.append(paragraph.get_text())
                        content = '\n'.join(content)
                    else:
                        content = "Elements not found."
            else:
                content = "Invalid category."
        else:
            content = "Failed to fetch content."

        return content

    def on_realize(self, widget):
        window = self.get_window()
        xid = window.get_xid()
        self.set_strut(xid)
        self.resize_to_fit_content()

    def resize_to_fit_content(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor()
        geometry = monitor.get_geometry()
        preferred_height = self.get_preferred_height()[1]
        self.set_default_size(geometry.width, preferred_height)
        self.move(0, geometry.height - preferred_height)

    def set_strut(self, xid):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor()
        geometry = monitor.get_geometry()
        preferred_height = self.get_preferred_height()[1]
        data = [0, 0, 0, preferred_height, 0, geometry.height, 0, geometry.height + preferred_height, 0, geometry.width, 0, geometry.width]
        subprocess.run(["xprop", "-id", str(xid), "-f", "_NET_WM_STRUT", "32c", "-set", "_NET_WM_STRUT", ",".join(map(str, data[:4]))])
        subprocess.run(["xprop", "-id", str(xid), "-f", "_NET_WM_STRUT_PARTIAL", "32c", "-set", "_NET_WM_STRUT_PARTIAL", ",".join(map(str, data))])

    def on_tray_popup(self, icon, button, time):
        self.menu = Gtk.Menu()

        show = Gtk.MenuItem(label="Show")
        show.connect('activate', self.on_show_click)
        self.menu.append(show)

        exit = Gtk.MenuItem(label="Exit")
        exit.connect('activate', self.on_exit_click)
        self.menu.append(exit)

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

    def create_system_tray_icon(self):
        self.tray = Gtk.StatusIcon()
        self.tray.set_from_icon_name("applications-internet")
        self.tray.connect('popup-menu', self.on_tray_popup)
        self.tray.connect('activate', self.on_tray_click)
        self.tray.set_tooltip_text("NewsDock")
        self.tray.set_visible(True)


if __name__ == "__main__":
    win = NewsDock()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
