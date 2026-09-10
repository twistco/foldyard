"""Dev-posture TUI — workspaces left, detail tabs right (Mode · plugin panels · Doctor).

Layout follows the docker-sandboxes reference: a card per workspace (main + every
worktree) down the left with live stack/devbox status; the right panel shows the
credential posture OF THE SELECTED WORKSPACE — posture is per-worktree now
(per-consumer-registry-plan.md), so moving the highlight repaints the Mode tab, its
daemon status, and the plugin panels for that worktree, and the mode buttons write THAT
worktree's posture (its dev box may still lag until its next `devbox up`, which the Mode
tab calls out). The one supervisor's combined log stays global. Then PLUGIN-contributed panels (e.g.
github's egress Network Log — `registry().tui_panels()`, rendered generically from each
plugin's TuiPanel/PanelData so the TUI names no credential mechanism), and the setup
doctor ("what can I even grant from here" — gcloud/ADC/gh/host.env checks; runs the deep
live-IAM probes on startup, and offers a one-click repair button per FAILING check that a
plugin contributes a `DoctorFix` for — its output streams into the log pane, then doctor
re-runs). The TUI follows the terminal's system light/dark theme (detected via OSC 11).

ALL state changes go through devmode.set_mode — the TUI has no logic of its own, so
it can never drift from what the recipes and the supervisor do. Keep it that way.

Run with `fy tui` (Mac; Textual comes from the foldyard package's deps).
Tested headlessly with `fy tui-test` (Textual's run_test pilot — test_tui.py).

Navigation is zoned (see DevModeTui / ModeGrid / WorkspaceList for the seams):
  • tab strip (nothing focused, the default): ←/→ switch panel; ← off the first tab
    steps out to the workspace list; ↓ enters the panel
  • Mode grid: ←/→ between mode buttons, ↑/↓ between axis rows; the focused button is
    bold-underlined (green = the active mode) and the status line previews it. ← off the
    left edge → workspace list, ↑ off the top row → back to the tab strip, enter switches
  • workspace list: ↑/↓ between workspaces (last row = ＋ new worktree, enter opens the
    branch picker), → hands off to the panel. With a workspace highlighted: c opens Code
    (`fy code`), d starts/stops its dev box (stopping confirms first — an agent session may
    be running inside), s starts/stops the Podman machine (stopping confirms first — it
    takes every workspace's containers down), x removes the worktree (not main — tears
    down its box + stack, archives transcripts, then git-removes it, after a confirm dialog)
  • [ ] switch panel anywhere (when a DataTable owns the arrows) · q quit. The doctor
    (incremental, per-line spinner; a log pane shows the background subprocess calls +
    output) runs deep on startup and re-runs from the Doctor tab's ↻ deep refresh button.
    There is deliberately NO all-off shortcut: blanket-resetting every axis to its default
    would also drop claude/codex keyless auth out from under a live agent session.
"""

from __future__ import annotations

import threading
from typing import cast

from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from . import allowlist, config, configpin, devmode, term
from .plugins import DoctorFix, PanelData, PanelTree, TuiPanel, registry

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


_DOCTOR_GLYPHS = {"ok": ("✓", "green"), "warn": ("○", "yellow"), "fail": ("✗", "red")}


def _tail_text(path, max_bytes: int = 32 * 1024, max_lines: int = 300) -> str:
    """The last ``max_lines`` lines of a plain-text log, reading only the final ``max_bytes`` so a
    long-running supervisor log never gets re-read whole each refresh. "" if the file is absent/
    empty/unreadable. Used to tail host-supervisor.log into the Mode tab."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - max_bytes))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-max_lines:])


def _detect_terminal_theme(timeout: float = 0.2) -> str | None:
    """Follow the terminal's system light/dark theme the way cmux does: query the background
    colour via OSC 11 and map its luminance to ``textual-light`` / ``textual-dark``. Returns
    None when the terminal can't be queried — not a TTY, a headless test, or no reply within
    ``timeout`` — so the caller keeps Textual's default. MUST run before the app takes over the
    terminal (called from ``main`` before ``run``). Mac/Linux only, which the TUI already is."""
    import os
    import select
    import sys
    import termios
    import tty

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        return None
    buf = ""
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b]11;?\x1b\\")  # OSC 11 ; ? ST → "what is your background colour?"
        sys.stdout.flush()
        while "\x1b\\" not in buf and "\x07" not in buf:  # reply ends in ST or BEL
            if not select.select([fd], [], [], timeout)[0]:
                break
            chunk = os.read(fd, 64)
            if not chunk:
                break
            buf += chunk.decode("latin-1", "replace")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return _theme_for_osc11(buf)


def _theme_for_osc11(reply: str) -> str | None:
    """Map an OSC 11 background-colour reply (``…11;rgb:RRRR/GGGG/BBBB…``) to a Textual theme
    name, or None if it doesn't parse. Split out from the terminal I/O so the luminance rule is
    unit-testable without a TTY."""
    import re

    m = re.search(r"11;rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)", reply)
    if not m:
        return None
    # Components come back as 1–4 hex digits; normalise each to 0..1 by its own width.
    r, g, b = (int(c, 16) / (16 ** len(c) - 1) for c in m.groups())
    # Rec. 601 luma; a dark background ⇒ the dark theme.
    return "textual-dark" if (0.299 * r + 0.587 * g + 0.114 * b) < 0.5 else "textual-light"


def _doctor_text(header: str, rows: list[tuple[str, str, str]], spin: int) -> Text:
    """Render doctor rows as a styled Rich Text (NOT a markup string). Details carry
    raw gcloud output — ANSI colour codes, stray brackets — that no escaping fully
    tames for the markup parser; a styled Text never parses the dynamic text, so it
    can't raise MarkupError. A 'running' row shows the current spinner frame."""
    text = Text()
    text.append(header + "\n", style="dim")
    if not rows:
        text.append("…", style="dim")
    for status, name, detail in rows:
        glyph, colour = _DOCTOR_GLYPHS.get(status, (SPINNER[spin % len(SPINNER)], "cyan"))
        text.append(glyph + " ", style=colour)
        text.append(name, style="bold")
        text.append("  ")
        if detail:
            text.append(devmode._strip_ansi(detail))  # belt-and-suspenders for the display
        elif status == "running":
            text.append("…", style="dim")
        text.append("\n")
    return text


def _cmd_log_text(entries: list[dict]) -> Text:
    """The background subprocess calls (command + rc + captured output) as styled Text
    — same no-markup-parsing safety as the doctor rows (gcloud output is arbitrary)."""
    text = Text()
    if not entries:
        text.append("no commands yet", style="dim")
        return text
    for e in entries:
        ok = e.get("rc") == 0
        text.append("$ ", style="dim")
        text.append(e.get("cmd", ""), style="bold" if ok else "bold red")
        text.append(f"  (rc={e.get('rc')})\n", style="dim" if ok else "red")
        out = devmode._strip_ansi(e.get("out", "")).strip()
        if out:
            for line in out.splitlines():
                text.append("    " + line + "\n", style="dim" if ok else "")
    return text


def _render_ws(ws: dict) -> str:
    stack = (
        "engine unreachable"
        if ws["containers"] is None
        else (f"{ws['containers']} containers" if ws["containers"] else "stack down")
    )
    box = "▣ devbox" if ws["devbox"] else "□ no devbox"
    app = (
        f"\n[dim]app[/dim] http://localhost:{ws['app_port']}"
        if ws.get("app_port") is not None
        else ""
    )
    return (
        f"[b]{ws['name']}[/b]  [dim]{ws['branch']}[/dim]\n"
        f"{stack} · {box}{app}\n[dim]{ws['path']}[/dim]"
    )


class WorkspaceCard(ListItem):
    def __init__(self, ws: dict) -> None:
        super().__init__(Static(_render_ws(ws)))
        self.ws = ws

    def refresh_ws(self, ws: dict) -> None:
        self.ws = ws
        try:
            self.query_one(Static).update(_render_ws(ws))
        except Exception:
            pass  # not mounted yet — constructor content is already current


class NewWorktreeCard(ListItem):
    """The always-last row of the workspace list — enter opens the branch picker."""

    def __init__(self) -> None:
        super().__init__(Static("＋ new worktree", id="ws-new"))


class WorkspaceList(ListView):
    """The left column. ↑/↓ are native (cursor); → hands off to the active panel.
    When a real workspace is highlighted, b/c/d/s act on it (open browser / open Code / toggle
    its dev box / toggle the Podman machine); x removes a worktree (not main). Workspace actions
    are hidden on the ＋ new-worktree row (which only takes enter), and x is also hidden on
    main. The stop directions of d and s confirm first (see DevModeTui.toggle_devbox/_machine)."""

    BINDINGS = [
        Binding("right", "to_panel", "panel ▶"),
        Binding("b", "open_browser", "open browser"),
        Binding("c", "open_code", "open Code"),
        Binding("d", "toggle_devbox", "start/stop devbox"),
        Binding("s", "toggle_machine", "start/stop machine"),
        Binding("x", "remove_worktree", "remove worktree"),
    ]

    def _on_real_worktree(self) -> bool:
        """The highlight is on a removable worktree — a real workspace card that isn't main."""
        item = self.highlighted_child
        return isinstance(item, WorkspaceCard) and item.ws["name"] != "main"

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        # Browser/Code/devbox need a workspace; hide them on the new-worktree row. The machine
        # toggle is global, so it stays available everywhere. Remove only makes sense on a real,
        # non-main worktree — hiding it on main / the new-worktree row keeps the footer honest.
        if action in ("open_browser", "open_code", "toggle_devbox") and isinstance(
            self.highlighted_child, NewWorktreeCard
        ):
            return False
        if (
            action == "open_browser"
            and isinstance(self.highlighted_child, WorkspaceCard)
            and self.highlighted_child.ws.get("app_port") is None
        ):
            return False
        if action == "remove_worktree" and not self._on_real_worktree():
            return False
        return True

    def action_to_panel(self) -> None:
        cast("DevModeTui", self.app).action_enter_content()

    def action_open_code(self) -> None:
        item = self.highlighted_child
        if isinstance(item, WorkspaceCard):
            cast("DevModeTui", self.app).open_code(item.ws)

    def action_open_browser(self) -> None:
        item = self.highlighted_child
        if isinstance(item, WorkspaceCard):
            cast("DevModeTui", self.app).open_browser(item.ws)

    def action_toggle_devbox(self) -> None:
        item = self.highlighted_child
        if isinstance(item, WorkspaceCard):
            cast("DevModeTui", self.app).toggle_devbox(item.ws)

    def action_toggle_machine(self) -> None:
        cast("DevModeTui", self.app).toggle_machine()

    def action_remove_worktree(self) -> None:
        item = self.highlighted_child
        if isinstance(item, WorkspaceCard) and item.ws["name"] != "main":
            cast("DevModeTui", self.app).remove_worktree(item.ws)


