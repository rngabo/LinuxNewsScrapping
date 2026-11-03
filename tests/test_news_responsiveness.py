"""Run with: xvfb-run -a venv/bin/python -B -m unittest discover -s tests -v"""

import ctypes
import html
import threading
import time
import unittest
import warnings
from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import news
from gi.repository import GLib, Gtk


def pump_until(predicate, timeout=4):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while True:
        for _ in range(100):
            if not context.pending():
                break
            context.iteration(False)
        if predicate():
            return
        if time.monotonic() >= deadline:
            raise AssertionError("GTK did not reach the expected state")
        time.sleep(0.002)


def settle():
    deadline = time.monotonic() + 0.06
    pump_until(lambda: time.monotonic() >= deadline)


class Feeds:
    def __init__(self):
        self.gates = {kind: threading.Event() for kind in ("news", "jobs", "it")}
        self.started = {kind: threading.Event() for kind in self.gates}
        self.calls = []
        self.fail = set()
        self.titles = {
            "All": ["Website developer"],
            "29": ["International relations officer"],
            "13": ["Economist"],
            "33": [f"IT engineer {i}" for i in range(8)],
        }
        self.release()

    def release(self):
        for gate in self.gates.values():
            gate.set()

    def get(self, session, url, **kwargs):
        category = parse_qs(urlparse(url).query).get("field_job_category_target_id", [None])[0]
        kind = "it" if category == "33" else "jobs" if category else "news"
        self.calls.append((kind, threading.get_ident(), id(session)))
        self.started[kind].set()
        if not self.gates[kind].wait(3):
            raise news.requests.Timeout("Simulated stalled feed")
        if kind in self.fail:
            raise news.requests.Timeout("Simulated network timeout")
        if category:
            content = "".join(
                '<article class="node--type-job"><span class="field--name-title">'
                + html.escape(title)
                + '</span><a href="/employer/example">Example &amp; Co</a></article>'
                for title in self.titles[category]
            )
        elif "newtimes" in url:
            content = '<div class="nt-home-tabs"><div class="article-title"><a href="/article">Rwanda ready</a></div></div>'
        elif "techcrunch" in url:
            content = '<div class="wp-block-techcrunch-card"><h3 class="loop-card__title"><a href="/article">Tech ready</a></h3></div>'
        else:
            content = '<div><a href="/article"><h2 data-testid="card-headline">BBC ready</h2></a></div>'
        response = news.requests.Response()
        response.status_code = 200
        response._content = content.encode()
        response.encoding = "utf-8"
        return response


