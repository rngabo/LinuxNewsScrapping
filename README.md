# LinuxNewsScrapping

Run `./news_app.sh start` (or `./news_app.sh restart` if already running).

The dock height is fixed exclusively by the four news headline rows. Job lists
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