class ModeGrid(Vertical):
    """The Mode tab's axis rows as a keyboard grid. While a cell (button) is focused it owns
    ←/→/↑/↓ for cell-to-cell movement, and releases focus at the edges: ← off the left column →
    the workspace list, ↑ off the top row → the tab strip.

    The axes flow into one OR two visual columns (``self.cols``, set responsively in ``on_resize``
    from the available width). Navigation is grid-aware: ↑/↓ step a whole visual row (``±cols`` in
    axis order), and ←/→ walk the buttons within an axis, then cross into the adjacent-column axis
    (or, off the left edge of the first column, out to the workspace list). With ``cols == 1`` this
    reduces exactly to the original single-column behaviour."""

    # Two columns once the grid is at least this wide; below it, one. ~40 cols per cell fits the
    # widest axis (its title + up to four min-width-8 buttons + gutter). Tunable.
    _MIN_CELL = 40

    BINDINGS = [
        Binding("up", "move('up')", "↑ row"),
        Binding("down", "move('down')", "↓ row"),
        Binding("left", "move('left')", "◀ mode"),
        Binding("right", "move('right')", "mode ▶"),
    ]

    cols = 1  # visual columns; recomputed on resize (init to 1 so nav works pre-layout)

    def on_resize(self, event) -> None:
        # Pick 1 or 2 columns from the grid's own width and push it to the CSS grid, so a wide
        # terminal uses the horizontal space and a narrow one stays single-column. Capped at 2 —
        # the axes are few and each is wide, so a third column rarely fits and crowds the buttons.
        self.cols = 2 if event.size.width >= 2 * self._MIN_CELL else 1
        self.styles.grid_size_columns = self.cols

    def _rows(self) -> list[AxisRow]:
        return list(self.query(AxisRow))

    @staticmethod
    def _buttons(row: AxisRow) -> list[Button]:
        return list(row.query(Button))

    def first_button(self) -> Button | None:
        rows = self._rows()
        buttons = self._buttons(rows[0]) if rows else []
        return buttons[0] if buttons else None

    def _pos(self) -> tuple[int, int, list[Button]] | None:
        """The focused button's (axis index in grid order, button index, that axis's buttons)."""
        focused = self.app.focused
        for r, row in enumerate(self._rows()):
            buttons = self._buttons(row)
            for c, button in enumerate(buttons):
                if button is focused:
                    return r, c, buttons
        return None

    def _focus_col(self, row: AxisRow, col: int) -> None:
        buttons = self._buttons(row)
        if buttons:
            buttons[min(col, len(buttons) - 1)].focus()

    def action_move(self, direction: str) -> None:
        rows = self._rows()
        pos = self._pos()
        if pos is None:  # nothing focused yet — land on the first cell
            if rows:
                self._focus_col(rows[0], 0)
            return
        # `i` is the focused axis's index in grid order; its visual column is i % cols (the grid
        # fills row-major), so a left edge is the first column, an up edge is the top visual row.
        i, c, buttons = pos
        cols = max(1, self.cols)
        vcol = i % cols
        app = cast("DevModeTui", self.app)
        if direction == "left":
            if c > 0:
                buttons[c - 1].focus()  # previous button in this axis
            elif vcol > 0:
                self._focus_col(rows[i - 1], len(self._buttons(rows[i - 1])))  # axis to the left
            else:
                app.focus_workspaces()  # off the left column → the workspace list
        elif direction == "right":
            if c < len(buttons) - 1:
                buttons[c + 1].focus()  # next button in this axis
            elif vcol < cols - 1 and i + 1 < len(rows):
                self._focus_col(rows[i + 1], 0)  # axis to the right
        elif direction == "up":
            if i - cols >= 0:
                self._focus_col(rows[i - cols], c)  # axis one visual row up, same button column
            else:
                app.leave_grid_top()  # off the top row → the tab strip
        elif direction == "down":
            if i + cols < len(rows):
                self._focus_col(rows[i + cols], c)  # axis one visual row down, same button column


class _FifoGate:
    """Serialization that preserves REQUEST order, for the per-worktree reconcile queue.

    ``take()`` is called on the UI thread — where toggles are ordered by the user's presses —
    and returns a ticket the worker thread enters, blocking until every earlier ticket has been
    released. A plain ``threading.Lock`` is not enough: it gives mutual exclusion but NO
    fairness, so under load the second toggle's worker can win the lock first and apply
    ``1→2`` before ``0→1``, leaving the stack on the posture the user toggled AWAY from. That
    inversion reproduced in ~8% of runs on a loaded machine (it read as a flaky TUI test)."""

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._tail: threading.Event | None = None

    def take(self) -> _Ticket:
        """Claim the next turn. UI-thread only — the caller's order IS the queue's order."""
        with self._mutex:
            predecessor, mine = self._tail, threading.Event()
            self._tail = mine
        return _Ticket(predecessor, mine)


class _Ticket:
    """One turn in a :class:`_FifoGate` queue: waits for its predecessor on enter and releases
    its successor on exit — ALWAYS, so a reconcile that raises can't stall the queue behind it.
    The wait is unbounded: a ticket is taken immediately before its worker is submitted, so the
    only way it never releases is the app tearing down, which ends the process anyway."""

    def __init__(self, predecessor: threading.Event | None, mine: threading.Event) -> None:
        self._predecessor, self._mine = predecessor, mine

    def __enter__(self) -> _Ticket:
        if self._predecessor is not None:
            self._predecessor.wait()
        return self

    def __exit__(self, *exc: object) -> None:
        self._mine.set()