@unittest.skipUnless(Gtk.init_check()[0], "GTK display required; use xvfb-run")
class NewsResponsivenessTests(unittest.TestCase):
    def setUp(self):
        warning_context = warnings.catch_warnings()
        warning_context.__enter__()
        self.addCleanup(warning_context.__exit__, None, None, None)
        warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"gi\.events")
        self.main_thread = threading.get_ident()
        self.feeds = Feeds()
        self.window = None

        def get_response(session, url, **kwargs):
            return self.feeds.get(session, url, **kwargs)

        self.patchers = [
            patch.object(news.requests.Session, "get", get_response),
            patch.object(news.NewsDock, "create_system_tray_icon"),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.callback_threads = []
        for name in ("update_news_display", "update_jobs_panel", "update_it_jobs_panel"):
            original = getattr(news.NewsDock, name)

            def callback(window, *args, _original=original):
                self.callback_threads.append(threading.get_ident())
                return _original(window, *args)

            patcher = patch.object(news.NewsDock, name, callback)
            patcher.start()
            self.patchers.append(patcher)

    def create_window(self):
        self.window = news.NewsDock()
        self.window.show_all()
        return self.window

    def wait_for_refresh(self):
        pump_until(lambda: not any((self.window.news_loading, self.window.jobs_loading,
                                   self.window.it_jobs_loading)))
        settle()

    def tearDown(self):
        self.feeds.release()
        if self.window is not None:
            self.window.destroy()
            self.wait_for_refresh()
        for patcher in reversed(self.patchers):
            patcher.stop()

    def geometry(self):
        settle()
        return tuple(self.window.get_position()) + tuple(self.window.get_size())

    def visible_it_titles(self):
        labels = []
        if self.window.it_jobs_middle_active:
            labels.append(self.window.jobs_it_middle_label)
        labels.append(self.window.jobs_it_label)
        return [line[2:] for label in labels for line in label.get_text().splitlines()
                if line.startswith("- ")]

    def test_slow_feeds_do_not_block_startup_toggles_or_event_loop(self):
        for gate in self.feeds.gates.values():
            gate.clear()
        started = time.monotonic()
        window = self.create_window()
        self.assertLess(time.monotonic() - started, 0.5)
        window.it_jobs_toggle.clicked()
        pump_until(lambda: all(event.is_set() for event in self.feeds.started.values()))
        original_timer = window.refresh_timer_source
        heartbeats = []

        def heartbeat():
            heartbeats.append(time.monotonic())
            return True

        heartbeat_source = GLib.timeout_add(10, heartbeat)
        try:
            baseline = self.geometry()
            for _ in range(10):
                window.start_news_refresh_timer()
                window.it_jobs_toggle.clicked()
                window.it_jobs_toggle.clicked()
                settle()
            self.assertGreaterEqual(len(heartbeats), 20)
            self.assertLess(max(b - a for a, b in zip(heartbeats, heartbeats[1:])), 0.2)
            self.assertEqual(Counter(call[0] for call in self.feeds.calls),
                             {"news": 1, "jobs": 1, "it": 1})
            self.assertEqual(len({call[2] for call in self.feeds.calls}), 3)
            self.assertEqual(window.refresh_timer_source, original_timer)
            self.assertEqual(self.geometry(), baseline)
        finally:
            GLib.source_remove(heartbeat_source)
            self.feeds.release()
        self.wait_for_refresh()
        self.assertTrue(all(thread != self.main_thread for _, thread, _ in self.feeds.calls))
        self.assertTrue(self.callback_threads)
        self.assertEqual(set(self.callback_threads), {self.main_thread})
        self.assertEqual(len(self.visible_it_titles()), 8)
        self.assertIn("Rwanda ready", window.headline_labels["Rwanda"].get_text())

    def test_default_jobs_load_even_when_news_is_stalled(self):
        self.feeds.gates["news"].clear()
        window = self.create_window()
        pump_until(lambda: not window.jobs_loading)
        self.assertTrue(window.news_loading)
        self.assertIn("Website developer", window.jobs_label.get_text())
        window.it_jobs_toggle.clicked()
        pump_until(lambda: not window.it_jobs_loading)
        settle()
        self.assertEqual(len(self.visible_it_titles()), 8)
        self.assertTrue(window.news_loading)

    def test_long_default_titles_are_cut_paged_and_never_change_dock_height(self):
        expected = []
        for category, name in (("All", "Website"), ("29", "International relations"),
                               ("13", "Economics")):
            self.feeds.titles[category] = [
                f"{category}-{i}: Resource Mobilization & Donor Engagement Specialist "
                "for International Partnerships, Programme Development and Financial Management"
                for i in range(3)
            ]
            expected.extend(f"- {title} ({name}) (Example & Co)"
                            for title in self.feeds.titles[category])
        self.feeds.titles["33"] = [f"IT engineer {i}" for i in range(60)]
        window = self.create_window()
        self.wait_for_refresh()
        default_labels = (window.jobs_label, window.jobs_overflow_label)
        compact = [f"- {news.compact_job_text(line[2:], news.DEFAULT_JOB_LINE_CHARS)}"
                   for line in expected]
        self.assertEqual(default_labels[0].get_text().splitlines(), compact[:4])
        self.assertEqual(default_labels[1].get_text().splitlines(), compact[4:7])
        self.assertTrue(window.jobs_overflow_label.get_visible())
        self.assertTrue(window.jobs_read_more.get_visible())
        for label in default_labels:
            layout = label.get_layout()
            width, height = layout.get_pixel_size()
            self.assertEqual(layout.get_line_count(), len(label.get_text().splitlines()))
            self.assertLessEqual(width, label.get_allocated_width())
            self.assertLessEqual(height, label.get_allocated_height())
            for line in label.get_text().splitlines():
                self.assertIn("(Example & Co)", line)
                self.assertIn("…", line)

        baseline = self.geometry()
        work = window.dock_workarea
        self.assertEqual(baseline[3], window.headline_height())
        self.assertEqual(baseline[1] + baseline[3], work.y + work.height)
        self.assertLess(baseline[3], work.height)
        overflow_x, _ = window.jobs_overflow_label.translate_coordinates(window, 0, 0)
        it_x, _ = window.it_jobs_column.translate_coordinates(window, 0, 0)
        self.assertLessEqual(overflow_x + window.jobs_overflow_label.get_allocated_width(), it_x)

        # Read more replaces both default columns with the missing entries.
        window.jobs_read_more.clicked()
        settle()
        self.assertEqual(default_labels[0].get_text().splitlines(), compact[7:])
        self.assertEqual(default_labels[1].get_text(), "")
        self.assertFalse(window.jobs_read_more.get_visible())
        self.assertEqual(self.geometry(), baseline)

        self.assertFalse(window.it_jobs_visible)
        window.it_jobs_toggle.clicked()
        self.wait_for_refresh()
        self.assertTrue(window.it_jobs_middle_active)
        self.assertEqual(len(self.visible_it_titles()), 60)
        self.assertEqual(window.jobs_it_middle_label.get_text().splitlines(), [
            f"- IT engineer {i} (Example & Co)" for i in range(30)
        ])
        self.assertTrue(window.jobs_it_label.get_text().startswith(
            "- IT engineer 30 (Example & Co)"
        ))
        self.assertAlmostEqual(
            window.it_jobs_middle_panel.get_allocated_width(),
            window.it_jobs_column.get_allocated_width(),
            delta=2,
        )
        self.assertEqual(self.geometry(), baseline)
        window.it_jobs_toggle.clicked()
        settle()
        self.assertEqual(window.default_jobs_grid.get_opacity(), 1)
        self.assertEqual(default_labels[0].get_text().splitlines(), compact[7:])
        self.assertEqual(self.geometry(), baseline)

    def test_default_overflow_appears_after_five_and_clears_when_list_shrinks(self):
        self.feeds.titles["All"] = []
        self.feeds.titles["29"] = []
        self.feeds.titles["13"] = [f"Economist {i}" for i in range(5)]
        window = self.create_window()
        self.wait_for_refresh()
        for count in (5, 6, 4):
            self.feeds.titles["13"] = [f"Economist {i}" for i in range(count)]
            window.refresh_jobs_panel()
            self.wait_for_refresh()
            expected = [f"- Economist {i} (Economics) (Example & Co)" for i in range(count)]
            self.assertEqual(window.jobs_label.get_text().splitlines(), expected[:4])
            self.assertEqual(window.jobs_overflow_label.get_text().splitlines(), expected[4:])
            self.assertEqual(window.jobs_overflow_label.get_visible(), count > 4)
            self.assertFalse(window.jobs_read_more.get_visible())
            self.assertEqual(window.it_jobs_toggle.get_label(), "Show")
            self.assertFalse(window.it_jobs_visible)

    def test_six_it_jobs_use_four_middle_rows_then_two_right_rows(self):
        self.feeds.titles["33"] = [f"IT engineer {i}" for i in range(6)]
        window = self.create_window()
        self.wait_for_refresh()
        baseline = self.geometry()
        window.it_jobs_toggle.clicked()
        self.wait_for_refresh()
        self.assertEqual(window.jobs_it_middle_label.get_text().splitlines(), [
            f"- IT engineer {i} (Example & Co)" for i in range(4)
        ])
        self.assertEqual(window.jobs_it_label.get_text().splitlines(), [
            f"- IT engineer {i} (Example & Co)" for i in range(4, 6)
        ])
        self.assertAlmostEqual(
            window.it_jobs_middle_panel.get_allocated_width(),
            window.it_jobs_column.get_allocated_width(),
            delta=2,
        )
        self.assertEqual(self.geometry(), baseline)

    def test_full_toggle_shows_complete_default_and_it_titles_without_growing(self):
        long_title = (
            "Principal Infrastructure and Cybersecurity Engineering Specialist "
            "for Regional Digital Transformation Programmes"
        )
        self.feeds.titles["All"] = [long_title]
        self.feeds.titles["29"] = []
        self.feeds.titles["13"] = []
        self.feeds.titles["33"] = [f"{long_title} {i}" for i in range(6)]
        window = self.create_window()
        self.wait_for_refresh()
        baseline = self.geometry()
        self.assertIn("…", window.jobs_label.get_text())

        window.jobs_full_toggle.clicked()
        settle()
        self.assertEqual(window.jobs_full_toggle.get_label(), "Short")
        self.assertIn(long_title, window.jobs_label.get_text())
        self.assertIn("(Example & Co)", window.jobs_label.get_text())
        self.assertNotIn("…", window.jobs_label.get_text())
        self.assertTrue(window.jobs_label.get_line_wrap())
        self.assertGreaterEqual(
            window.default_jobs_middle_panel.get_allocated_height(),
            window.middle_jobs_area.get_allocated_height() - 2,
        )
        self.assertGreaterEqual(
            window.default_jobs_overflow_panel.get_allocated_height(),
            window.middle_jobs_area.get_allocated_height() - 2,
        )
        self.assertEqual(self.geometry(), baseline)

        window.it_jobs_toggle.clicked()
        self.wait_for_refresh()
        tech_text = (
            window.jobs_it_middle_label.get_text()
            + "\n"
            + window.jobs_it_label.get_text()
        )
        self.assertIn(f"{long_title} 0", tech_text)
        self.assertIn("(Example & Co)", tech_text)
        self.assertNotIn("…", tech_text)
        self.assertEqual(self.geometry(), baseline)

        window.jobs_full_toggle.clicked()
        settle()
        self.assertEqual(window.jobs_full_toggle.get_label(), "Full")
        self.assertIn("…", window.jobs_label.get_text())
        self.assertFalse(window.jobs_label.get_line_wrap())
        self.assertEqual(self.geometry(), baseline)

    def test_full_confirmation_waits_for_visible_it_results_to_render(self):
        self.feeds.gates["it"].clear()
        window = self.create_window()
        pump_until(lambda: not window.news_loading and not window.jobs_loading)

        window.it_jobs_toggle.clicked()
        pump_until(lambda: self.feeds.started["it"].is_set())
        window.jobs_full_toggle.clicked()
        full_action = window.pending_full_verification
        self.assertIsNotNone(full_action)

        # Verification timers may run, but Loading... is not a successful
        # rendered Full result while the IT output is visible.
        settle()
        self.assertEqual(window.pending_full_verification, full_action)
        self.assertEqual(window.it_jobs_message, "Loading...")

        self.feeds.gates["it"].set()
        self.wait_for_refresh()
        pump_until(lambda: window.pending_full_verification is None)
        self.assertEqual(window.jobs_full_toggle.get_label(), "Short")
        self.assertNotEqual(window.it_jobs_message, "Loading...")
        self.assertEqual(len(self.visible_it_titles()), 8)

    def test_actual_paint_confirms_with_a_frozen_frame_counter(self):
        window = self.create_window()
        self.wait_for_refresh()

        with self.assertLogs(news.LOGGER, level="INFO") as captured:
            window.jobs_full_toggle.clicked()
            action_id = window.pending_full_verification
            self.assertIsNotNone(action_id)
            window.action_frame_counters[action_id] = 10**9
            pump_until(lambda: window.pending_full_verification is None)

        confirmation = "\n".join(captured.output)
        self.assertIn(
            f"action_confirmed id={action_id} control=full-titles result=success",
            confirmation,
        )
        self.assertIn("paints=", confirmation)
        self.assertIn("frames=1000000000->", confirmation)

    def test_real_clicks_work_after_monitor_moves_and_background_starts_drag(self):
        try:
            x11 = ctypes.CDLL("libX11.so.6")
            xtst = ctypes.CDLL("libXtst.so.6")
        except OSError:
            self.skipTest("X11 mouse simulation requires libXtst")
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_ulong]
        xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                            ctypes.c_int, ctypes.c_ulong]
        display = x11.XOpenDisplay(None)
        if not display:
            self.skipTest("X11 display required for real mouse events")
        pointer = news.Gdk.Display.get_default().get_default_seat().get_pointer()
        _, original_x, original_y = pointer.get_position()

        def motion(x, y):
            xtst.XTestFakeMotionEvent(display, -1, int(x), int(y), 0)
            x11.XFlush(display)
            settle()

        def button(down):
            xtst.XTestFakeButtonEvent(display, 1, down, 0)
            x11.XFlush(display)
            settle()

        def center(widget):
            x, y = widget.translate_coordinates(self.window, 0, 0)
            win_x, win_y = self.window.get_position()
            return (win_x + x + widget.get_allocated_width() // 2,
                    win_y + y + widget.get_allocated_height() // 2)

        # Unequal adjacent work areas reproduce moving between differently
        # sized monitors even on a single Xvfb screen.
        screen = news.Gdk.Display.get_default().get_monitor(0).get_geometry()

        left_width = (screen.width * 9) // 16
        right_width = screen.width - left_width

        def monitor(x, width):
            rect = news.Gdk.Rectangle()
            rect.x, rect.y = x, screen.y
            rect.width, rect.height = width, screen.height
            return SimpleNamespace(get_geometry=lambda: rect, get_workarea=lambda: rect)

        left = monitor(screen.x, left_width)
        right = monitor(screen.x + left_width, right_width)
        try:
            with patch.object(news.NewsDock, "get_default_monitor", return_value=left), \
                 patch.object(news.NewsDock, "get_monitor_for_point",
                              side_effect=lambda x, y: left if x < right.get_geometry().x else right):
                window = self.create_window()
                self.wait_for_refresh()

                def click_toggle():
                    before = window.it_jobs_visible
                    baseline = self.geometry()
                    x, y = center(window.it_jobs_toggle)
                    motion(x, y)
                    button(True)
                    # Check during the press: release alone clears a stolen drag.
                    self.assertFalse(window.drag_in_progress)
                    self.assertIsNot(Gtk.grab_get_current(), window)
                    motion(x + 2, y + 1)
                    self.assertEqual(self.geometry(), baseline)
                    button(False)
                    self.wait_for_refresh()
                    self.assertEqual(window.it_jobs_visible, not before)
                    self.assertEqual(window.it_jobs_toggle.get_label(), "Show" if before else "Hide")
                    if not before:
                        self.assertGreater(
                            window.it_jobs_middle_panel.get_allocated_width(),
                            80,
                        )
                        self.assertGreater(
                            window.jobs_it_middle_label.get_allocated_width(),
                            80,
                        )
                    else:
                        self.assertEqual(window.jobs_it_label.get_text(), "")
                        self.assertEqual(window.jobs_it_middle_label.get_text(), "")
                    self.assertEqual(self.geometry(), baseline)

                def click_full_toggle():
                    before = window.full_job_text
                    baseline = self.geometry()
                    x, y = center(window.jobs_full_toggle)
                    motion(x, y)
                    button(True)
                    self.assertFalse(window.drag_in_progress)
                    motion(x + 1, y)
                    self.assertEqual(self.geometry(), baseline)
                    button(False)
                    settle()
                    self.assertEqual(window.full_job_text, not before)
                    self.assertEqual(
                        window.jobs_full_toggle.get_label(),
                        "Full" if before else "Short",
                    )
                    self.assertEqual(self.geometry(), baseline)

                for destination in (left, right):
                    window.dock_to_monitor(destination)
                    pump_until(lambda: window.dock_settle_source is None)
                    self.assertEqual(
                        window.get_size()[0],
                        destination.get_geometry().width,
                    )
                    click_toggle()
                    click_toggle()
                    click_full_toggle()
                    click_full_toggle()

                window.dock_to_monitor(left)
                settle()
                self.assertFalse(hasattr(window, "drag_button"))
                drag_target = window.headline_labels["Rwanda"]
                motion(*center(drag_target))
                button(True)
                self.assertTrue(window.drag_in_progress)
                self.assertIs(Gtk.grab_get_current(), window)
                motion(right.get_geometry().x + right.get_geometry().width // 2,
                       screen.y + screen.height - 100)
                button(False)
                self.assertFalse(window.drag_in_progress)
                self.assertEqual(window.get_position()[0], right.get_geometry().x)
                click_toggle()
                click_toggle()

                # Repeated refreshes must preserve the button's real input area,
                # including when the normal listings change height and overflow.
                for refresh in range(12):
                    self.feeds.titles["All"] = [
                        f"Website developer {refresh}-{i} with product engineering experience"
                        for i in range(1 + refresh % 6)
                    ]
                    window.refresh_news()
                    self.wait_for_refresh()
                    click_toggle()
                    click_toggle()

                # A compositor or interrupted drag may swallow the release.
                # The next unpressed motion must give control back to Show.
                original_release = window.on_dock_button_release
                window.disconnect_by_func(original_release)
                motion(*center(drag_target))
                button(True)
                self.assertTrue(window.drag_in_progress)
                self.assertIs(Gtk.grab_get_current(), window)
                button(False)
                window.connect("button-release-event", original_release)
                motion(*center(window.it_jobs_toggle))
                self.assertFalse(window.drag_in_progress)
                self.assertIsNot(Gtk.grab_get_current(), window)
                click_toggle()
                click_toggle()

                # Recovery also works without moving the mouse after release.
                window.disconnect_by_func(original_release)
                try:
                    motion(*center(drag_target))
                    button(True)
                    self.assertTrue(window.drag_in_progress)
                    button(False)
                    pump_until(lambda: not window.drag_in_progress, timeout=0.5)
                finally:
                    window.connect("button-release-event", original_release)
                self.assertIsNone(window.drag_watch_source)
                self.assertIsNot(Gtk.grab_get_current(), window)
                click_toggle()
                click_toggle()

                # Hiding the dock during a held drag must not leave a grab or
                # polling timer behind when the window is shown again.
                motion(*center(drag_target))
                button(True)
                self.assertTrue(window.drag_in_progress)
                window.hide()
                button(False)
                self.assertFalse(window.drag_in_progress)
                self.assertIsNone(window.drag_watch_source)
                self.assertIsNot(Gtk.grab_get_current(), window)
                window.show()
                settle()
                click_toggle()
                click_toggle()
        finally:
            button(False)
            motion(original_x, original_y)
            x11.XCloseDisplay(display)

    def test_refresh_and_hide_restore_defaults_without_moving_the_dock(self):
        window = self.create_window()
        self.wait_for_refresh()
        baseline = self.geometry()
        window.it_jobs_toggle.clicked()
        self.wait_for_refresh()
        self.assertTrue(window.it_jobs_middle_active)
        expected = [f"{title} (Example & Co)" for title in self.feeds.titles["33"]]
        self.assertEqual(self.visible_it_titles(), expected)
        self.assertNotIn("IT jobs", window.jobs_it_label.get_text())
        self.feeds.titles["All"] = ["Updated website developer"]
        for _ in range(3):
            self.assertTrue(window.refresh_news())
            self.wait_for_refresh()
            self.assertEqual(self.geometry(), baseline)
            self.assertEqual(window.default_jobs_grid.get_opacity(), 0)
        window.it_jobs_toggle.clicked()
        self.assertFalse(window.it_jobs_middle_active)
        self.assertEqual(window.default_jobs_grid.get_opacity(), 1)
        self.assertIn("Updated website developer", window.jobs_label.get_text())
        self.assertEqual(self.geometry(), baseline)

    def test_timeouts_release_refresh_guards_and_allow_recovery(self):
        self.feeds.fail.update(("news", "jobs", "it"))
        window = self.create_window()
        window.it_jobs_toggle.clicked()
        self.wait_for_refresh()
        self.assertIn("Failed to load", window.jobs_label.get_text())
        self.assertIn("Failed to load", window.jobs_it_label.get_text())
        self.assertIn("Failed to fetch", window.headline_labels["Rwanda"].get_text())
        self.feeds.fail.clear()
        window.refresh_news()
        self.wait_for_refresh()
        self.assertIn("Website developer", window.jobs_label.get_text())
        self.assertIn("Rwanda ready", window.headline_labels["Rwanda"].get_text())
        self.assertEqual(len(self.visible_it_titles()), 8)

    def test_late_results_do_not_update_a_destroyed_window(self):
        for gate in self.feeds.gates.values():
            gate.clear()
        window = self.create_window()
        window.it_jobs_toggle.clicked()
        pump_until(lambda: all(event.is_set() for event in self.feeds.started.values()))
        timer = window.refresh_timer_source
        window.destroy()
        self.assertTrue(window.closed)
        self.assertIsNone(GLib.MainContext.default().find_source_by_id(timer))
        self.feeds.release()
        self.wait_for_refresh()
        self.assertEqual(window.news["Rwanda"]["title"], "Loading...")
        self.assertEqual(window.it_job_titles, [])

    def test_long_it_list_preserves_every_position_and_responsive_toggling(self):
        self.feeds.titles["33"] = [f"Infrastructure & security engineer {i}" for i in range(200)]
        window = self.create_window()
        self.wait_for_refresh()
        baseline = self.geometry()
        started = time.monotonic()
        window.it_jobs_toggle.clicked()
        self.wait_for_refresh()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(self.visible_it_titles()), 200)
        self.assertEqual(self.geometry(), baseline)
        window.it_jobs_toggle.clicked()
        self.assertEqual(window.it_jobs_toggle.get_label(), "Show")
        self.assertEqual(window.default_jobs_grid.get_opacity(), 1)


if __name__ == "__main__":
    unittest.main()
