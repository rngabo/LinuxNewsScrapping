import gi
import subprocess
import requests
from bs4 import BeautifulSoup
import time
import warnings
import torch
from gi.repository import GObject
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import TimeoutException
from transformers import T5ForConditionalGeneration, T5Tokenizer

gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GdkX11, GLib

warnings.filterwarnings("ignore", category=DeprecationWarning)

def scrape_article_content(url):
    # Set up Chrome options for headless mode
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")

    # Set path to chromedriver as per your configuration
    #sudo find / -type f -name chromedriver 2>/dev/null
    path_to_chromedriver = '/snap/chromium/2897/usr/lib/chromium-browser/chromedriver'

    # Set up the Chrome WebDriver using Service and Options
    service = Service(executable_path=path_to_chromedriver)
    driver = webdriver.Chrome(service=service, options=chrome_options)

    # Navigate to the URL
    driver.get(url)

    # Set up the explicit wait
    wait = WebDriverWait(driver, 25)

    article_body_text = ""  # Initialize variable to hold the article text

    try:
        # Wait for the article body to be present and then extract the text
        article_body_element = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@class='article-body']")))
        article_body_text = article_body_element.text
        print(article_body_element)
    finally:
        # Close the browser window
        driver.quit()

    return article_body_text

def scrape_rwanda_article_content(url):
        return scrape_article_content(url)