class DevModeTui(App):
    TITLE = f"{config.project()} dev posture"
    SUB_TITLE = str(config.mode_file())
    # Start with nothing focused (the "tab strip"): ◀/▶ switch panels, ↓ enters the
    # active panel. Without this Textual auto-focuses a widget and steals the arrows.
    AUTO_FOCUS = None
    BINDINGS = [
        # ◀/▶ move along the tab strip; ◀ off the first tab steps out to the workspace
        # list (mirrors ◀ off the Mode grid's left edge). Not while focus is in the Mode
        # grid — its own bindings intercept the arrows for cell-to-cell movement.
        ("left", "nav_left", "◀ list/panel"),
        ("right", "nav_right", "panel ▶"),
        ("down", "enter_content", "↓ select"),
        # Bracket alternates: pure panel cycling, for when a focused DataTable owns the
        # arrows (these always switch tabs, never step out to the list).
        Binding("[", "prev_tab", "◀ panel", show=False),
        Binding("]", "next_tab", "panel ▶", show=False),
        # No `o` ALL OFF: blanket-resetting every axis would drop claude/codex keyless auth out
        # from under a live agent session. No d/D doctor either — the doctor runs deep on startup
        # and re-runs from the Doctor tab's ↻ deep refresh button (the `d` key now belongs to the
        # workspace list's dev-box toggle).
        ("a", "allow_host", "allow host"),
        # The wall pane's other half: decline a recommended host / revoke a grant. Gated to the
        # Network Log tab in check_action, like `a`.
        ("x", "wall_answer", "reject/revoke"),
        ("q", "quit", "quit"),
    ]
    CSS = """
    /* The detail pane fills the space left of the fixed-width workspace list. Without an
       explicit 1fr, TabbedContent's width resolves to `auto` (its widest content) and
       overflows past the right edge, so its descendants wrap to an off-screen width and
       the visible text looks truncated. 1fr makes it take exactly the remaining columns
       and stay responsive to terminal resize. */
    TabbedContent { width: 1fr; }
    #workspaces { width: 42; border-right: solid $surface; }
    #workspaces ListItem { padding: 1 1; margin: 0 0 1 0; border: round $surface; height: auto; }
    #workspaces ListItem.-highlight { border: round $accent; }
    #ws-new { color: $text-muted; text-style: italic; }
    /* The Mode tab is split into two scroll regions: the axes (+ banner/hint/host-status) on top,
       the host-supervisor log below. Both flex; the top gets the larger share. Each scrolls its own
       overflow, so neither clips the other (the axes used to push the log off-screen / clip the
       last axes when the list grew). */
    #mode-top { height: 2fr; }
    #host-log-wrap { height: 1fr; margin: 1 1 0 1; border-top: solid $surface; }
    /* The axis rows lay out as a grid so wide terminals use two columns (set responsively in
       ModeGrid.on_resize); narrow ones stay single-column. grid-rows: auto sizes each row to its
       content instead of stretching it to an even fraction. */
    /* Fixed grid-rows, NOT auto: the grid's auto row-sizing under-measures a nested container (it
       sees the tallest single child — the 3-row button strip — not title(1)+buttons(3)+status
       stacked) and clips the row. 6 fits title + buttons + a 2-line status; grid-gutter adds the
       row spacing (so AxisRow carries no vertical margin, which would eat the fixed height). */
    #mode-grid { layout: grid; grid-size: 1; grid-rows: 6; grid-gutter: 1 2; height: auto; }
    AxisRow { height: auto; width: 1fr; margin: 0 1; }
    AxisRow .axis-title { text-style: bold; }
    AxisRow Horizontal { height: auto; }
    AxisRow Button { margin-right: 1; min-width: 8; }
    /* green = active mode (variant); the keyboard selection is shown by bold-underline
       text rather than a border, so the button keeps its exact size (a focus border
       would eat into the box and shift the rows below). */
    AxisRow Button:focus { text-style: bold underline; }
    AxisRow .axis-status { color: $text-muted; margin-left: 1; height: auto; }
    #banner { margin: 0 1; color: $warning; text-style: bold; height: auto; }
    #ws-hint { margin: 0 1; color: $text-muted; height: auto; }
    #host-status { height: auto; margin: 1 1 0 1; color: $text-muted; }
    #host-log { height: auto; color: $text-muted; }
    .panel-summary { margin: 0 1; height: auto; }
    /* Tree-kind plugin panels (the Network Log groups requests by host) fill the pane + scroll. */
    Tree { height: 1fr; }
    /* The Network Log splits: the request log on top, the wall-management pane below (grants +
       pending `[proxy] recommend` offers). The id selector out-specifies the generic Tree rule. */
    #network-tree { height: 3fr; }
    #network-manage-summary { margin-top: 1; border-top: solid $surface; padding-top: 1; }
    #network-manage { height: 2fr; }
    #doctor-out { margin: 0 1; height: auto; }
    #doctor-fixes { height: auto; margin: 0 1; }
    #doctor-fixes Button { margin: 0 1 0 0; min-width: 12; }
    #doctor-log-wrap { height: 1fr; margin: 1 1 0 1; border-top: solid $surface; }
    #doctor-log { height: auto; color: $text-muted; }
    NewWorktreeScreen { align: center middle; }
    #wt-dialog { width: 70; height: auto; max-height: 90%; border: thick $primary;
                 background: $surface; padding: 1 2; }
    #wt-title { text-style: bold; height: auto; }
    #wt-input { margin: 1 0; }
    #wt-list { height: auto; max-height: 12; border: round $surface; }
    #wt-list ListItem { padding: 0 1; }
    #wt-hint { color: $text-muted; margin-top: 1; height: auto; }
    AllowHostScreen { align: center middle; }
    #allow-dialog { width: 70; height: auto; max-height: 90%; border: thick $primary;
                    background: $surface; padding: 1 2; }
    #allow-title { text-style: bold; height: auto; }
    #allow-list { height: auto; max-height: 6; border: round $surface; margin: 1 0; }
    #allow-list ListItem { padding: 0 1; }
    #allow-hint { color: $text-muted; height: auto; }
    /* The adopt gate. The diff is the reviewable part, so it gets its own scroll pane — a
       branch can add any number of lines and none of them may be silently cut off. */
    AdoptConfigScreen { align: center middle; }
    #adopt-dialog { width: 84; height: auto; max-height: 90%; border: thick $primary;
                    background: $surface; padding: 1 2; }
    #adopt-title { text-style: bold; height: auto; }
    #adopt-body { height: auto; margin: 1 0; }
    #adopt-diff { height: auto; max-height: 16; border: round $surface; margin-bottom: 1; }
    #adopt-diff-body { height: auto; }
    #adopt-hint { color: $text-muted; height: auto; }
    /* The destructive worktree-removal confirmation. $error border to signal "this deletes". */
    RemoveWorktreeScreen { align: center middle; }
    #rm-dialog { width: 74; height: auto; max-height: 90%; border: thick $error;
                 background: $surface; padding: 1 2; }
    #rm-title { text-style: bold; height: auto; }
    #rm-body { height: auto; margin: 1 0; }
    #rm-buttons { height: auto; margin-top: 1; }
    #rm-buttons Button { margin-right: 2; min-width: 12; }
    #rm-hint { color: $text-muted; height: auto; margin-top: 1; }
    /* The stop-devbox / stop-machine confirmation. $warning (not $error): stopping is
       disruptive (kills whatever runs inside) but recoverable with a plain start. */
    ConfirmScreen { align: center middle; }
    #cf-dialog { width: 74; height: auto; max-height: 90%; border: thick $warning;
                 background: $surface; padding: 1 2; }
    #cf-title { text-style: bold; height: auto; }
    #cf-body { height: auto; margin: 1 0; }
    #cf-buttons { height: auto; margin-top: 1; }
    #cf-buttons Button { margin-right: 2; min-width: 12; }
    #cf-hint { color: $text-muted; height: auto; margin-top: 1; }
    """

    def __init__(self, start_theme: str | None = None) -> None:
        super().__init__()
        # Theme to adopt on mount (the terminal's system light/dark, detected in `main` before we
        # took over the terminal); None ⇒ keep Textual's default. Applied in on_mount, not here,
        # so the reactive's watcher runs against a live app.
        self._start_theme = start_theme
        # Plugin-contributed tabs (data-only TuiPanels) — snapshot once; the panels are
        # static, only their data changes (refreshed on a timer). The TUI names no specific
        # credential mechanism: github's Network Log arrives via this hook like any other.
        self._panels = registry().tui_panels()
        # Plugin-contributed one-click doctor repairs (DoctorFix). A button per fix is mounted in
        # the Doctor tab and shown only while its check FAILS — again named by no plugin here.
        self._fixes = list(registry().doctor_fixes())
        # Per-worktree reconcile serialization (see _reconcile_off_thread). Created/looked up on
        # the UI thread only; a ticket is held by its worker thread for the whole compose run.
        self._reconcile_gates: dict[str, _FifoGate] = {}
        # Workspaces with a dev-box operation in flight (see _run_devbox): the card's devbox flag
        # refreshes on a 5s timer, so without this a second `d` mid-`fy box up|down` would launch
        # the same (or the opposite) operation concurrently against the same container.
        self._devbox_busy: set[str] = set()

    def _pane_ids(self) -> list[str]:
        """The tab ids in display order: Mode, then each plugin panel, then Doctor."""
        return ["tab-mode", *(f"tab-{p.id}" for p in self._panels), "tab-doctor"]

    # ── layout ───────────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield WorkspaceList(id="workspaces")
            with TabbedContent(initial="tab-mode"):
                with TabPane("Mode", id="tab-mode"):
                    # The axes (+ banner/hint/host-status) live in their own scroll region so a tall
                    # axis list — there are more axes now (gcp/github/penpot/capture/dump/claude/
                    # codex) than fit a short terminal — scrolls instead of pushing the host-log off
                    # the bottom or clipping the last axes (claude/codex) out of reach. Keyboard
                    # focus auto-scrolls this region as you arrow down to a clipped axis.
                    with VerticalScroll(id="mode-top"):
                        yield Static("", id="banner")
                        with ModeGrid(id="mode-grid"):
                            axes = devmode.axes()  # cache: rebuilds the registry lookups otherwise
                            emergency = devmode.emergency()
                            for axis, rungs in axes.items():
                                yield AxisRow(axis, rungs, emergency.get(axis, ()))
                        yield Static("", id="ws-hint")
                        # The host-daemon supervisor (`fy host` / `foldyard host`) is launched by
                        # `fy up` and is machine-scoped (not owned by this TUI) — so we don't start
                        # or stop it here, just show its state and TAIL its log
                        # (host-supervisor.log). If it's down, Doctor explains why box egress fails.
                        yield Static("", id="host-status")
                    with VerticalScroll(id="host-log-wrap"):
                        yield Static("", id="host-log")
                for panel in self._panels:
                    with TabPane(panel.title, id=f"tab-{panel.id}"):
                        yield Static("", id=f"{panel.id}-summary", classes="panel-summary")
                        if panel.kind == "tree":
                            # Groups (e.g. hosts) expand to their rows; root hidden so the groups
                            # are the top level. Built/reconciled in _refresh_tree_panel.
                            tree: Tree = Tree("", id=f"{panel.id}-tree")
                            tree.show_root = False
                            yield tree
                        else:
                            yield DataTable(id=f"{panel.id}-table", cursor_type="row")
                        if panel.id == "network":
                            # The egress-wall management pane, below the log: the store's grants
                            # (revoke with x) + the recommendations still awaiting an answer —
                            # the ADOPTED config's `[proxy] recommend` and the declared plugins'
                            # own install hosts (allow with a, decline with x). A proxy-tab
                            # special case like the `a` binding itself — the generic TuiPanel
                            # contract stays data-only.
                            yield Static("", id="network-manage-summary", classes="panel-summary")
                            yield DataTable(id="network-manage", cursor_type="row")
                with TabPane("Doctor", id="tab-doctor"):
                    yield Static("running checks…", id="doctor-out")
                    # The always-visible ↻ deep refresh (the doctor's only manual trigger — the
                    # old d/D shortcuts are gone), then a button per plugin-contributed DoctorFix,
                    # shown only while its check fails (toggled in _update_fix_buttons). Pressing
                    # a fix runs it, streams its output into the log pane below, then re-runs
                    # doctor.
                    with Horizontal(id="doctor-fixes"):
                        yield Button("↻ deep refresh", id="doctor-refresh")
                        for i, fix in enumerate(self._fixes):
                            yield Button(fix.label, id=f"fix-{i}", variant="primary")
                    with VerticalScroll(id="doctor-log-wrap"):
                        yield Static("", id="doctor-log")
        yield Footer()

    def on_mount(self) -> None:
        if self._start_theme:
            self.theme = self._start_theme  # adopt the detected system light/dark theme
        self._doctor_rows: list[tuple[str, str, str]] = []
        self._doctor_header = ""
        self._spin = 0
        # Doctor-fix state: button-id → DoctorFix, all hidden until a check fails; `_fixing`
        # guards against re-entrancy while one runs; `_fix_output` is its streamed log.
        self._fix_buttons = {f"fix-{i}": fix for i, fix in enumerate(self._fixes)}
        self._fixing = False
        self._fix_output: Text | None = None
        for bid in self._fix_buttons:
            self.query_one(f"#{bid}", Button).display = False
        # The host supervisor isn't owned by this TUI (it's launched by `fy up`, machine-scoped);
        # we only show its state + tail its log. Render once now, then on the refresh_mode timer.
        self._render_host_status()
        self._render_host_log()
        for panel in self._panels:
            if panel.kind == "table":
                self.query_one(f"#{panel.id}-table", DataTable).add_columns(*panel.columns)
        # The wall-management pane (present only when the proxy contributes the Network Log).
        # `_manage_keys` maps its cursor rows back to (kind, host) — see _manage_data.
        self._manage_keys: list[tuple[str, str]] = []
        self._manage_sig: tuple = ()
        manage = self.query("#network-manage")
        if manage:
            manage.first(DataTable).add_columns("", "host", "why / expiry")
        # Tree-panel rebuild guard: panel-id → last (key, child-count) signature, so a tree only
        # rebuilds when its data changed (otherwise the user's expand/collapse state is left alone).
        self._tree_sigs: dict[str, tuple] = {}
        self.refresh_workspaces()
        self._select_launch_workspace()
        self.refresh_mode()
        self.refresh_panels()
        self._run_doctor(deep=True)  # deep (live IAM probes) on startup; renders as rows land
        self.set_interval(0.12, self._tick_doctor)
        # 3s, not 1s: refresh_mode TCP-probes every daemon port (devmode.daemon_status) to show
        # up/down, so a tighter interval just hammers the proxy with empty liveness connections.
        self.set_interval(3.0, self.refresh_mode)
        self.set_interval(1.0, self.refresh_panels)
        self.set_interval(5.0, self.refresh_workspaces)

    # ── left column ──────────────────────────────────────────────────────────────
    def _workspace_list(self) -> WorkspaceList | None:
        """The left column, or ``None`` when it isn't in the active screen — during startup
        before compose mounts it, mid-teardown after it's detached, or while a modal is on top.
        The 1s/3s/5s timers and the async ``Highlighted`` handler all reach the per-worktree
        binding through here, so they must tolerate its transient absence (the same NoMatches
        race refresh_mode/refresh_panels already guard) rather than crash on a stray tick."""
        lists = self.query("#workspaces")
        return lists.first(WorkspaceList) if lists else None

    def refresh_workspaces(self) -> None:
        lv = self._workspace_list()
        if lv is None:
            return
        spaces = devmode.workspaces()
        cards = list(lv.query(WorkspaceCard))
        if len(cards) != len(spaces):
            index = lv.index
            lv.clear()
            for ws in spaces:
                card = WorkspaceCard(ws)
                lv.append(card)
                card.refresh_ws(ws)
            lv.append(NewWorktreeCard())  # always the last row
            lv.index = min(index or 0, len(spaces) - 1)
        else:
            for card, ws in zip(cards, spaces, strict=False):
                card.refresh_ws(ws)
        self.refresh_ws_hint()

    def _select_launch_workspace(self) -> None:
        """Open on the checkout `fy tui` was launched from. The list shows EVERY workspace
        wherever it runs (devmode.workspaces anchors on the primary checkout), so from a worktree
        the useful row is that worktree's own — highlighting it makes the Mode tab, the panels and
        every workspace action land on the checkout you were standing in, which is what the
        `WORKTREE=<name>` prefix would have selected on the command line. Runs ONCE at mount:
        later refreshes must never yank the highlight back from wherever the human moved it.
        Unknown name (CWD outside every checkout, or a dir git no longer tracks) ⇒ leave row 0."""
        lv = self._workspace_list()
        if lv is None:
            return
        name = devmode.current_workspace()
        for i, card in enumerate(lv.query(WorkspaceCard)):
            if card.ws["name"] == name:
                lv.index = i
                return

    @property
    def selected_workspace(self) -> dict | None:
        lv = self._workspace_list()
        if lv is None:
            return None
        item = lv.highlighted_child
        return item.ws if isinstance(item, WorkspaceCard) else None

    def on_list_view_highlighted(self, _event) -> None:
        # A Highlighted queued just before quit is still DISPATCHED after the app has stopped and
        # its screens have been popped (captured mid-failure: is_running False, screen stack
        # empty). Everything below repaints the DOM, and refresh_bindings reaches through
        # `self.screen` — so unguarded, the message loop raises ScreenStackError on the way out:
        # a crash traceback instead of a clean exit, and in the suite a failure pinned on
        # whichever test happened to be running. A stopped app has nothing to repaint.
        if not self.is_running:
            return
        self.refresh_ws_hint()
        # The list's c/x bindings are gated on which row is highlighted (open Code / remove are
        # hidden on the ＋ row and, for remove, on main). Re-evaluate so the footer tracks the move.
        self.refresh_bindings()
        # Posture is per-worktree — moving the highlight changes WHOSE posture the Mode tab + panels
        # show, so repaint them immediately (don't wait for the 3s/1s timers). Guarded: these run
        # during startup before the panes mount (refresh_mode/refresh_panels swallow NoMatches).
        self.refresh_mode()
        self.refresh_panels()

    def on_list_view_selected(self, event) -> None:
        if isinstance(event.item, NewWorktreeCard):
            self.push_screen(NewWorktreeScreen(), self._after_new_worktree)

    def _after_new_worktree(self, result: tuple[str, str] | None) -> None:
        if not result:
            return
        name, branch = result
        self.notify(f"creating worktree '{name}' on branch '{branch}'…")

        def work() -> None:
            rc, out = devmode.create_worktree(name, branch)
            tail = out.splitlines()[-1] if out else ""
            self.call_from_thread(
                self.notify,
                (f"✓ worktree '{name}' ready" if rc == 0 else f"✗ worktree failed: {tail}"),
                severity=("information" if rc == 0 else "error"),
            )
            self.call_from_thread(self.refresh_workspaces)

        self.run_worker(work, thread=True)

    def remove_worktree(self, ws: dict) -> None:
        """Confirm, then tear down + remove the worktree off the UI thread (it shells out to
        `fy worktree remove --yes`, which stops the box, nukes the stack, and git-removes it)."""
        self.push_screen(RemoveWorktreeScreen(ws), lambda ok: self._after_remove_worktree(ws, ok))

    def _after_remove_worktree(self, ws: dict, confirmed: bool | None) -> None:
        if not confirmed:
            return
        name = ws["name"]
        self.notify(f"removing worktree '{name}' (box + stack teardown)…")

        def work() -> None:
            rc, out = devmode.remove_worktree(name)
            tail = out.splitlines()[-1] if out else ""
            self.call_from_thread(
                self.notify,
                (f"✓ worktree '{name}' removed" if rc == 0 else f"✗ remove failed: {tail}"),
                severity=("information" if rc == 0 else "error"),
            )
            self.call_from_thread(self.refresh_workspaces)

        self.run_worker(work, thread=True)

    def open_code(self, ws: dict) -> None:
        self.notify(f"opening VS Code on '{ws['name']}'…")

        def work() -> None:
            rc, out = devmode.open_code(ws["name"])
            if rc != 0:
                tail = out.splitlines()[-1] if out else ""
                self.call_from_thread(self.notify, f"✗ fy code failed: {tail}", severity="error")

        self.run_worker(work, thread=True)

    def open_browser(self, ws: dict) -> None:
        port = ws.get("app_port")
        if not port:  # defensive: callers gate on this, but never emit localhost:None directly
            self.notify("✗ no app port for this worktree", severity="error")
            return
        self.notify(f"opening http://localhost:{port}…")

        def work() -> None:
            rc, out = devmode.open_browser(ws["name"])
            if rc != 0:
                tail = out.splitlines()[-1] if out else ""
                self.call_from_thread(self.notify, f"✗ fy open failed: {tail}", severity="error")

        self.run_worker(work, thread=True)

    def toggle_machine(self) -> None:
        # Stopping is the destructive direction — it takes EVERY workspace's containers and dev
        # boxes down at once — so it confirms first; starting a stopped machine just runs.
        if devmode.machine_state() == "running":
            self.push_screen(
                ConfirmScreen(
                    title="Stop the Podman machine?",
                    body=(
                        "The machine hosts [b]every[/b] workspace: stopping it takes all "
                        "stacks, dev boxes, and any agent sessions running inside them down "
                        "at once.\n\nStart it again with [b]s[/b] (or `fy up`)."
                    ),
                    confirm_label="Stop machine",
                ),
                lambda ok: self._do_toggle_machine() if ok else None,
            )
        else:
            self._do_toggle_machine()

    def _do_toggle_machine(self) -> None:
        self.notify("toggling the Podman machine…")

        def work() -> None:
            rc, out = devmode.machine_toggle()
            tail = out.splitlines()[-1] if out else ""
            self.call_from_thread(
                self.notify,
                (f"✓ machine {devmode.machine_state()}" if rc == 0 else f"✗ {tail}"),
                severity=("information" if rc == 0 else "error"),
            )
            self.call_from_thread(self.refresh_workspaces)

        self.run_worker(work, thread=True)

    def toggle_devbox(self, ws: dict) -> None:
        """Start the highlighted workspace's dev box, or — after a confirmation, since an agent
        session (`fy claude` / `fy codex`) may be running inside it — stop it. Both directions
        shell out to `fy box up|down` off the UI thread (up warms deps, down is teardown)."""
        if not ws["devbox"]:
            self._start_devbox(ws)
            return
        self.push_screen(
            ConfirmScreen(
                title=f"Stop the dev box for [b]{escape(ws['name'])}[/b]?",
                body=(
                    f"Stops + removes [dim]{escape(ws['project'])}-devbox[/dim]. Anything "
                    "running inside it — including a live `fy claude` / `fy codex` agent "
                    "session — is killed; the stack's other containers keep running and "
                    "login/CLI volumes are kept.\n\nStart it again with [b]d[/b] "
                    "(or `fy box up`)."
                ),
                confirm_label="Stop box",
            ),
            lambda ok: self._run_devbox(ws, up=False) if ok else None,
        )

    def _start_devbox(self, ws: dict) -> None:
        """Bring a box up, resolving the adopt gate FIRST when this checkout has never been
        adopted (or has drifted since).

        `fy box up` inherits `configpin.gate`, which on a terminal asks whether to adopt. The TUI
        has no terminal to answer on, so the gate now refuses instead of hanging — correct, but it
        leaves a fresh worktree unable to start its box from here at all. So ask the same question
        as a modal and adopt in-process, keeping the decision an operator's rather than skipping
        it: a new worktree means a new branch, and branch-carried config is exactly the channel
        ADR-0022 closed (adopting is what lets `[proxy] passthrough` / `[[inject]].host` reach the
        host). First adoption is the most important one to see, not the least.
        """
        try:
            name = ws["name"]
            drift = configpin.inspect(devmode.worktree_config("" if name == "main" else name))
        except Exception as e:  # never let the gate's own failure block starting a box
            self.notify(f"couldn't check the adopted config ({e})", severity="warning")
            self._run_devbox(ws, up=True)
            return
        if drift.pinned_exists and not drift.changed:
            self._run_devbox(ws, up=True)
            return
        try:
            # What this branch changes about the config the host already runs for main — the only
            # review a first adoption HAS. Guarded separately: it reads main's pin, and losing the
            # comparison must not lose the question.
            baseline = configpin.main_baseline(drift) if not drift.pinned_exists else None
        except Exception as e:
            self.notify(f"couldn't compare against main's config ({e})", severity="warning")
            baseline = None

        def _after(answer: str | None) -> None:
            if answer != "adopt":
                return  # ignore/cancel: the box would only hit the same refusal, so don't start
            try:
                # What the modal SHOWED, not whatever the checkout holds by now: a mismatch means
                # the tree moved while the question was open, so ask again over the new tree. The
                # baseline is the same check on main's side — a modal can stay open indefinitely,
                # and "nothing new vs main" expires when main adopts something else.
                configpin.adopt(
                    drift.cfg,
                    reviewed=drift.tree_digest(),
                    baseline=baseline.digest if baseline is not None else None,
                )
            except configpin.ReviewStale as e:
                self.notify(f"{e} — asking again", severity="warning")
                self._start_devbox(ws)
                return
            except Exception as e:
                self.notify(f"adopt failed: {e}", severity="error")
                return
            self.notify(f"✓ adopted config for '{ws['name']}'")
            self._run_devbox(ws, up=True)

        self.push_screen(AdoptConfigScreen(drift, baseline), _after)

    def _run_devbox(self, ws: dict, up: bool) -> None:
        name = ws["name"]
        # One in-flight box operation per workspace: marked HERE on the UI thread (both `d`
        # presses land there, so the check-then-add can't race) and cleared in the worker's
        # finally — success or failure, up or down — so a failed run never wedges the toggle.
        if name in self._devbox_busy:
            self.notify(f"[{name}] a dev-box operation is already running…", severity="warning")
            return
        self._devbox_busy.add(name)
        self.notify(f"{'starting' if up else 'stopping'} dev box for '{name}'…")

        def work() -> None:
            try:
                rc, out = (devmode.box_up if up else devmode.box_down)(name)
                tail = out.splitlines()[-1] if out else ""
                self.call_from_thread(
                    self.notify,
                    (
                        f"✓ [{name}] dev box {'up' if up else 'stopped'}"
                        if rc == 0
                        else f"✗ [{name}] box {'up' if up else 'down'} failed: {tail}"
                    ),
                    severity=("information" if rc == 0 else "error"),
                )
            finally:
                self._devbox_busy.discard(name)  # atomic (GIL) — safe from the worker thread
                self.call_from_thread(self.refresh_workspaces)

        self.run_worker(work, thread=True)

    def refresh_ws_hint(self) -> None:
        lv = self._workspace_list()
        if lv is None:  # transient: list not mounted yet / already torn down (see _workspace_list)
            return
        item = lv.highlighted_child
        if isinstance(item, NewWorktreeCard):
            self.query_one("#ws-hint", Static).update(
                "[b]new worktree[/b]: enter → pick a branch (search, or type a new name)"
            )
            return
        ws = self.selected_workspace
        if ws is None:
            return
        # This workspace's own posture (bound), so the box-vs-mode drift hint compares the right one
        with config.using(self._worktree_cfg()):
            mode = devmode.read()["mode"]
        hint = devmode._box_env_hint(mode, ws["project"]) if ws["devbox"] else None
        prefix = f"[b]{ws['name']}[/b]: "
        wt = "" if ws["name"] == "main" else f"just wt {ws['name']} "
        self.query_one("#ws-hint", Static).update(
            prefix
            + (
                (hint and f"{hint.replace('fy box up', wt + 'box up')}")
                or ("devbox env matches the mode" if ws["devbox"] else "no devbox running")
            )
        )

    # ── per-worktree binding (the selected workspace is the consumer) ──────────────
    def _active_worktree(self) -> str:
        """The worktree key of the highlighted workspace (``""`` = main). Posture is per-worktree
        now (ADR-0016), so the Mode tab + its actions act on the SELECTED card — not a
        single host-wide posture — which is what a human staring at one card expects."""
        ws = self.selected_workspace
        return "" if ws is None or ws["name"] == "main" else ws["name"]

    def _worktree_cfg(self) -> config.Config:
        """The resolved config for the highlighted workspace — bind it (``with config.using(…)``)
        around any posture read/write so it targets that worktree's mode file, ports, and logs."""
        return devmode.worktree_config(self._active_worktree())

    # ── Mode tab ─────────────────────────────────────────────────────────────────
    def refresh_mode(self) -> None:
        try:
            self._refresh_mode()
        except NoMatches:
            # The 3s timer can outlive (or precede) the axis widgets: it may fire after teardown
            # has detached the AxisRows' buttons, or before compose has mounted them. Skip the
            # tick rather than crash the app on a transient NoMatches (same race refresh_panels
            # guards against — a loaded CI box keeps an app alive past the interval mid-shutdown).
            pass

    def _refresh_mode(self) -> None:
        # Read the SELECTED workspace's posture (bound to its config) so the grid + status reflect
        # that worktree, not a host-wide global. The supervisor log below is the one supervisor's,
        # it's read UNBOUND (outside the `with`).
        cfg = self._worktree_cfg()
        with config.using(cfg):
            state = devmode.read()
            mode, expires = state["mode"], state["expires"]
            daemons = devmode.daemon_status(mode)
            spec_by_axis = devmode.axis_daemon()  # axis → its daemon name (plugin-declared)
            rungs = devmode.axes()
            defaults = devmode.axis_defaults()
            blurb = devmode.mode_blurb()
            emergency_rungs = devmode.emergency()
            degraded = devmode.degraded_capabilities(mode)
        self._sync_mode_rows(rungs, emergency_rungs)
        focused = self.focused
        for row in self.query(AxisRow):
            # A worktree on a branch that declares a different plugin set may not have this axis —
            # show the row inert rather than KeyError on mode[row.axis] (the grid is from main).
            if row.axis not in rungs:
                for button in row.query(Button):
                    button.variant = "default"
                row.query_one(".axis-status", Static).update(
                    "[dim](not configured for this workspace)[/dim]"
                )
                continue
            current = mode[row.axis]
            emergency = emergency_rungs.get(row.axis, ())
            selected = None  # the value whose button currently has keyboard focus
            for value in rungs[row.axis]:
                button = row.query_one(f"#{row.axis}-{value}", Button)
                button.variant = (
                    ("error" if value in emergency else "success")
                    if value == current
                    else "default"
                )
                if button is focused:
                    selected = value
            if selected is not None:
                # The cursor is on this row — describe the SELECTED mode, not the active
                # one, so ↑/↓/←/→ previews what enter would switch to.
                tag = "active" if selected == current else "enter to switch"
                status = f"▸ {selected}: {blurb[(row.axis, selected)]}  [dim]({tag})[/dim]"
            else:
                status = blurb[(row.axis, current)]
                if row.axis in expires:
                    status += f"   expires in {devmode._countdown(expires[row.axis])}"
                dn = spec_by_axis.get(row.axis)  # axis → daemon name; None ⇒ no daemon
                daemon = daemons.get(dn) if dn else None
                # Only flag an ACTIVE axis whose backing daemon isn't serving — an actionable
                # problem. The healthy state is shown ONCE in the host-status footer; repeating
                # "{daemon} :{port} ● up" under every axis that shares the one egress-proxy (github,
                # penpot, capture, claude, codex all do) was just duplicated noise.
                if daemon and current != defaults[row.axis] and not daemon["up"]:
                    status += f"\n{dn} :{daemon['port']} ○ DOWN — run `fy up` (or `fy host`)"
            row.query_one(".axis-status", Static).update(status)
        # The banner stacks every posture-level alarm: the emergency rung (as before) and any
        # probed-and-failing capability (the supervisor's published claim — same source as
        # `fy mode`'s ⚠ DEGRADED and `fy up`'s closing warning), each with the fix from the
        # probe detail.
        alarms = []
        if any(mode.get(a) in emergency_rungs.get(a, ()) for a in rungs):
            alarms.append("⚠ EMERGENCY user-credential mode is ON — auto-reverts at expiry")
        alarms += [f"⚠ {axis} capability DEGRADED — {detail}" for axis, detail in degraded]
        self.query_one("#banner", Static).update("\n".join(alarms))
        # A persistent header so a human always knows WHOSE posture is shown (and where stored).
        self.sub_title = f"{self._active_worktree() or 'main'}  ·  {cfg.mode_file()}"
        self._render_host_status(daemons, self._active_worktree())  # this worktree's daemons
        self._render_host_log()  # the ONE supervisor's combined log (global, unbound)

    def _sync_mode_rows(
        self, rungs: dict[str, tuple[str, ...]], emergency_rungs: dict[str, tuple[str, ...]]
    ) -> None:
        """Mount rows for the selected workspace's registry, since branches can declare different
        plugin axes/rungs."""
        grid = self.query_one("#mode-grid", ModeGrid)
        rows = list(grid.query(AxisRow))
        if [row.axis for row in rows] == list(rungs):
            return
        for row in rows:
            row.remove()
        for axis, values in rungs.items():
            grid.mount(AxisRow(axis, values, emergency_rungs.get(axis, ())))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "doctor-refresh":  # the Doctor tab's manual re-run (replaces the d/D keys)
            self._run_doctor(deep=True)
            return
        if bid.startswith("fix-"):  # a Doctor-tab repair button, not a mode button
            self._run_fix(self._fix_buttons[bid])
            return
        axis, value = bid.split("-", 1)
        # Apply to the SELECTED workspace (bound), so a posture button writes THAT worktree's mode
        # file (+ reconciles its stack) — not a host-wide global. The supervisor picks it up on its
        # next tick and reconciles that worktree's daemons.
        ws_name = self._active_worktree() or "main"
        cfg = self._worktree_cfg()
        # Apply the mode file SYNCHRONOUSLY (fast) so the button highlights immediately, but DEFER
        # the stack reconcile (a slow `docker compose up` subprocess) to a worker thread. Running it
        # inline used to (a) freeze the whole TUI for seconds and (b) bleed raw compose output over
        # the Textual UI — the subprocess inherited the terminal fds, which Textual can't redirect.
        with config.using(cfg):
            try:
                res = devmode.set_mode({axis: value}, reconcile=False)
            except SystemExit as e:  # a mode_issues error refused the combination — surface it
                self.notify(str(e), severity="error", timeout=10)
                return
            is_emergency = value in devmode.emergency().get(axis, ())
        if is_emergency:
            self.notify(
                f"[{ws_name}] {axis}=user armed for {devmode.DEFAULT_TTL // 60} min "
                f"(longer: `fy mode {axis}=user ttl=…`)",
                severity="warning",
            )
        self.refresh_mode()
        self.refresh_ws_hint()
        self._reconcile_off_thread(res, cfg, ws_name)

    def _reconcile_off_thread(self, res: dict, cfg, ws_name: str) -> None:
        """Run the (slow) stack posture reconcile for a just-applied ``set_mode`` result in a
        worker thread — EVERY TUI path that applies a mode must come through here (buttons AND
        the all-off action): an inline reconcile freezes the UI for the compose run's duration
        and, with the default stderr sink, bleeds raw compose output over the Textual UI."""
        if res["prev_posture"] == res["new_posture"]:
            return  # posture signature unchanged → nothing to reconcile, no spinner needed

        # Off-thread reconcile: capture the compose output through a sink (never the terminal fds)
        # and surface a start/finish notify as a lightweight spinner. The captured tail is shown so
        # a slow recreate is visible without the raw bleed.
        self.notify(f"[{ws_name}] reconciling stack ({res['new_profiles'] or 'none'})…")

        # Serialize reconciles PER WORKTREE: two quick toggles would otherwise run two
        # `docker compose` operations against the same stack concurrently (run_worker's
        # exclusive= only cancels same-group workers, it doesn't queue them). The ticket is
        # taken HERE, on the UI thread, so the queue order is the press order — the worker
        # threads then race to start, and a plain lock would let the second toggle apply
        # first, ending on the earlier posture (see _FifoGate).
        gate = self._reconcile_gates.get(ws_name)
        if gate is None:
            gate = self._reconcile_gates[ws_name] = _FifoGate()
        ticket = gate.take()

        def work() -> None:
            lines: list[str] = []
            try:
                with ticket, config.using(cfg):
                    ok = devmode.reconcile_stack(
                        res["prev_posture"], res["new_posture"], sink=lines.append
                    )
                tail = next((ln for ln in reversed(lines) if ln.strip()), "")
                if ok:
                    msg = f"✓ [{ws_name}] stack reconciled" + (f" · {tail}" if tail else "")
                    severity = "information"
                else:  # the compose run itself failed — do NOT claim success
                    msg = f"✗ [{ws_name}] reconcile FAILED" + (f" · {tail}" if tail else "")
                    severity = "error"
            except Exception as e:  # reconcile is best-effort — never crash the TUI
                msg, severity = f"✗ [{ws_name}] reconcile failed: {e}", "error"
            self.call_from_thread(self.notify, msg, severity=severity)
            self.call_from_thread(self.refresh_workspaces)

        self.run_worker(work, thread=True)

    def _switch_tab(self, delta: int) -> None:
        tabs = self.query_one(TabbedContent)
        panes = self._pane_ids()
        tabs.active = panes[(panes.index(tabs.active) + delta) % len(panes)]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        # `a` (allow a blocked host) and `x` (decline/revoke in the wall pane) only make sense on
        # the Network Log tab — hide them elsewhere so the footer doesn't advertise no-ops.
        # Tolerate the tabs not being mounted yet (startup).
        if action in ("allow_host", "wall_answer"):
            try:
                return self.query_one(TabbedContent).active == "tab-network"
            except Exception:
                return True
        return True

    def action_allow_host(self) -> None:
        """Allow the focused host through the egress wall — from either half of the Network Log
        tab. In the wall pane (below the log) it acts on the selected row: a pending
        recommendation or an existing grant (re-levelling it). In the log tree it keys off the
        cursor node — group nodes carry their host in `.data`, leaf (request) nodes carry none.
        Both push the level picker (once/session/permanent)."""
        if self.query_one(TabbedContent).active != "tab-network":
            return
        sel = self._manage_selection()
        if sel is not None:
            # Its own name, not `host`: the lambda closes over the VARIABLE, and the tree branch
            # below rebinds `host` — never reached after this return, but one refactor away from
            # the picker granting a different row than it named.
            _kind, sel_host = sel
            self.push_screen(
                AllowHostScreen(sel_host), lambda level: self._after_allow(sel_host, level)
            )
            return
        tree = self.query_one("#network-tree", Tree)
        node = tree.cursor_node
        host = node.data if node is not None else None
        if not isinstance(host, str) or not host:
            self.notify(
                "move the cursor onto a host row in the Network Log first, then press a",
                severity="warning",
            )
            return
        self.push_screen(AllowHostScreen(host), lambda level: self._after_allow(host, level))

    def _after_allow(self, host: str, level: str | None) -> None:
        if not level:
            return
        self.notify(f"allowing {host} ({level})…")

        def work() -> None:
            try:
                allowlist.grant(host, level)
                ok, msg = True, f"✓ allowed {host} ({level})"
            except Exception as e:  # grant raises SystemExit (bad host/level/in-box) or OSError
                ok, msg = False, f"✗ allow failed: {e}"
            self.call_from_thread(self.notify, msg, severity=("information" if ok else "error"))
            self.call_from_thread(self.refresh_panels)  # the wall pane reflects the grant now

        self.run_worker(work, thread=True)

    def _manage_selection(self) -> tuple[str, str] | None:
        """The wall pane's selected ``(kind, host)`` — ``kind`` is ``rec`` (a pending
        recommendation) or ``grant`` — but only while that table has focus, so `a` on the log
        tree keeps its original meaning. ``None`` otherwise."""
        pane = self.query("#network-manage")
        if not pane:
            return None
        table = pane.first(DataTable)
        if self.focused is not table or not self._manage_keys:
            return None
        idx = table.cursor_row
        if idx is None or not 0 <= idx < len(self._manage_keys):
            return None
        return self._manage_keys[idx]

    def action_wall_answer(self) -> None:
        """The `a` key's other half, on the wall pane's selected row: decline a recommended host
        ("never offer again" — undone by granting it) or revoke an existing grant."""
        if self.query_one(TabbedContent).active != "tab-network":
            return
        sel = self._manage_selection()
        if sel is None:
            self.notify(
                "focus a row in the wall pane below the log (tab / click), then press x",
                severity="warning",
            )
            return
        kind, host = sel

        def work() -> None:
            try:
                if kind == "rec":
                    allowlist.decline(host)
                    msg = f"✗ {host} declined — not offered again (`fy allow add` re-allows)"
                else:
                    allowlist.revoke(host)
                    msg = f"✓ {host} revoked"
                ok = True
            except Exception as e:  # SystemExit (in-box/bad host) or OSError
                ok, msg = False, f"✗ failed: {e}"
            self.call_from_thread(self.notify, msg, severity=("information" if ok else "error"))
            self.call_from_thread(self.refresh_panels)

        self.run_worker(work, thread=True)

    def action_next_tab(self) -> None:
        self._switch_tab(1)

    def action_prev_tab(self) -> None:
        self._switch_tab(-1)

    def _tab_index(self) -> int:
        return self._pane_ids().index(self.query_one(TabbedContent).active)

    def action_nav_left(self) -> None:
        # ◀ off the first tab steps out to the workspace list; otherwise previous tab.
        if self._tab_index() == 0:
            self.focus_workspaces()
        else:
            self._switch_tab(-1)

    def action_nav_right(self) -> None:
        if self._tab_index() < len(self._pane_ids()) - 1:
            self._switch_tab(1)  # last tab: stay put (no wrap), mirroring the grid edge

    # ── cross-zone keyboard navigation ─────────────────────────────────────────────
    # Three zones: the workspace list (left), the tab strip (no focus — ◀/▶ switch
    # panels), and the focused content of the active panel. The Mode grid and the
    # workspace list bind the arrows that move WITHIN them; these handle the seams.
    def action_enter_content(self) -> None:
        """↓ from the tab strip drops into the active panel's content."""
        active = self.query_one(TabbedContent).active
        if active == "tab-mode":
            self.enter_grid()
        elif active == "tab-doctor":
            self.query_one("#doctor-log-wrap", VerticalScroll).focus()  # scroll the log
        else:  # a plugin panel → focus its widget (so the arrows scroll/expand it)
            suffix = active[len("tab-") :]
            panel = next((p for p in self._panels if p.id == suffix), None)
            if panel is not None and panel.kind == "tree":
                self.query_one(f"#{suffix}-tree", Tree).focus()
            else:
                self.query_one(f"#{suffix}-table", DataTable).focus()

    def enter_grid(self) -> None:
        button = self.query_one("#mode-grid", ModeGrid).first_button()
        if button is not None:
            button.focus()

    def focus_workspaces(self) -> None:
        """← off the left edge of the Mode grid lands on the workspace list."""
        self.query_one("#workspaces", WorkspaceList).focus()

    def leave_grid_top(self) -> None:
        """↑ off the top row releases focus back to the tab strip."""
        self.set_focus(None)

    def on_descendant_focus(self, _event) -> None:
        self.refresh_mode()  # repaint selection colour + selected-mode status line

    def on_descendant_blur(self, _event) -> None:
        # Blur fires before the new focus is set; defer so self.focused is current.
        self.call_after_refresh(self.refresh_mode)

    # (There is intentionally no all-off action: resetting every axis to its default would also
    # drop claude/codex keyless auth out from under a live agent session. De-escalate per axis.)

    # ── host-daemon supervisor (status + log tail; NOT owned by the TUI) ────────────
    # The supervisor (`foldyard host`) is launched by `fy up` and is machine-scoped — quitting
    # the TUI must NOT kill the box's egress proxy, so we don't start/stop it here. We only show
    # whether its daemons are up and tail the log it always writes (supervisor's stdio tee).
    def _render_host_status(self, daemons: dict | None = None, worktree: str | None = None) -> None:
        pane = self.query("#host-status")
        if not pane:
            return
        if daemons is None:
            daemons = devmode.daemon_status(devmode.read()["mode"])
        up = any(d.get("up") for d in daemons.values())
        # Per-workspace: these are the SELECTED worktree's daemons (its own proxy/minter ports). The
        # one supervisor serves every worktree, but a worktree only has daemons up once its box
        # is up + its posture needs them — phrase it per-workspace to match the focused card.
        where = f" for {worktree or 'main'}"
        head = (
            f"host daemons{where}: ● up (the supervisor is serving this workspace)"
            if up
            else f"host daemons{where}: ○ none up — run `fy up` in this workspace; its box routes "
            "through the proxy, so egress fails until it's up (see Doctor)"
        )
        # List each daemon ONCE here (port + which injectors ride it), instead of repeating that
        # line under every axis that shares the daemon — the de-duplicated home for the detail.
        lines = [head] + [
            f"  {d.get('label', name)} :{d.get('port')} " + ("● up" if d.get("up") else "○ DOWN")
            for name, d in sorted(daemons.items())
        ]
        pane.first(Static).update("\n".join(lines))

    def _render_host_log(self) -> None:
        pane = self.query("#host-log")  # tolerate the pane being off-screen (see _render_doctor)
        if not pane:
            return
        # Follow-the-tail, but only while the user is already pinned to the bottom. Capture that
        # BEFORE updating content (the update re-lays-out and moves max_scroll_y): if they've
        # scrolled up to read earlier lines, an unconditional scroll_end on every 1s tick would
        # yank them back down and make scrollback impossible. ``- 1`` absorbs sub-line rounding.
        scroll = (
            self.query("#host-log-wrap").first(VerticalScroll)
            if self.query("#host-log-wrap")
            else None
        )
        follow = scroll is None or scroll.scroll_offset.y >= scroll.max_scroll_y - 1
        log_path = config.supervisor_log_file()
        tail = _tail_text(log_path, max_bytes=32 * 1024, max_lines=300)
        if tail:
            pane.first(Static).update(Text(tail))
        else:
            pane.first(Static).update(
                Text(f"no supervisor log yet at {log_path} — start it with `fy up`", style="dim")
            )
        if scroll is not None and follow:
            scroll.scroll_end(animate=False)  # keep the newest lines in view

    # ── plugin panels ────────────────────────────────────────────────────────────
    def refresh_panels(self) -> None:
        """Repaint each plugin-contributed panel from its current data. The plugin owns the data +
        styling (markup-bearing strings, untrusted text escaped plugin-side); the TUI just renders.
        Rebuild only when the content changed (cheap guard) so a static panel doesn't churn every
        second — and, for trees, so the user's expand/collapse state survives the idle ticks.

        Bound to the SELECTED workspace so each panel (Network Log, GCP Tokens) tails its worktree's
        per-worktree logs (its own egress.jsonl / gcp-minter.jsonl) — matching the card in focus."""
        with config.using(self._worktree_cfg()):
            panel_data = [(panel, panel.refresh()) for panel in self._panels]
            manage = self._manage_data()  # same binding: recommend reads the worktree's config
        self._refresh_manage_panel(*manage)
        for panel, data in panel_data:
            try:
                # The 1s timer can outlive the panel widgets: it may fire after teardown has
                # detached the TabbedContent, or before compose has mounted it. Skip the panel
                # rather than crash the app on a transient NoMatches.
                if panel.kind == "tree":
                    self._refresh_tree_panel(panel, cast(PanelTree, data))
                    continue
                data = cast(PanelData, data)
                table = self.query_one(f"#{panel.id}-table", DataTable)
                self.query_one(f"#{panel.id}-summary", Static).update(data.summary)
            except NoMatches:
                continue
            if table.row_count == len(data.rows):
                continue
            table.clear()
            for row in data.rows:
                table.add_row(*row)

    def _refresh_tree_panel(self, panel: TuiPanel, data: PanelTree) -> None:
        """Render a tree-kind panel's PanelTree as a Textual Tree (groups → their rows). Rebuilds
        only when the (group key, child count) signature changed; across a rebuild the user's
        expanded groups are preserved by key (live counts in the labels would otherwise reset it).
        While unchanged we don't touch the tree at all, so native expand/collapse just works."""
        tree = self.query_one(f"#{panel.id}-tree", Tree)
        self.query_one(f"#{panel.id}-summary", Static).update(data.summary)
        sig = tuple((g.key, len(g.children)) for g in data.groups)
        if self._tree_sigs.get(panel.id) == sig:
            return
        self._tree_sigs[panel.id] = sig
        expanded = {n.data for n in tree.root.children if n.is_expanded}  # keep open groups open
        tree.clear()
        tree.show_root = False
        for g in data.groups:
            node = tree.root.add(Text.from_markup(g.label), data=g.key)
            for child in g.children:
                node.add_leaf(Text.from_markup(child))
            if g.key in expanded:
                node.expand()
        tree.root.expand()

    def _manage_data(self) -> tuple[str, list[tuple[str, str, str]], list[tuple[str, str]]]:
        """``(summary, rows, keys)`` for the wall pane — pending recommendation offers first (the
        rows awaiting an ANSWER: ``[proxy] recommend`` plus the declared plugins' own install
        hosts), then the store's live grants. Computed under the caller's bound worktree config
        (pending reads the ADOPTED recommend list + that config's plugins; the store is
        project-shared). Keys mirror the rows for the cursor→(kind, host) mapping. Best-effort:
        a store hiccup empties the pane for a tick rather than killing the refresh timer."""
        try:
            pending = allowlist.pending_recommendations()
            granted = allowlist.grants()
            enforcing = allowlist.default_deny()
        except Exception:
            return "", [], []
        rows: list[tuple[str, str, str]] = []
        keys: list[tuple[str, str]] = []
        for e in pending:
            rows.append(("rec?", e["host"], e["why"]))
            keys.append(("rec", e["host"]))
        for g in granted:
            exp = g["expires"]
            rows.append((g["level"], g["host"], f"expires {exp[11:19]}" if exp else ""))
            keys.append(("grant", g["host"]))
        wall = "ENFORCING" if enforcing else "observing only"
        summary = (
            f"[b]egress wall[/b] — {wall} · {len(granted)} granted"
            + (f" · [magenta]{len(pending)} recommended pending[/magenta]" if pending else "")
            + "   [dim]a allow · x decline/revoke[/dim]"
        )
        return summary, rows, keys

    def _refresh_manage_panel(
        self,
        summary: str,
        rows: list[tuple[str, str, str]],
        keys: list[tuple[str, str]],
    ) -> None:
        """Repaint the wall pane, rebuilding the table only when its rows changed (the guard that
        keeps the cursor put across idle ticks, like the tree panels' signature).

        BOTH widgets are checked, not just the table. This runs off the 1s ``refresh_panels``
        timer, so it can land while the pane is half-mounted (compose) or half-detached
        (teardown) — and guarding on ``#network-manage`` and then querying ``#network-manage-
        summary`` unguarded put a NoMatches straight out of a timer callback, which Textual
        re-raises from ``run_test`` as the failure of whichever test's app was alive at that
        instant. That made it look like a flake in an unrelated test, on a different test each
        run. The caller invokes this BEFORE the ``except NoMatches: continue`` that protects the
        panel loop, so it has to guard itself."""
        pane = self.query("#network-manage")
        summary_out = self.query("#network-manage-summary")
        if not pane or not summary_out:
            return
        summary_out.first(Static).update(Text.from_markup(summary))
        sig = tuple(rows)
        if sig == self._manage_sig:
            return
        self._manage_sig = sig
        self._manage_keys = keys
        table = pane.first(DataTable)
        table.clear()
        for row in rows:
            table.add_row(*row)

    # ── Doctor tab ───────────────────────────────────────────────────────────────
    # (No key bindings: the doctor runs deep on startup, and re-runs via the tab's own
    # ↻ deep refresh button — handled in on_button_pressed → _run_doctor.)
    def _tick_doctor(self) -> None:
        """Advance the spinner frame while any doctor row is still running."""
        if any(status == "running" for status, _, _ in self._doctor_rows):
            self._spin = (self._spin + 1) % len(SPINNER)
            self._render_doctor()

    def _render_doctor(self) -> None:
        # A background tick (spinner) or a doctor result can land while a modal screen (e.g. the
        # worktree picker) is on top, or during startup/teardown — when #doctor-out isn't in the
        # active screen. query() (vs query_one) returns empty instead of raising; render only when
        # the pane is present, so the timer never crashes the app.
        pane = self.query("#doctor-out")
        if pane:
            pane.first(Static).update(
                _doctor_text(self._doctor_header, self._doctor_rows, self._spin)
            )

    def _set_doctor_rows(self, rows: list[tuple[str, str, str]]) -> None:
        self._doctor_rows = rows
        self._render_doctor()
        self._render_cmd_log()

    def _render_cmd_log(self) -> None:
        pane = self.query("#doctor-log")  # see _render_doctor: tolerate the pane being off-screen
        if not pane:
            return
        # A running/just-run fix's streamed output sits ABOVE the doctor's own background commands
        # (the "show the output in the panel below" the fix buttons promise); cleared on the next
        # user-initiated doctor run (keep_fix_output=False), kept across the post-fix re-check.
        text = Text()
        if self._fix_output is not None:
            text.append_text(self._fix_output)
            text.append("\n")
        text.append_text(_cmd_log_text(devmode.cmd_log()))
        pane.first(Static).update(text)

    # ── Doctor-tab fixes ───────────────────────────────────────────────────────────
    def _update_fix_buttons(self) -> None:
        """Show a fix button iff its check is currently not-ok — ``fail`` OR ``warn`` (the missing
        mitm CA is a warn, not a fail, but it's still one-click fixable). Hide the rest. Called
        when a doctor run finishes. No-op mid-fix — _run_fix hides them and re-checks when done."""
        if self._fixing:
            return
        fixable = {name for status, name, _ in self._doctor_rows if status in ("fail", "warn")}
        for bid, fix in self._fix_buttons.items():
            pane = self.query(f"#{bid}")  # tolerate the pane being off-screen (modal on top)
            if pane:
                pane.first(Button).display = fix.check in fixable

    def _run_fix(self, fix: DoctorFix) -> None:
        if self._fixing:
            return
        self._fixing = True
        self._fix_output = Text()
        self._fix_output.append(f"▶ {fix.label}\n", style="bold")
        self._fix_output.append("$ " + " ".join(fix.cmd) + "\n", style="dim")
        for bid in self._fix_buttons:  # hide all fix buttons while one runs
            pane = self.query(f"#{bid}")
            if pane:
                pane.first(Button).display = False
        self._render_cmd_log()
        self.notify(f"running: {fix.label}…")

        def on_line(line: str) -> None:
            self.call_from_thread(self._fix_line, line)

        def work() -> None:
            rc = devmode.run_stream(fix.cmd, on_line)
            self.call_from_thread(self._finish_fix, fix, rc)

        self.run_worker(work, thread=True, exclusive=True, group="fix")

    def _fix_line(self, line: str) -> None:
        if self._fix_output is not None:
            self._fix_output.append(line + "\n")
            self._render_cmd_log()

    def _finish_fix(self, fix: DoctorFix, rc: int) -> None:
        if self._fix_output is not None:
            ok = rc == 0
            self._fix_output.append(
                f"({'done' if ok else 'failed'} — rc={rc})\n", style="green" if ok else "red"
            )
        self._fixing = False
        self.notify(
            f"{'✓' if rc == 0 else '✗'} {fix.label} (rc={rc})",
            severity="information" if rc == 0 else "error",
        )
        self._run_doctor(deep=True, keep_fix_output=True)  # re-check; keep the fix output visible

    def _run_doctor(self, deep: bool, keep_fix_output: bool = False) -> None:
        if not keep_fix_output:
            self._fix_output = None  # a fresh user-initiated run clears the last fix's output
        self._doctor_header = "deep (live IAM probes)" if deep else "fast checks"
        self._doctor_rows = []
        devmode.cmd_log_reset()  # show only this run's background commands
        self._render_doctor()
        self._render_cmd_log()

        def work() -> None:
            # Render progressively, keyed by name: a 'running' row is shown with a
            # spinner, then replaced in place when its real result arrives.
            order: dict[str, int] = {}
            rows: list[tuple[str, str, str]] = []
            for status, name, detail in devmode.doctor(deep):
                if name in order:
                    rows[order[name]] = (status, name, detail)
                else:
                    order[name] = len(rows)
                    rows.append((status, name, detail))
                self.call_from_thread(self._set_doctor_rows, list(rows))
            self.call_from_thread(self._update_fix_buttons)  # offer repairs for what failed

        self.run_worker(work, thread=True, exclusive=True, group="doctor")


