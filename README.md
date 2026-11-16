# LinuxNewsScrapping

Run `./news_app.sh start` (or `./news_app.sh restart` if already running).

The dock height is fixed exclusively by four single-line headline rows. Headlines
are one line each so all four stay visible and fully readable. The first column
widens to fit the longest headline (up to half the dock) and pushes the job
columns to the right. Job lists
never increase it. Default job positions use one line each: long position names
are shortened, while category and company names in parentheses remain intact.
Four entries fill the first middle column before continuing into the second.
When both columns cannot hold the remaining entries, **(read more)** replaces
their text with the next page without changing the dock height.

Use **Full** to show complete, wrapped position titles in every normal and IT
job column. The button changes to **Short** for returning to the compact view.
Full text stays inside scrollable panels, so it never increases the dock height.

IT positions from JobInRwanda category **33** appear in a separate far-right
column. They are hidden on startup; use the floating **Show** / **Hide** button
inside that column to toggle them. IT listings load when shown and refresh with
the news while visible. Up to four entries stay in the far-right column. Longer
lists reflow in reading order with at least four entries in an equally sized
middle column, then continue on the right. The middle temporarily replaces the
normal listings; **Hide** restores those listings immediately.

IT entries use `Position (Company name)` when the employer is available. Long
position names are shortened without shortening the parenthesized company.
Both columns display the list directly, without an IT heading.

All category 33 results are included, following additional result pages when
present. The bar stays pinned at the same size; either IT column can scroll if
the full list is still taller than the available space.

News and job feeds refresh in the background, so slow or unavailable sites do
not block Show/Hide. A single refresh timer prevents overlapping requests.
Drag from any non-interactive part of the dock, including headline and job-text
areas. Buttons and scrollbars are excluded from dragging, so Full, Show/Hide,
and (read more) keep their normal click behavior. Interrupted drags automatically
release their mouse grab, so a missed mouse release cannot leave controls
unresponsive later in the session.

Every startup, control press/release/click, drag, refresh, render result, network
failure, and shutdown is written to a rotating persistent log:
`~/.local/state/news-dock/news-dock.log`. Native crash traces are written to
`~/.local/state/news-dock/crash.log`. Use `./news_app.sh logs` to print the most
recent 200 action-log lines. `./news_app.sh status` also prints the active log
location.

Button callbacks are logged as `action_requested`; that entry does not claim
success. `action_confirmed ... result=success` is written only after the actual
labels, wrapping/ellipsis mode, button label, visibility, column split, item
count, and underlying-grid state match the requested result. It also waits for an
actual post-action GTK draw event and verifies that the dock window is viewable
on its target monitor and that every expected output label has a mapped, usable
viewport and Pango text layout. After a monitor move, the dock performs a short
geometry-settle sequence that reapplies its new monitor size and explicitly asks
GTK/X11 to update, allocate, and repaint. The entry includes action latency,
window geometry, viewport and label sizes, paint serials, and diagnostic frame
counters. Load failures, render mismatches, timeouts, and superseded requests are
logged explicitly.

Run the GUI regression checks in an isolated display:
`xvfb-run -a venv/bin/python -B -m unittest discover -s tests -v`.

Click any job line in the middle or right columns (normal or IT) to open a small
popup above the dock with the full position, category and company. Esc, a click
elsewhere, or focus leaving the popup closes it. Dragging still moves the dock;
only a press and release without movement counts as a click.

When IT is shown, its list spreads in reading order over dock columns 2, 3 and 4
(four to a column) once it has more than eight entries; up to eight use columns 3
and 4, and up to four use column 4 only. While IT is hidden, the job columns use
the otherwise empty IT area, starting right after the headline column.

Results are cached in `~/.cache/news-dock/cache.json` (override with
`NEWS_DOCK_CACHE_FILE`). The last good headlines, job categories and IT jobs
appear immediately at startup and are replaced section by section as fresh
results arrive. A failed fetch keeps the cached result instead of showing an
error, and entries older than 24 hours are ignored. Writes are atomic.

Under Mutter/XWayland the dock can stop painting and re-allocating after it is
moved to another monitor, leaving stale content (for example IT lists showing
only their first line). After every visible change (Show/Hide, Full, new
results) the dock checks that GTK really painted and that the job labels got the
height their text needs; if not it logs `paint_stalled` and remaps the window,
at most once every five seconds.

While IT is hidden the normal job listings also use the last dock column (it
shows the Full/Show buttons at the bottom right, so its text keeps a right
margin for them). Up to four entries stay in one column; longer lists spread
evenly over the three columns, and full pages hold four per column with
**(read more)** at the end of the last one. Show replaces the last column with
the IT list and Hide brings the normal listings back.