class NewsDock(Gtk.Window):
    def __init__(self):
        super().__init__()

        # Initialize a session for making HTTP requests
        self.session = requests.Session()
        self.session.headers.update({
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
        })

        # Configure the window
        self.configure_window()

        # Create the news layout
        self.create_news_layout()

        # Add a system tray icon
        self.create_system_tray_icon()

        self.update_counter = 0  # Initialize the counter

        # Schedule the news refresh every 1 minute
        GObject.timeout_add(60 * 1000, self.refresh_news)

        # Start a timer for updating fetch_news() every 1 minute
        self.start_news_refresh_timer()
        #Facebook: not so deep, (2/6)
        # self.model_name = "facebook/bart-large-cnn"
        # self.model = BartForConditionalGeneration.from_pretrained(self.model_name)
        # self.tokenizer = BartTokenizer.from_pretrained(self.model_name)
        # Google close to facebook, a bit good
        self.model_name = "t5-large"  # t-small: missleading, t5-large:2/6, t5-base:2/6, t5-11b, t5-3b 
        self.model = T5ForConditionalGeneration.from_pretrained(self.model_name)
        self.tokenizer = T5Tokenizer.from_pretrained(self.model_name)
        #Pegasus // verbose a bit long
        # self.model_name = "google/pegasus-large"  # or "google/pegasus-xsum" based on your needs
        # self.model = PegasusForConditionalGeneration.from_pretrained(self.model_name)
        # self.tokenizer = PegasusTokenizer.from_pretrained(self.model_name)
        # self.model_name = "gpt2"  # Other options include gpt2-medium, gpt2-large, gpt2-xl
        # self.model = GPT2LMHeadModel.from_pretrained(self.model_name)
        # self.tokenizer = GPT2Tokenizer.from_pretrained(self.model_name)

    
    def start_news_refresh_timer(self):
        self.update_counter += 1
        # print(f"update {self.update_counter}")
        
        # Refresh the news
        self.refresh_news()
        
        # Call this method again after 60 seconds
        GObject.timeout_add(60 * 1000, self.start_news_refresh_timer)



    # @staticmethod
    def summarize_text(self, text, max_output_length=90, max_tokens=1024, chunk_size=900):
        # Split the text into chunks
        words = text.split()
        chunks = [' '.join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]
        
        combined_summary = ""

        for chunk in chunks:
            tokens = self.tokenizer.encode("summarize: " + chunk, return_tensors="pt", max_length=max_tokens, truncation=True)
            
            if len(tokens[0]) == max_tokens:
                warnings.warn("Chunk is too long and has been truncated to fit.")

            with torch.no_grad():
                summary_ids = self.model.generate(tokens, max_length=max_output_length, min_length=25, 
                                                length_penalty=2.0, num_beams=4, early_stopping=True)
            summary = self.tokenizer.decode(summary_ids[0], skip_special_tokens=True)

            combined_summary += summary + " "

        return combined_summary

    def chunked_summary(self, article, max_output_length=90, max_tokens=1024, chunk_size=800):
        summary = self.summarize_text(article, max_output_length=max_output_length, max_tokens=max_tokens, chunk_size=chunk_size)

        return summary

    
    def fetch_tech_content(self, tech_link):
        # To fetch Tech content, set is_tech=True
        tech_content = self.fetch_content(tech_link, is_tech=True, is_world=False, is_africa=False, is_rwanda=False)
        # Print the content in the console
        summary_content = self.chunked_summary(tech_content)
        # Clear the loading message when content is received
        # print(tech_summary_content)
        self.clear_loading_message(summary_content)

    def fetch_world_content(self, world_link):
        # To fetch World content, set is_world=True
        world_content = self.fetch_content(world_link, is_tech=False, is_world=True, is_africa=False, is_rwanda=False)
        # Print the content in the console or display it in your UI
        summary_content = self.chunked_summary(world_content)
        # Clear the loading message when content is received
        self.clear_loading_message(summary_content)

    def fetch_rwanda_content(self, rwanda_link):
        # To fetch World content, set is_world=True
        # rwanda_content = self.fetch_content(rwanda_link, is_tech=True, is_world=False, is_africa=False, is_rwanda=True)
        rwanda_content = self.fetch_content(rwanda_link, is_tech=False, is_world=False, is_africa=False, is_rwanda=True, rwanda_link=rwanda_link)
        # Print the content in the console or display it in your UI
        summary_content = self.chunked_summary(rwanda_content)
        # Clear the loading message when content is received
        self.clear_loading_message(summary_content)

    def fetch_africa_content(self, africa_link):
        if "?" in africa_link:
            # Find the index of the "?" character
            index = africa_link.index("?")
            # Slice the string to keep only the part before "?"
            africa_link = africa_link[:index]
        # print(africa_link)
        # To fetch Africa content, set is_africa=True
        africa_content = self.fetch_content(africa_link, is_tech=False, is_world=False, is_africa=True, is_rwanda=False)
        # Print the content in the console or display it in your UI
        summary_content = self.chunked_summary(africa_content)
        # Clear the loading message when content is received
        self.clear_loading_message(summary_content)

    
    def configure_window(self):
        # Configure the main window as a dock
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_keep_above(True)
        self.set_decorated(False)

        # Get the primary monitor's geometry and set the window size
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor()
        geometry = monitor.get_geometry()
        self.set_default_size(geometry.width, -1)
        self.connect("realize", self.on_realize)

    def create_news_layout(self):
        # Create a grid layout for the news
        self.grid = Gtk.Grid()
        self.grid.set_column_spacing(10)

        # Initialize Loading Label
        self.loading_label = Gtk.Label("Loading...")
        self.loading_label.set_line_wrap(True)
        self.loading_label.set_valign(Gtk.Align.START)
        self.loading_label.set_halign(Gtk.Align.START)
        self.loading_label.set_margin_start(20)
        self.loading_label.set_markup("<span size='large'></span>")

        # Initialize Content Label
        self.content_label = Gtk.Label()
        self.content_label.set_line_wrap(True)
        self.content_label.set_valign(Gtk.Align.START)
        self.content_label.set_halign(Gtk.Align.START)
        self.content_label.set_margin_start(20)
        self.content_label.set_markup("<span size='large'></span>")

        # Populate the grid with news data
        self.add_news_to_grid()

        # Finally, add the entire grid to the main window
        self.add(self.grid)

    
    def add_news_to_grid(self):
        # Clear the existing children from the grid
        for child in self.grid.get_children():
            self.grid.remove(child)
        
        # Initialize the row number
        row_number = 0

        # Fetch the news data
        self.news = self.fetch_news()

        for key, value in self.news.items():
            hbox = Gtk.HBox(False, 5)
            title = Gtk.Label.new(f"{key}: {value['title']}")
            button_read = Gtk.Button.new_with_label("→")
            button_clear = Gtk.Button.new_with_label("Clear")
            
            button_read.connect("clicked", self.on_arrow_click, key)
            button_clear.connect("clicked", self.on_clear_click)  # Connect the clear button
            
            hbox.pack_start(title, False, False, 0)
            hbox.pack_start(button_read, False, False, 0)
            hbox.pack_start(button_clear, False, False, 0)  # Add the clear button to the hbox
            
            self.grid.attach(hbox, 0, row_number, 1, 1)
            row_number += 1

        # Attach the loading label to the grid
        self.grid.attach(self.loading_label, 1, 0, 1, row_number)



    def on_clear_click(self, button):
        self.loading_label.set_text("")  # Clear the loading label
        self.content_label.set_text("")  # Clear the content label (if needed)

   

    def display_loading_message(self):
        self.loading_label.set_text("Loading...")

    def clear_loading_message(self, summary_content):
        self.loading_label.set_text(summary_content)

    def on_arrow_click(self, widget, section_key):
        # Set the "Loading..." message immediately upon clicking
        self.display_loading_message()
        content = self.news.get(section_key, "")
        if section_key == "Tech" and isinstance(content, dict):
            link = content["link"]
            # Fetch the content asynchronously
            GObject.timeout_add(100, self.fetch_tech_content, link)
        elif section_key == "World" and isinstance(content, dict):
            link = content["link"]
            # Fetch the content asynchronously
            GObject.timeout_add(100, self.fetch_world_content, link)
        elif section_key == "Rwanda" and isinstance(content, dict):
            link = content["link"]
            # Fetch the content asynchronously
            GObject.timeout_add(100, self.fetch_rwanda_content, link)
        elif section_key == "Africa" and isinstance(content, dict):
            link = content["link"]
            # Fetch the content asynchronously
            GObject.timeout_add(100, self.fetch_africa_content, link)
        # Add similar conditions for other categories here

    def display_content(self, content):
        if content == "Loading...":
            self.content_label.set_markup("<span size='large'>Loading...</span>")
        else:
            self.content_label.set_markup(content)
        self.content_label.set_halign(Gtk.Align.START)

    def update_news_display(self):
            # Clear the existing children from the grid
            for child in self.grid.get_children():
                self.grid.remove(child)

            # Re-populate the grid
            self.add_news_to_grid()

    def fetch_news(self):
            # Define parsers for each news category
            parsers = {
                "Rwanda": ("https://www.newtimes.co.rw/", self.rwanda_parser),
                "Tech": ("https://techcrunch.com/", self.tech_parser),
                "World": ("https://www.bbc.com/news/", self.world_parser),
                "Africa": ("https://www.bbc.com/news/world/africa", self.africa_parser)
            }

            news = {}
            for key, (url, parser) in parsers.items():
                try:
                    response = self.session.get(url, timeout=(5, 30))  # Set timeouts here
                    response.raise_for_status()  # Raise an exception for HTTP errors

                    soup = BeautifulSoup(response.text, "html.parser")
                    content = parser(soup)

                    if key == "Tech" and isinstance(content, dict):
                        news[key] = {"title": content.get("title", ""), "link": content.get("link", "")}
                    elif key == "World":
                        # Apply your logic for the "World" category
                        news[key] = {"title": content.get("title", ""), "link": content.get("link", "")}
                    elif key == "Africa":
                        # Apply your logic for the "Africa" category
                        news[key] = {"title": content.get("title", ""), "link": content.get("link", "")}
                    elif key == "Rwanda":
                        # Apply your logic for the "Africa" category
                        news[key] = {"title": content.get("title", ""), "link": content.get("link", "")}
                    else:
                        # Default logic for other categories
                        news[key] = {"title": content.get("title", ""), "link": content.get("link", "")}
                except requests.exceptions.RequestException as e:
                    # Handle connection or HTTP request errors here
                    print(f"Error fetching news for '{key}': {e}")
                    news[key] = {"title": "Failed to fetch news.", "link": ""}
                    time.sleep(5)  # Wait for a while before retrying

            return news
    def refresh_news(self):
        # Fetch new news data (and process as needed)
        
        # Clear the existing children from the grid
        for child in self.grid.get_children():
            self.grid.remove(child)
        
        # Repopulate the grid
        self.add_news_to_grid()
        
        # Redraw the window to reflect changes (if needed)
        self.show_all()



    def fetch_and_display_news(self):
        self.news = self.fetch_news()
        GLib.idle_add(self.update_news_display)  # Schedule UI update on the main thread

    # def tech_parser(self, soup):
    #     first_article = soup.find("a", class_="post-block__title__link")
    #     if first_article:
    #         title = first_article.text.strip()
    #         link = first_article['href']
    #         return {"title": title, "link": link}
    #     else:
    #         return {"title": "TechCrunch article title not found.", "link": ""}
        

    def tech_parser(self, soup=None):
        # URL of the TechCrunch homepage
        url = "https://techcrunch.com/"

        # Send a GET request to the URL if soup is not provided
        if soup is None:
            response = requests.get(url)

            # Check if the request was successful
            if response.status_code == 200:
                # Parse the HTML content of the page
                soup = BeautifulSoup(response.content, 'html.parser')
            else:
                return {"title": "Failed to retrieve the page.", "link": ""}

        # Extract the text content of the element
        post_picker_div = soup.find('div', class_='wp-block-tc23-post-picker')
        if post_picker_div:
            title_element = post_picker_div.find('h2', class_='wp-block-post-title')
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
            # Find all h2 tags with the data-testid 'card-headline'
            container = soup.find_all('h2', {'data-testid': 'card-headline'})

            if not container:
                print("No container found with data-testid 'card-headline'.")
                return {"title": "BBC container not found.", "link": ""}

            for h2_tag in container:
                headline_text = h2_tag.get_text(strip=True)

                # Traverse upwards to find the parent div containing the link
                parent_div = h2_tag.find_parent('div')
                while parent_div:
                    link_tag = parent_div.find('a', href=True)
                    if link_tag:
                        link = link_tag['href']
                        if not link.startswith('http'):
                            link = "https://www.bbc.com" + link
                        return {"title": headline_text, "link": link}
                    parent_div = parent_div.find_parent('div')
                print("No parent div with link found for headline.")

            print("Specific headline not found.")
            return {"title": "Target headline not found.", "link": ""}

        except Exception as e:
            print(f"An error occurred: {e}")
            return {"title": "Error occurred while parsing.", "link": ""}


    def africa_parser(self, soup):
        try:
            # Find all h2 tags with the data-testid 'card-headline'
            container = soup.find_all('h2', {'data-testid': 'card-headline'})

            if not container:
                print("No container found with data-testid 'card-headline'.")
                return {"title": "BBC container not found.", "link": ""}

            for h2_tag in container:
                headline_text = h2_tag.get_text(strip=True)

                # Traverse upwards to find the parent div containing the link
                parent_div = h2_tag.find_parent('div')
                while parent_div:
                    link_tag = parent_div.find('a', href=True)
                    if link_tag:
                        link = link_tag['href']
                        if not link.startswith('http'):
                            link = "https://www.bbc.com" + link
                        return {"title": headline_text, "link": link}
                    parent_div = parent_div.find_parent('div')
                print("No parent div with link found for headline.")

            print("Specific headline not found.")
            return {"title": "Target headline not found.", "link": ""}

        except Exception as e:
            print(f"An error occurred: {e}")
            return {"title": "Error occurred while parsing.", "link": ""}

        
    def rwanda_parser(self, soup):
        # Search for the div with class 'article-title'
        link_element = soup.find('div', class_='article-title')
        
        if link_element:
            title = link_element.text.strip()
            # Search for the anchor <a> tag that encloses the article title div
            anchor_tags = soup.select('.nt-home-tabs .article-title a')  # This returns a list
            if anchor_tags:
                # Check if there are any anchor tags in the list
                first_anchor_tag = anchor_tags[0]  # Access the first anchor tag in the list
                link = first_anchor_tag['href']  # Access the 'href' attribute of the first anchor tag
                
                return {"title": title, "link": link}
            else:
                return {"title": title, "link": "Link not found for the article."}
        else:
            return {"title": "Article title not found.", "link": ""}
        

    def fetch_content(self, link, is_tech=False, rwanda_link=None, is_world=False, is_africa=False, is_rwanda=False):
        # print(f"fetch_content called with link: {link}")
        # Initialize the URL and make an HTTP request
        url = link
        response = self.session.get(url)

        # Check if the request was successful
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            time.sleep(2)

            # Check the category and parse content accordingly
            if is_tech:

                # For Tech category
                container = soup.find('div', class_='entry-content wp-block-post-content is-layout-flow wp-block-post-content-is-layout-flow')
                if container:
                    paragraphs = container.find_all('p')
                    content = "\n".join(paragraph.get_text() for paragraph in paragraphs)
                    print(content)
                else:
                    content = "Elements not found."

            elif is_rwanda:
                # For Rwanda category
                    if rwanda_link:
                        content = scrape_rwanda_article_content(link)
                        # content = scrape_rwanda_article_content(url)

            elif is_world:
                
                # For World category
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
                # For Africa category
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

        # Show menu item
        show = Gtk.MenuItem(label="Show")
        show.connect('activate', self.on_show_click)
        self.menu.append(show)

        # Exit menu item
        exit = Gtk.MenuItem(label="Exit")
        exit.connect('activate', self.on_exit_click)
        self.menu.append(exit)

        self.menu.show_all()

        # Popup the menu
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
        # Add a system tray icon
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