class AxisRow(Vertical):
    """One credential axis: its mode buttons + a live status line. A real container (Vertical), not
    a Static — so its height auto-sizes to title + buttons + status. As a Static it under-measured
    its children and clipped the buttons' bottom row, especially inside the grid layout."""

    def __init__(
        self, axis: str, values: tuple[str, ...] | None = None, emergency: tuple[str, ...] = ()
    ) -> None:
        super().__init__()
        self.axis = axis
        self.values = values
        self.emergency = emergency

    def compose(self) -> ComposeResult:
        yield Static(self.axis.upper(), classes="axis-title")
        with Horizontal():
            values = self.values or devmode.axes()[self.axis]
            emergency = self.emergency or devmode.emergency().get(self.axis, ())
            for value in values:
                label = f"{value} ⏱" if value in emergency else value
                yield Button(label, id=f"{self.axis}-{value}")
        yield Static("", classes="axis-status")


class BranchItem(ListItem):
    """A new-worktree-picker row carrying the branch name it offers (read back via
    `getattr(item, "branch", …)` when a row is chosen)."""

    def __init__(self, branch: str) -> None:
        super().__init__(Static(escape(branch)))
        self.branch = branch


class _LevelItem(ListItem):
    """An allow-level picker row carrying the level it grants (read back via `getattr`)."""

    def __init__(self, level: str, label: str) -> None:
        super().__init__(Static(label))
        self.level = level


class AllowHostScreen(ModalScreen):
    """Pick the level to allow a blocked host through the egress wall: once (~2 min, then auto-
    reverts) / session (until the host supervisor restarts) / permanent (kept in the host-side
    allow-store — never in repo config). ↑/↓ + enter, or the o/s/p shortcuts. Dismisses with the
    level or None."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("o", "pick('once')", "once"),
        Binding("s", "pick('session')", "session"),
        Binding("p", "pick('permanent')", "permanent"),
        Binding("down", "hl_move(1)", "↓", show=False),
        Binding("up", "hl_move(-1)", "↑", show=False),
        Binding("enter", "choose", "allow", show=False),
    ]

    _LEVELS = (
        ("once", "once — ~2 min, then auto-reverts"),
        ("session", "session — until the host supervisor restarts"),
        ("permanent", "permanent — kept in the host allow-store, survives restarts"),
    )

    def __init__(self, host: str) -> None:
        super().__init__()
        self.host = host

    def compose(self) -> ComposeResult:
        with Vertical(id="allow-dialog"):
            yield Static(
                Text.from_markup(f"Allow [b]{escape(self.host)}[/b] through the egress wall"),
                id="allow-title",
            )
            yield ListView(id="allow-list")
            yield Static("o once · s session · p permanent · esc cancel", id="allow-hint")

    def on_mount(self) -> None:
        lv = self.query_one("#allow-list", ListView)
        for level, label in self._LEVELS:
            lv.append(_LevelItem(level, label))
        lv.index = 0
        lv.focus()

    def action_pick(self, level: str) -> None:
        self.dismiss(level)

    def action_hl_move(self, delta: int) -> None:
        lv = self.query_one("#allow-list", ListView)
        n = len(lv.children)
        if n:
            lv.index = 0 if lv.index is None else max(0, min(lv.index + delta, n - 1))

    def action_choose(self) -> None:
        level = getattr(self.query_one("#allow-list", ListView).highlighted_child, "level", None)
        if level:
            self.dismiss(level)

    def on_list_view_selected(self, event) -> None:  # mouse click on a level
        level = getattr(event.item, "level", None)
        if level:
            self.dismiss(level)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _diff_markup(body: str) -> str:
    """A config diff as Rich markup, classified by ``term.diff_style`` so the modal can't color a
    line differently from the terminal's ``fy config diff``. Every line is escaped: a diff carries
    arbitrary TOML, and a `[proxy]` table header is also valid markup."""
    out = []
    for line in body.splitlines():
        style = term.diff_style(line)
        out.append(f"[{style}]{escape(line)}[/{style}]" if style else escape(line))
    return "\n".join(out)


class AdoptConfigScreen(ModalScreen):
    """The adopt gate, asked where the TUI can answer it. Adopting means the host supervisor
    reconciles from THIS checkout's config — the one decision that lets repo-carried
    `[proxy]`/`[[inject]]` reach the host — so it stays an explicit operator choice here rather
    than being waved through because the terminal prompt was inconvenient. Dismisses with
    "adopt", "ignore", or None."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("a", "pick('adopt')", "adopt"),
        Binding("i", "pick('ignore')", "ignore"),
    ]

    def __init__(self, drift, baseline=None) -> None:
        super().__init__()
        self.drift = drift
        # A worktree's first adoption has nothing on its own axis to diff, so the reviewable
        # comparison is against what the host already runs for main — computed by the caller
        # (it reads the host pin), None when there is none. See `configpin.main_baseline`.
        self.baseline = baseline

    def compose(self) -> ComposeResult:
        first = not self.drift.pinned_exists
        what = "has never been adopted" if first else "has changed since it was adopted"
        detail = (
            f"{self.drift.tree_digest()}, {configpin.size(self.drift)}"
            if first
            else self.drift.summary()
        )
        # On a first adoption that's the diff from main, or — with no reviewed baseline — the
        # config itself, commentless; otherwise it's this checkout's own drift. Either way it goes
        # IN the modal rather than behind a "run `fy config diff`" pointer: this is where the
        # decision is taken, and on a first adoption that pointer used to lead to a refusal.
        body = (
            configpin.first_adoption_body(self.drift, self.baseline) if first else self.drift.diff()
        )
        headline = configpin.first_adoption_headline(self.drift, self.baseline) if first else ""
        with Vertical(id="adopt-dialog"):
            yield Static(
                Text.from_markup(
                    f"Config for [b]{escape(self.drift.label)}[/b] {what}",
                ),
                id="adopt-title",
            )
            yield Static(
                Text.from_markup(
                    f"[dim]{escape(detail)}[/dim]"
                    + (f"\n{escape(headline)}" if headline else "")
                    + "\n\nAdopting means the host runs THIS config. Until then the box can't "
                    "start (the host would otherwise reconcile from a config nobody has read)."
                ),
                id="adopt-body",
            )
            if body:
                with VerticalScroll(id="adopt-diff"):
                    yield Static(Text.from_markup(_diff_markup(body)), id="adopt-diff-body")
            yield Static("a adopt · i ignore · esc cancel", id="adopt-hint")

    def on_mount(self) -> None:
        """Focus the diff pane so ↑/↓/PageDown reach past the first screenful.

        ``DevModeTui`` sets ``AUTO_FOCUS = None`` (the arrows drive its tab strip), so a modal
        focuses explicitly or nothing is focused at all — which left the ONE screen whose content
        has to be read in full before it's answered showing only as much of it as fits. A first
        adoption with no baseline renders the whole config, so overflowing is the normal case, not
        the edge one. Nothing to focus when there IS no diff (a checkout declaring nothing, or a
        worktree byte-identical to main's adopted copy); a/i/esc are screen-level bindings and
        answer the modal either way."""
        pane = self.query("#adopt-diff")
        if pane:
            pane.first(VerticalScroll).focus()

    def action_pick(self, answer: str) -> None:
        self.dismiss(answer)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RemoveWorktreeScreen(ModalScreen):
    """Confirm removing a worktree — it's destructive, so it spells out exactly what happens: the
    dev box + stack are torn down (compose down + the worktree's volumes dropped), the Claude
    transcripts are archived, and the checkout is git-removed. Defaults focus to Cancel; y (or the
    Remove button) confirms, esc/n cancels. Dismisses True (remove) / False (keep)."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("n", "cancel", "no", show=False),
        Binding("y", "confirm", "remove"),
    ]

    def __init__(self, ws: dict) -> None:
        super().__init__()
        self.ws = ws

    def compose(self) -> ComposeResult:
        name = escape(self.ws["name"])
        project = escape(self.ws["project"])
        branch = self.ws.get("branch") or ""
        body = (
            "This [b]permanently[/b]:\n"
            f"  • stops + removes its dev box ([dim]{project}-devbox[/dim])\n"
            f"  • tears down its stack ([dim]{project}[/dim]): compose down + drops its volumes\n"
            "    ([dim]postgres + GCS/BigQuery data; shared caches are kept[/dim])\n"
            "  • archives its Claude transcripts to the durable Mac store\n"
            "  • deletes the checkout directory (git worktree remove)"
        )
        if branch:
            body += f"\n\nThe branch [b]{escape(branch)}[/b] is kept."
        with Vertical(id="rm-dialog"):
            yield Static(Text.from_markup(f"Remove worktree [b]{name}[/b]?"), id="rm-title")
            yield Static(Text.from_markup(body), id="rm-body")
            with Horizontal(id="rm-buttons"):
                yield Button("Remove", id="rm-yes", variant="error")
                yield Button("Cancel", id="rm-no")
            yield Static("y remove · n / esc cancel", id="rm-hint")

    def on_mount(self) -> None:
        self.query_one("#rm-no", Button).focus()  # default to the safe choice

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Consume the press: it would otherwise bubble to DevModeTui.on_button_pressed, which
        # parses button ids as mode buttons ("rm-yes" → axis 'rm') and toasts "unknown axis".
        event.stop()
        self.dismiss(event.button.id == "rm-yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ConfirmScreen(ModalScreen):
    """A generic stop-something confirmation (dev box / Podman machine): title + body spell out
    the blast radius, focus defaults to Cancel, y (or the confirm button) confirms, esc/n cancels.
    Dismisses True (do it) / False (leave it). `title`/`body` are Rich markup — escape() any
    interpolated names at the call site."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("n", "cancel", "no", show=False),
        Binding("y", "confirm", "confirm"),
    ]

    def __init__(self, title: str, body: str, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="cf-dialog"):
            yield Static(Text.from_markup(self._title), id="cf-title")
            yield Static(Text.from_markup(self._body), id="cf-body")
            with Horizontal(id="cf-buttons"):
                yield Button(self._confirm_label, id="cf-yes", variant="warning")
                yield Button("Cancel", id="cf-no")
            yield Static("y confirm · n / esc cancel", id="cf-hint")

    def on_mount(self) -> None:
        self.query_one("#cf-no", Button).focus()  # default to the safe choice

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Consume the press: it would otherwise bubble to DevModeTui.on_button_pressed, which
        # parses button ids as mode buttons ("cf-yes" → axis 'cf') — same trap RemoveWorktreeScreen
        # guards against.
        event.stop()
        self.dismiss(event.button.id == "cf-yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class NewWorktreeScreen(ModalScreen):
    """Create a worktree by branch. Type to fuzz-search existing branches; ↑/↓ move
    the highlight (the Input keeps focus the whole time); enter checks out the
    highlighted branch, or — with no match — creates a new branch of the typed name.
    Dismisses with (worktree_name, branch) or None on cancel."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("down", "hl_move(1)", "↓"),
        Binding("up", "hl_move(-1)", "↑"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._branches = devmode.branches()

    def compose(self) -> ComposeResult:
        with Vertical(id="wt-dialog"):
            yield Static("New worktree — pick or type a branch", id="wt-title")
            yield Input(
                placeholder="branch (type to search; a new name creates a branch)", id="wt-input"
            )
            yield ListView(id="wt-list")
            yield Static("", id="wt-hint")

    def on_mount(self) -> None:
        lv = self.query_one("#wt-list", ListView)
        lv.can_focus = False  # the Input keeps focus; we drive the highlight ourselves
        self._refilter("")
        self.query_one("#wt-input", Input).focus()

    # match: prefix hits first, then other substring hits, both in branch order
    def _matches(self, text: str) -> list[str]:
        t = text.strip().lower()
        if not t:
            return self._branches
        pre = [b for b in self._branches if b.lower().startswith(t)]
        sub = [b for b in self._branches if t in b.lower() and not b.lower().startswith(t)]
        return pre + sub

    def _refilter(self, text: str) -> None:
        lv = self.query_one("#wt-list", ListView)
        lv.clear()
        matches = self._matches(text)
        for branch in matches:
            lv.append(BranchItem(branch))
        lv.index = 0 if matches else None
        typed = text.strip()
        if not typed:
            hint = f"{len(self._branches)} branches — type to filter, ↑/↓ to choose"
        elif matches:
            hint = f"{len(matches)} match — enter checks out the highlighted branch"
        else:
            hint = f"no match — enter creates new branch [b]{escape(typed)}[/b]"
        self.query_one("#wt-hint", Static).update(hint)

    @staticmethod
    def _wt_name(branch: str) -> str:
        return branch.strip().strip("/").replace("/", "-")

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refilter(event.value)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._choose()

    def on_list_view_selected(self, event) -> None:  # mouse click on a branch
        branch = getattr(event.item, "branch", None)
        if branch:
            self.dismiss((self._wt_name(branch), branch))

    def action_hl_move(self, delta: int) -> None:
        lv = self.query_one("#wt-list", ListView)
        n = len(lv.children)
        if not n:
            return
        lv.index = 0 if lv.index is None else max(0, min(lv.index + delta, n - 1))

    def _choose(self) -> None:
        text = self.query_one("#wt-input", Input).value.strip()
        lv = self.query_one("#wt-list", ListView)
        highlighted = lv.highlighted_child
        branch = getattr(highlighted, "branch", None) or (self._matches(text)[:1] or [None])[0]
        if not branch:  # no existing match → create a branch named exactly as typed
            branch = text
        if branch:
            self.dismiss((self._wt_name(branch), branch))

    def action_cancel(self) -> None:
        self.dismiss(None)


def main() -> int:
    """`foldyard tui` entry point."""
    if devmode.in_box():
        raise SystemExit("✗ the TUI runs ON THE MAC (mode changes are Mac-only).")
    # Detect the terminal's light/dark theme BEFORE Textual grabs the terminal, then let the app
    # adopt it on mount (so the TUI follows the system theme like cmux does).
    DevModeTui(start_theme=_detect_terminal_theme()).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
