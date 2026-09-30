"""
The side panel *is* Microsoft Excel, docked into the app window.

bny_sidepanel.xlsx is opened in a real Excel instance whose window is
stripped of its chrome, made an owned window of the main window and
pinned over the panel area, so the architect edits the actual sheet —
full Excel: formulas, merged cells, the logo, copy/paste — with no second
window on the taskbar and nothing to alt-tab between.  Nothing here is a
re-implementation of Excel's grid.

Excel's own chrome (title bar, ribbon, formula bar, status bar, row and
column headings, sheet tabs) is switched off so what is left looks like
the printed panel.

Clicking Done exports the live sheet with ExportAsFixedFormat and emits
``generate_requested``; app.core.panel_compositor then attaches that PDF
to the right of the cropped drawing to make the full-size sheet.

Windows-only: needs pywin32 and a local Microsoft Excel.  If either is
missing the panel degrades to an explanatory message and PDF generation
falls back to the in-process renderer in app.tools.render_side_panel.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
import time
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QDate, QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCalendarWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.utils.paths import resource_path

# ---------------------------------------------------------------------------
# Optional Windows dependencies
# ---------------------------------------------------------------------------
try:
    import pythoncom
    import win32api
    import win32com.client
    import win32con
    import win32gui
    import win32process

    WIN32_AVAILABLE = True
    WIN32_IMPORT_ERROR = ""
except Exception as _exc:  # pragma: no cover - platform dependent
    pythoncom = win32api = win32com = win32con = win32gui = win32process = None
    WIN32_AVAILABLE = False
    WIN32_IMPORT_ERROR = str(_exc)


TEMPLATES = resource_path(os.path.join("app", "resources", "templates"))
EXCEL_PATH = os.path.join(TEMPLATES, "bny_sidepanel.xlsx")
CROPPED_BASE = os.path.join(TEMPLATES, "bny_standard_a1_cropped.pdf")
FULL_BASE = os.path.join(TEMPLATES, "bny_standard_a1.pdf")

# The block of the sheet that is the panel.  Used for zoom-to-width and as
# the print area on export.
SHEET_RANGE = "A1:O38"

# Cells the rest of the app reads back.  METADATA_COORDS feed
# ProjectMetadata; ALL_DATA_COORDS is everything worth round-tripping.
METADATA_COORDS = [
    "B27",   # client_name
    "B25",   # project_title
    "B31",   # sheet_title
    "G35",   # job_no
    "G36",   # drawing_no
    "G33",   # scale
    "G32",   # paper_size
    "G34",   # rev_no
]

ALL_DATA_COORDS = [
    # Key plan and notes blocks.
    "B7", "B9",
    # Reference drawings table - row 10 is its banner, row 16 its header.
    "B11", "D11", "I11",
    "B12", "D12", "I12",
    "B13", "D13", "I13",
    "B14", "D14", "I14",
    "B15", "D15", "I15",
    # Revision history - row 17 is its banner, row 23 its header.
    "B18", "D18", "F18", "K18", "N18",
    "B19", "D19", "F19", "K19", "N19",
    "B20", "D20", "F20", "K20", "N20",
    "B21", "D21", "F21", "K21", "N21",
    "B22", "D22", "F22", "K22", "N22",
    # Title block: the field under each label.
    "B25", "B27", "B29", "B31",
    "G32", "G33", "G34", "G35", "G36",
    # Released-for row; the tick is part of each cell's text.
    "C38", "E38", "H38", "J38", "M38",
]

# The DATE column of the revision-history rows.  Clicking one raises a
# calendar so the date can be picked rather than typed.
DATE_COORDS = ("D18", "D19", "D20", "D21", "D22")

# How a picked date is written, matching the firm's sheets ("13-03-2026").
DATE_FORMAT = "dd-MM-yyyy"

# Excel COM constants we use by value.
_XL_NORMAL_WINDOW = -4143      # xlNormal
_XL_TYPE_PDF = 0               # xlTypePDF
_XL_QUALITY_STANDARD = 0       # xlQualityStandard
_XL_PORTRAIT = 1               # xlPortrait
_XL_PAPER_A3 = 8               # xlPaperA3

# COM errors that mean "Excel is busy right now, ask again" — typically the
# user is mid-edit in a cell, which blocks the automation interface.
_RETRY_HRESULTS = frozenset({
    -2147418111,   # RPC_E_CALL_REJECTED
    -2147417846,   # RPC_E_SERVERCALL_RETRYLATER
    -2147417851,   # RPC_E_SERVERFAULT
})


def _com_retry(fn, tries: int = 10, delay: float = 0.12):
    """Call *fn*, retrying while Excel says it is busy.

    Excel refuses automation calls outright while a cell is in edit mode.
    That is the normal state of a panel someone is typing into, so every
    read and write goes through here.
    """
    last: Optional[Exception] = None
    for _ in range(max(1, tries)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below
            hresult = exc.args[0] if exc.args else None
            if isinstance(hresult, int) and hresult in _RETRY_HRESULTS:
                last = exc
                time.sleep(delay)
                continue
            raise
    raise last if last else RuntimeError("Excel stayed busy")


# GWLP_HWNDPARENT sets a window's *owner*, not its parent — the one
# relationship that ties two top-level windows together without breaking
# either one's activation.  pywin32 has no wrapper for the 64-bit form.
_GWLP_HWNDPARENT = -8


def _set_window_owner(hwnd: int, owner_hwnd: int) -> None:
    user32 = ctypes.windll.user32
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        user32.SetWindowLongPtrW(hwnd, _GWLP_HWNDPARENT,
                                 ctypes.c_void_p(owner_hwnd))
    else:
        user32.SetWindowLongW(hwnd, _GWLP_HWNDPARENT, owner_hwnd)


class _GUIThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("hwndActive", ctypes.c_void_p),
        ("hwndFocus", ctypes.c_void_p),
        ("hwndCapture", ctypes.c_void_p),
        ("hwndMenuOwner", ctypes.c_void_p),
        ("hwndMoveSize", ctypes.c_void_p),
        ("hwndCaret", ctypes.c_void_p),
        ("rcCaret", ctypes.c_long * 4),
    ]


def _excel_focus_window(excel_hwnd: int) -> Optional[int]:
    """The focused window inside Excel's own UI thread.

    GetFocus() only ever answers for the calling thread, and Excel runs in
    another process — GetGUIThreadInfo is the way to ask about someone
    else's.  pywin32 does not wrap it.
    """
    try:
        thread_id, _ = win32process.GetWindowThreadProcessId(excel_hwnd)
        info = _GUIThreadInfo()
        info.cbSize = ctypes.sizeof(_GUIThreadInfo)
        if not ctypes.windll.user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return None
        return int(info.hwndFocus) if info.hwndFocus else None
    except Exception:
        return None


# ExportAsFixedFormat goes through the active printer's driver for page
# metrics.  If that printer is offline — an unplugged USB printer, say —
# the export crawls or never returns.  Microsoft Print to PDF ships with
# Windows 10/11 and always answers immediately.  Excel wants
# "<name> on <port>" with its own legacy port names, so the port has to be
# found by trying them.
_PDF_PRINTER_NAME = "Microsoft Print to PDF"
_PDF_PRINTER_PORTS = [f"Ne{i:02d}:" for i in range(16)] + ["PORTPROMPT:"]


def _split_cell(cell: str) -> Tuple[str, int]:
    column = "".join(ch for ch in cell if ch.isalpha()).upper()
    row = "".join(ch for ch in cell if ch.isdigit())
    return column, int(row or 0)


def _column_index(column: str) -> int:
    """"A" -> 1, "Z" -> 26, "AA" -> 27 — for indexing into a read block."""
    index = 0
    for ch in column:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index


def _absolute_range(a1_range: str) -> str:
    """"A1:O38" -> "$A$1:$O$38" — the form PageSetup.PrintArea expects."""
    def absolute(cell: str) -> str:
        column, row = _split_cell(cell)
        return f"${column}${row}"

    start, _, end = a1_range.partition(":")
    return f"{absolute(start)}:{absolute(end)}" if end else absolute(start)


def _range_end(a1_range: str) -> Tuple[str, int]:
    """The bottom-right corner of an A1-style range, as (column, row)."""
    _, _, end = a1_range.partition(":")
    return _split_cell(end or a1_range)


def _next_column(column: str) -> str:
    """The column letter after this one: "O" -> "P", "Z" -> "AA"."""
    index = 0
    for ch in column:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    index += 1
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


# The far edge of an Excel worksheet.
_LAST_COLUMN = "XFD"
_LAST_ROW = 1048576


# Everything that can move the panel, hide it, or put something in front
# of it.  WindowBlocked/WindowUnblocked bracket a modal dialog.
_WINDOW_EVENTS = frozenset({
    QEvent.Type.Move,
    QEvent.Type.Resize,
    QEvent.Type.Show,
    QEvent.Type.Hide,
    QEvent.Type.WindowStateChange,
    QEvent.Type.WindowBlocked,
    QEvent.Type.WindowUnblocked,
    QEvent.Type.WindowActivate,
    QEvent.Type.WindowDeactivate,
})


class ExcelStyleSidePanel(QWidget):
    """Live Microsoft Excel, embedded as the app's right-hand side panel.

    Signals
    -------
    data_changed
        The sheet's tracked cells changed.
    generate_requested
        Done was clicked and the panel has been exported.  ``panel_pdf``
        holds the path to that export.
    """

    data_changed = Signal()
    generate_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("excelSidePanel")
        self.setMinimumWidth(400)
        self.setMaximumWidth(600)

        self._excel = None
        self._workbook = None
        self._sheet = None
        self._window = None          # Excel Window object
        self._hwnd: Optional[int] = None
        self._pid: Optional[int] = None
        self._sheet_width_pts: Optional[float] = None
        self._zoom_override: Optional[int] = None
        self._excel_ready = False
        self._starting = False
        self._last_values: Dict[str, str] = {}
        # The size each cell is meant to be, so a value that gets shorter
        # goes back up again instead of staying shrunk.
        self._base_font: Dict[str, Tuple[float, bool]] = {}
        self._base_wrap: Dict[str, bool] = {}
        self._fitted_sizes: Dict[str, float] = {}
        self._poll_timer: Optional[QTimer] = None
        self._calendar: Optional[QCalendarWidget] = None
        self._calendar_coord: Optional[str] = None
        self._last_active_cell: Optional[str] = None
        self._track_timer: Optional[QTimer] = None
        self._watched_window: Optional[QWidget] = None
        self._placed_rect: Optional[Tuple[int, int, int, int]] = None
        self._panel_visible = True
        self._shown = False
        self._panel_pdf: Optional[str] = None
        self._shut_down = False
        self._temp_dir = tempfile.mkdtemp(prefix="bny_panel_")
        self._working_path = os.path.join(self._temp_dir, "bny_sidepanel.xlsx")

        self._excel_path = EXCEL_PATH
        self.cropped_base = CROPPED_BASE
        self.full_base = FULL_BASE

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame()
        header.setObjectName("excelPanelHeader")
        bar = QHBoxLayout(header)
        bar.setContentsMargins(10, 8, 10, 8)
        bar.setSpacing(6)

        title = QLabel("Sheet panel")
        title.setObjectName("panelTitle")
        bar.addWidget(title)
        bar.addStretch(1)

        self._zoom_out_btn = self._tool_button("−", "Zoom out", self._zoom_out)
        self._zoom_in_btn = self._tool_button("+", "Zoom in", self._zoom_in)
        self._fit_btn = self._tool_button("Fit", "Fit the panel to the width of this column",
                                          self._fit_width)
        for button in (self._zoom_out_btn, self._zoom_in_btn, self._fit_btn):
            bar.addWidget(button)

        self._done_btn = QPushButton("Done")
        self._done_btn.setObjectName("generateButton")
        self._done_btn.setToolTip(
            "Finish editing, turn this sheet into a PDF and attach it to the "
            "right of the drawing"
        )
        self._done_btn.clicked.connect(self._on_done_clicked)
        bar.addWidget(self._done_btn)

        outer.addWidget(header)

        # Excel's window is pinned over this widget's area (see
        # _attach_window); the widget itself only reserves the space.
        self._host = QWidget()
        self._host.setObjectName("excelHost")
        self._host.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Expanding)
        self._host.resizeEvent = self._host_resize_event  # type: ignore[method-assign]
        self._host.moveEvent = self._host_move_event  # type: ignore[method-assign]
        outer.addWidget(self._host, 1)

        self._status = QLabel("")
        self._status.setObjectName("helpText")
        self._status.setWordWrap(True)
        self._status.setContentsMargins(12, 10, 12, 10)
        self._status.hide()
        outer.addWidget(self._status)

    def _tool_button(self, text: str, tip: str, slot) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tip)
        button.clicked.connect(slot)
        return button

    def _host_resize_event(self, event) -> None:
        QWidget.resizeEvent(self._host, event)
        self._track_window()

    def _host_move_event(self, event) -> None:
        QWidget.moveEvent(self._host, event)
        self._track_window()

    def _show_status(self, message: str) -> None:
        self._status.setText(message)
        self._status.show()
        for button in (self._zoom_in_btn, self._zoom_out_btn, self._fit_btn):
            button.setEnabled(False)

    def _clear_status(self) -> None:
        """Put the panel back to normal once Excel is actually up."""
        self._status.hide()
        for button in (self._zoom_in_btn, self._zoom_out_btn, self._fit_btn):
            button.setEnabled(True)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        if not self._excel_ready and not self._starting:
            # Let the host widget get its real geometry first, otherwise
            # Excel is sized against a 640x480 placeholder.
            #
            # Starting Excel takes three or four seconds and holds the UI
            # thread for all of it - COM is apartment-bound, so it cannot
            # go to a worker.  A 0ms timer fires before the first paint,
            # which meant the app opened to a blank rectangle and looked
            # hung on the way in.  A short delay lets the window draw
            # itself first, so the wait happens against a visible app.
            self._show_status("Opening the sheet panel…")
            QTimer.singleShot(120, self._start_excel)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Stop polling, release the embedded window and quit Excel."""
        self._stop_excel()
        self._shut_down = True

    def __del__(self):  # pragma: no cover - interpreter teardown
        """Last resort only, and deliberately does almost nothing.

        This used to call the full _stop_excel.  By the time __del__ runs
        the C++ side of this widget and its timers may already be gone,
        and calling stop() on a deleted QTimer is an access violation —
        a hard crash on the way out, after the window had closed and with
        nothing to show for it. The orderly path is shutdown(), which
        closeEvent calls; all this has to do is make sure an Excel that
        somehow survived that does not outlive the process.
        """
        if getattr(self, "_shut_down", False):
            return
        try:
            self._terminate_excel_process()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Excel startup
    # ------------------------------------------------------------------

    def _start_excel(self) -> None:
        if self._excel_ready or self._starting:
            return
        self._starting = True
        try:
            self._launch()
        finally:
            self._starting = False

    def _launch(self) -> None:
        if not WIN32_AVAILABLE:
            self._show_status(
                "The side panel needs Microsoft Excel and pywin32, which "
                f"are not available here ({WIN32_IMPORT_ERROR}).\n\n"
                "You can still generate the sheet — the panel will be drawn "
                "from the template instead of the live sheet."
            )
            return

        if not os.path.isfile(self._excel_path):
            self._show_status(f"Side panel workbook not found:\n{self._excel_path}")
            self._log("error", f"Side panel workbook missing: {self._excel_path}")
            return

        try:
            pythoncom.CoInitialize()
        except Exception:
            pass

        try:
            shutil.copy2(self._excel_path, self._working_path)
        except Exception as exc:
            self._show_status(f"Could not prepare the panel workbook:\n{exc}")
            self._log("error", f"Could not copy side panel workbook: {exc}")
            return

        try:
            self._excel = win32com.client.DispatchEx("Excel.Application")
        except Exception as exc:
            self._show_status(
                "Microsoft Excel could not be started, so the side panel "
                f"cannot be embedded:\n{exc}"
            )
            self._log("error", f"Could not start Excel: {exc}")
            return

        try:
            # Excel must be Visible for its window to exist at all — and
            # ExportAsFixedFormat silently hangs on an invisible instance.
            self._excel.Visible = True
            self._excel.DisplayAlerts = False
            self._excel.AskToUpdateLinks = False

            self._workbook = self._excel.Workbooks.Open(
                self._working_path, ReadOnly=False, UpdateLinks=0,
            )
            self._sheet = self._workbook.ActiveSheet
            self._window = self._workbook.Windows(1)
            self._hwnd = int(self._window.Hwnd)
            _, self._pid = win32process.GetWindowThreadProcessId(self._hwnd)

            try:
                self._sheet_width_pts = float(self._sheet.Range(SHEET_RANGE).Width)
            except Exception:
                self._sheet_width_pts = None

            self._strip_excel_chrome()
            self._attach_window()
            self._excel_ready = True
            self._fit_excel_window()
            self._clear_status()
            self._log("success", "Side panel opened in Excel inside the app")
        except Exception as exc:
            self._show_status(f"Could not open the panel sheet:\n{exc}")
            self._log("error", f"Could not open the panel sheet: {exc}")
            self._stop_excel()

    def _strip_excel_chrome(self) -> None:
        """Turn off everything that would give away a separate Excel app."""
        app_settings = (
            ("WindowState", _XL_NORMAL_WINDOW),
            ("DisplayStatusBar", False),
            ("DisplayFormulaBar", False),
        )
        for name, value in app_settings:
            try:
                setattr(self._excel, name, value)
            except Exception as exc:
                self._log("info", f"Excel chrome ({name}): {exc}")

        try:
            # The ribbon has no COM property; this XLM macro is the only
            # supported way to collapse it away entirely.
            self._excel.ExecuteExcel4Macro('SHOW.TOOLBAR("Ribbon",False)')
        except Exception as exc:
            self._log("info", f"Could not hide the Excel ribbon: {exc}")

        window_settings = (
            ("DisplayHeadings", False),
            ("DisplayWorkbookTabs", False),
            ("DisplayGridlines", False),
            ("DisplayHorizontalScrollBar", False),
        )
        for name, value in window_settings:
            try:
                setattr(self._window, name, value)
            except Exception as exc:
                self._log("info", f"Excel window chrome ({name}): {exc}")

        self._trim_sheet_to_panel()
        self._prepare_cells_for_fitting()
        self._start_date_watch()

    # ── Date picker ───────────────────────────────────────────────────

    def _start_date_watch(self) -> None:
        """Poll which cell is selected so the calendar can follow it.

        Excel raises SelectionChange through a COM event sink, which means
        another apartment-threaded object to keep alive next to the window
        tracking already here; asking which cell is active is one cheap
        call, so it is polled instead.
        """
        if self._poll_timer is not None:
            return
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(300)
        self._poll_timer.timeout.connect(self._check_active_cell)
        self._poll_timer.start()

    def _check_active_cell(self) -> None:
        """Raise or dismiss the calendar as the selection moves."""
        if not self._excel_ready or not self._sheet:
            self._hide_calendar()
            return
        # Ask the window, not the foreground bookkeeping: _shown is false
        # whenever another app is in front, which is most of the time from
        # the panel's point of view and has nothing to do with whether a
        # date cell is selected.
        try:
            if not win32gui.IsWindowVisible(self._hwnd):
                self._hide_calendar()
                return
        except Exception:
            pass
        try:
            # Address is a property under late binding, not a callable, and
            # it comes back absolute ("$D$17").
            #
            # One attempt, no waiting.  The default ten tries at 0.12s is
            # 1.2 seconds of a blocked UI thread, and a cell being typed
            # into rejects every call - which is precisely when this timer
            # is running.  Firing every 300ms, that left the window frozen
            # for as long as anyone was entering data, which is most of
            # what the side panel is for.  A missed tick costs nothing:
            # there is another one along in 300ms.
            raw = _com_retry(
                lambda: self._sheet.Application.ActiveCell.Address,
                tries=1, delay=0.0,
            )
            address = str(raw).replace("$", "")
        except Exception:
            return

        if address == self._last_active_cell:
            return
        self._last_active_cell = address

        if address in DATE_COORDS:
            self._show_calendar(address)
        else:
            self._hide_calendar()

    def _build_calendar(self) -> QCalendarWidget:
        calendar = QCalendarWidget(None)
        calendar.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        # Excel keeps the keyboard: the date can still be typed, and the
        # calendar is there for whoever would rather point at it.
        calendar.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        calendar.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        calendar.setGridVisible(True)
        calendar.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
        )
        calendar.clicked.connect(self._on_date_picked)
        return calendar

    def _cell_screen_position(self, coord: str):
        """Top-left of a cell in screen pixels, or None.

        Worked out from the window rectangle and the visible range rather
        than with PointsToScreenPixels: in this embedded window that call
        returns one pixel per point whatever the zoom, which put the
        calendar hundreds of pixels above the cell.  The visible range
        gives the scale directly - its extent in points spans the client
        area in pixels - and that holds at any zoom or DPI setting.
        """
        try:
            window = self._excel.ActiveWindow
            visible = window.VisibleRange
            cell = self._sheet.Range(coord).MergeArea

            client_w, client_h = win32gui.GetClientRect(self._hwnd)[2:]
            origin_x, origin_y = win32gui.ClientToScreen(self._hwnd, (0, 0))

            span_w = float(visible.Width)
            span_h = float(visible.Height)
            if span_w <= 0 or span_h <= 0 or client_w <= 0 or client_h <= 0:
                return None

            per_point_x = client_w / span_w
            per_point_y = client_h / span_h

            x = int(origin_x
                    + (float(cell.Left) - float(visible.Left)) * per_point_x)
            y = int(origin_y
                    + (float(cell.Top) - float(visible.Top)) * per_point_y)
            height = max(1, int(float(cell.Height) * per_point_y))
            return x, y, height
        except Exception:
            return None

    def _show_calendar(self, coord: str) -> None:
        if self._calendar is None:
            self._calendar = self._build_calendar()

        placed = self._cell_screen_position(coord)
        if placed is None:
            return
        x, y, cell_height = placed

        self._calendar_coord = coord
        # Start on whatever the cell already holds, so re-picking a date
        # opens on that month rather than today.
        current = QDate.currentDate()
        try:
            existing = str(_com_retry(
                lambda: self._sheet.Range(coord).Text) or "").strip()
            if existing:
                parsed = QDate.fromString(existing, DATE_FORMAT)
                if parsed.isValid():
                    current = parsed
        except Exception:
            pass
        self._calendar.setSelectedDate(current)

        self._calendar.adjustSize()
        self._calendar.move(*self._popup_position(self._calendar, x, y, cell_height))
        self._calendar.show()
        self._calendar.raise_()

    @staticmethod
    def _popup_position(popup, x: int, y: int, anchor_height: int) -> tuple:
        """Where a popup anchored at a cell can actually sit on screen.

        The calendar is a frameless top-level window, so nothing clips it
        to the app: moved past the edge of the display it is simply cut
        off by the desktop, which is how the date picker ended up half
        off the right-hand side.  The DATE column sits near the right of
        the side panel, and the side panel is the right-hand edge of the
        window, so on any screen the app fills this was the normal case
        rather than an edge case.

        The old code checked the bottom edge and nothing else - no left,
        no right, and it clamped the top to zero, which is wrong the
        moment a second monitor puts the desktop origin somewhere other
        than the top-left of this screen.
        """
        width = popup.width()
        height = popup.height()

        # The screen the cell is on, not whichever one the popup was last
        # shown on: on a two-monitor desk those are routinely different,
        # and clamping to the wrong one moves it somewhere worse.
        screen = QGuiApplication.screenAt(QPoint(x, y))
        if screen is None:
            screen = popup.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return (x, y + anchor_height)

        area = screen.availableGeometry()

        # Below the cell by preference; above it if there is no room
        # below but there is above.  Otherwise leave it below and let the
        # clamp bring it back on screen.
        top = y + anchor_height
        if top + height > area.bottom() and y - height >= area.top():
            top = y - height

        left = min(max(x, area.left()), area.right() - width + 1)
        top = min(max(top, area.top()), area.bottom() - height + 1)
        return (int(left), int(top))

    def _hide_calendar(self) -> None:
        if self._calendar is not None and self._calendar.isVisible():
            self._calendar.hide()
        self._calendar_coord = None

    def _on_date_picked(self, date: QDate) -> None:
        """Write the chosen date into the cell it was raised for."""
        coord = self._calendar_coord
        if not coord or not self._excel_ready or not self._sheet:
            return
        text = date.toString(DATE_FORMAT)
        try:
            _com_retry(lambda: self._write_cell(self._sheet, coord, text))
        except Exception as exc:
            self._log("info", f"Could not write the date into {coord}: {exc}")
            return
        self._last_values[coord] = text
        self.fit_cell_fonts([coord])
        self._hide_calendar()
        self.data_changed.emit()

    def _prepare_cells_for_fitting(self) -> None:
        """Put each tracked cell's wrapping back to what the template says.

        Every tracked cell is merged across columns and Excel ignores
        ShrinkToFit on a merged cell, so the size is worked out in
        _fit_cell_font instead.  That used to switch wrapping off to
        measure on one line, which collapsed the paragraphs that are
        meant to wrap; the fit is height-aware now, so wrapping stays.
        """
        self._load_base_fonts()
        for coord, wraps in self._base_wrap.items():
            try:
                self._sheet.Range(coord).MergeArea.WrapText = bool(wraps)
            except Exception as exc:
                self._log("info", f"Could not prepare {coord} for fitting: {exc}")
                return

    def _trim_sheet_to_panel(self) -> None:
        """Show the panel block and nothing else — no empty grid around it.

        Everything past the block is hidden rather than just scrolled out
        of the way, and ScrollArea pins the sheet to the block, so the
        panel ends at the border of the box exactly as it prints.
        """
        last_column, last_row = _range_end(SHEET_RANGE)
        try:
            self._sheet.Range(
                f"{_next_column(last_column)}:{_LAST_COLUMN}"
            ).EntireColumn.Hidden = True
        except Exception as exc:
            self._log("info", f"Could not hide the columns past the panel: {exc}")
        try:
            self._sheet.Range(
                f"{last_row + 1}:{_LAST_ROW}"
            ).EntireRow.Hidden = True
        except Exception as exc:
            self._log("info", f"Could not hide the rows past the panel: {exc}")
        try:
            self._sheet.ScrollArea = _absolute_range(SHEET_RANGE)
        except Exception as exc:
            self._log("info", f"Could not pin the scroll area: {exc}")

    def _attach_window(self) -> None:
        """Dock Excel's window onto the panel: no chrome, owned by the app.

        Excel's window stays a top-level window and is only *owned* by the
        main window — it is not reparented into it.  That distinction
        matters: a SetParent'd Excel never receives activation, so
        Application.ActiveSheet stays None, Range.Select fails and nothing
        typed on the keyboard reaches a cell.  Owning it instead leaves
        Excel fully functional while Windows ties it to the app — it sits
        above the main window, gets no taskbar button and no Alt-Tab entry,
        and minimises, restores and closes with it.  _track_window then
        keeps it pinned over the host widget.
        """
        hwnd = self._hwnd
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        # WS_SYSMENU stays: without it — and with no caption either —
        # Windows stops activating the window when it is clicked, and an
        # Excel that never becomes the foreground window swallows every
        # keystroke.  It draws nothing once WS_CAPTION is gone.
        style &= ~(
            win32con.WS_CAPTION | win32con.WS_THICKFRAME
            | win32con.WS_MINIMIZEBOX | win32con.WS_MAXIMIZEBOX
        )
        win32gui.SetWindowLong(hwnd, win32con.GWL_STYLE, style)

        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        ex_style &= ~(
            win32con.WS_EX_APPWINDOW | win32con.WS_EX_WINDOWEDGE
            | win32con.WS_EX_CLIENTEDGE | win32con.WS_EX_DLGMODALFRAME
            | win32con.WS_EX_STATICEDGE
        )
        ex_style |= win32con.WS_EX_TOOLWINDOW  # keeps it out of the taskbar
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex_style)

        top_level = self.window()
        _set_window_owner(hwnd, int(top_level.winId()))

        # Follow the panel by watching the window rather than polling it:
        # this app's event loop starves short repeating timers (the canvas
        # keeps it busy), so a timer alone lags a window drag badly.
        top_level.installEventFilter(self)
        self._watched_window = top_level

        # Backstop for anything the events miss — a layout change that
        # moves the panel without resizing the window, say.
        self._track_timer = QTimer(self)
        self._track_timer.setInterval(400)
        self._track_timer.timeout.connect(self._track_window)
        self._track_timer.start()

    def eventFilter(self, watched, event):  # noqa: N802 - Qt naming
        if watched is getattr(self, "_watched_window", None):
            kind = event.type()
            if kind in _WINDOW_EVENTS:
                if kind in (QEvent.Type.WindowBlocked, QEvent.Type.WindowUnblocked):
                    # A modal dialog is up.  Excel is an owned top-level
                    # window and would otherwise float over it.
                    self._panel_visible = kind == QEvent.Type.WindowUnblocked
                self._track_window()
                if kind in (QEvent.Type.WindowActivate,
                            QEvent.Type.WindowDeactivate):
                    # Qt says the window changed activation before Windows
                    # has settled on the new foreground; look again once it
                    # has, so _foreground_is_ours sees the truth.
                    QTimer.singleShot(150, self._track_window)
        return super().eventFilter(watched, event)

    def _foreground_is_ours(self) -> bool:
        """True while the app — or Excel itself — is the app in front.

        Excel's window is owned by the main window, which keeps it above
        the app but not above everybody else's windows.  Without this it
        would sit on top of whatever the architect switched to.
        """
        try:
            foreground = win32gui.GetForegroundWindow()
            if not foreground:
                return False
            ours = {int(self.window().winId()), self._hwnd}
            if foreground in ours:
                return True
            # Dialogs — ours or Excel's own — are owned by one of those.
            owner = win32gui.GetWindow(foreground, win32con.GW_OWNER)
            return owner in ours
        except Exception:
            return True  # never hide the panel over a bookkeeping error

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def _host_global_rect(self) -> Optional[tuple]:
        """The host widget's area in screen pixels, or None if not shown."""
        if not self._host.isVisible():
            return None
        top_left = self._host.mapToGlobal(self._host.rect().topLeft())
        ratio = self._host.devicePixelRatioF()
        return (
            int(round(top_left.x() * ratio)),
            int(round(top_left.y() * ratio)),
            max(1, int(round(self._host.width() * ratio))),
            max(1, int(round(self._host.height() * ratio))),
        )

    def _track_window(self) -> None:
        """Keep Excel exactly over the host widget, and hidden with the app.

        One poll covers every way the panel can move — the window being
        dragged, resized or maximised, a dock opening, the app being
        minimised — without hooking each of them separately.
        """
        if not self._excel_ready or not self._hwnd:
            return

        owner = self.window()
        should_show = (
            self._panel_visible
            and owner.isVisible()
            and not owner.isMinimized()
            and self._host.isVisible()
            and self._foreground_is_ours()
        )
        if not should_show:
            if self._shown:
                self._shown = False
                try:
                    win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
                except Exception:
                    pass
            return

        rect = self._host_global_rect()
        if rect is None:
            return
        if not self._shown:
            self._shown = True
            self._placed_rect = None
            try:
                win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
            except Exception:
                pass

        if rect == self._placed_rect:
            return
        resized = self._placed_rect is None or rect[2:] != self._placed_rect[2:]
        self._placed_rect = rect
        try:
            win32gui.SetWindowPos(
                self._hwnd, 0, rect[0], rect[1], rect[2], rect[3],
                win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
                | win32con.SWP_SHOWWINDOW,
            )
        except Exception as exc:
            self._log("info", f"Could not place the panel window: {exc}")
            return
        if resized:
            # Dragging the window around must not reach for COM — see the
            # note above _read_values.
            self._apply_zoom()

    def _fit_excel_window(self) -> None:
        """Re-place Excel now rather than waiting for the next poll."""
        self._placed_rect = None
        self._track_window()

    def set_panel_visible(self, visible: bool) -> None:
        """Show or hide the Excel window without shutting Excel down.

        Used to get the sheet out of the way of the app's own modal
        dialogs, which would otherwise open behind it.
        """
        self._panel_visible = visible
        self._track_window()

    def _apply_zoom(self) -> None:
        """Zoom the sheet so the panel's full width is visible."""
        if not self._excel_ready or not self._window:
            return
        zoom = self._zoom_override or self._zoom_to_width()
        if zoom is None:
            return
        try:
            _com_retry(lambda: setattr(self._window, "Zoom", zoom))
            _com_retry(lambda: setattr(self._window, "ScrollColumn", 1))
        except Exception:
            # Busy Excel (mid-edit) — the next resize or Fit will catch up.
            return
        self._force_repaint()

    def _force_repaint(self) -> None:
        """Make Excel redraw the whole grid.

        Sizing XLDESK and EXCEL7 by hand skips Excel's own layout pass, so
        floating shapes — the studio logo — are left half-painted from the
        previous zoom.  Invalidating the tree repaints them.
        """
        if not self._hwnd:
            return
        flags = (
            win32con.RDW_INVALIDATE | win32con.RDW_ERASE
            | win32con.RDW_ALLCHILDREN | win32con.RDW_UPDATENOW
        )
        try:
            win32gui.RedrawWindow(self._hwnd, None, None, flags)
        except Exception:
            pass

    def _zoom_to_width(self) -> Optional[int]:
        if not self._sheet_width_pts:
            return None
        scrollbar = win32api.GetSystemMetrics(win32con.SM_CXVSCROLL)
        available = max(60, self._host.width() - scrollbar - 2)
        natural_px = self._sheet_width_pts * 96.0 / 72.0
        return int(max(10, min(400, round(available / natural_px * 100))))

    def _fit_width(self) -> None:
        self._zoom_override = None
        self._apply_zoom()

    def _nudge_zoom(self, delta: int) -> None:
        if not self._excel_ready or not self._window:
            return
        try:
            current = int(_com_retry(lambda: self._window.Zoom))
        except Exception:
            current = self._zoom_to_width() or 100
        self._zoom_override = max(10, min(400, current + delta))
        self._apply_zoom()

    def _zoom_in(self) -> None:
        self._nudge_zoom(10)

    def _zoom_out(self) -> None:
        self._nudge_zoom(-10)

    # ------------------------------------------------------------------
    # Reading the sheet
    # ------------------------------------------------------------------
    #
    # There is deliberately no background poll of the cell values.  Every
    # COM call makes Excel busy for a moment, and an Excel that is busy
    # discards the keystroke that arrives while it is — a sheet polled on a
    # timer drops characters as fast as they are typed.  The values are
    # read when something actually needs them: Done, or Generate PDF.

    def _read_values(self, tries: int = 6) -> Dict[str, str]:
        """Every tracked cell, in as few round trips as possible.

        Asking for each of the fifty-odd cells separately is fifty COM
        calls, and a busy Excel makes each of them retry: at six tries and
        0.12s apiece that is half a minute of a frozen window, on the Save
        and Generate paths where it is least welcome.  One read of the
        whole block is a single call that either works or does not.
        """
        sheet = self._sheet
        if sheet is None:
            return dict(self._last_values)

        block = self._read_block(sheet, tries)
        if block is not None:
            return block

        # Excel would not give up the block — fall back to reading the
        # cells one at a time, with a single attempt each so a sheet that
        # is genuinely stuck does not hold the window.
        values: Dict[str, str] = {}
        for coord in ALL_DATA_COORDS:
            try:
                raw = _com_retry(lambda c=coord: sheet.Range(c).Value, tries=1,
                                 delay=0.0)
            except Exception:
                raw = None
            values[coord] = str(raw).strip() if raw is not None else ""
        return values

    def _read_block(self, sheet, tries: int) -> Optional[Dict[str, str]]:
        """SHEET_RANGE in one call, unpacked into the tracked coordinates."""
        try:
            grid = _com_retry(lambda: sheet.Range(SHEET_RANGE).Value, tries=tries)
        except Exception:
            return None
        if not grid:
            return None

        start_col, start_row = _split_cell(SHEET_RANGE.partition(":")[0])
        base_col = _column_index(start_col)

        values: Dict[str, str] = {}
        for coord in ALL_DATA_COORDS:
            column, row = _split_cell(coord)
            r = row - start_row
            c = _column_index(column) - base_col
            raw = None
            try:
                if 0 <= r < len(grid):
                    cells = grid[r]
                    if 0 <= c < len(cells):
                        raw = cells[c]
            except (TypeError, IndexError):
                raw = None
            values[coord] = str(raw).strip() if raw is not None else ""
        return values

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _stop_excel(self) -> None:
        if self._watched_window is not None:
            try:
                self._watched_window.removeEventFilter(self)
            except Exception:
                pass
            self._watched_window = None

        if self._calendar is not None:
            try:
                self._calendar.close()
                self._calendar.deleteLater()
            except Exception:
                pass
            self._calendar = None

        for timer_name in ("_poll_timer", "_track_timer"):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass
                setattr(self, timer_name, None)

        self._release_window()

        if self._excel is not None:
            try:
                if self._workbook is not None:
                    # Saved=True suppresses the save prompt; the working copy
                    # is a throwaway in the session temp directory anyway.
                    try:
                        self._workbook.Saved = True
                    except Exception:
                        pass
                    try:
                        self._workbook.Close(SaveChanges=False)
                    except Exception:
                        pass
                self._excel.Quit()
            except Exception:
                pass

        # Quit() is not always enough for an instance whose window we have
        # been driving from outside.  Never leave an orphan EXCEL.EXE
        # holding the working copy open.
        self._terminate_excel_process()

        self._excel = None
        self._workbook = None
        self._sheet = None
        self._window = None
        self._hwnd = None
        self._pid = None
        self._excel_ready = False

        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _release_window(self) -> None:
        """Take Excel's window off the screen and off our window.

        Dropping the ownership link first means a slow-quitting Excel
        cannot flash back up over the closing app.
        """
        if not self._hwnd or win32gui is None:
            return
        self._shown = False
        try:
            win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
            _set_window_owner(self._hwnd, 0)
        except Exception:
            pass

    def _terminate_excel_process(self) -> None:
        if not self._pid or win32api is None:
            return
        deadline = time.time() + 1.5
        while time.time() < deadline:
            if not self._process_alive(self._pid):
                return
            time.sleep(0.15)
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, self._pid)
            win32api.TerminateProcess(handle, 0)
            win32api.CloseHandle(handle)
            self._log("info", "Closed the panel's Excel instance")
        except Exception:
            pass

    @staticmethod
    def _process_alive(pid: int) -> bool:
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION,
                                          False, pid)
        except Exception:
            return False
        try:
            return win32process.GetExitCodeProcess(handle) == 259  # STILL_ACTIVE
        except Exception:
            return False
        finally:
            try:
                win32api.CloseHandle(handle)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Done
    # ------------------------------------------------------------------

    def _on_done_clicked(self) -> None:
        if not self._excel_ready:
            # Nothing live to export — let the window fall back to the
            # in-process renderer.
            self.generate_requested.emit()
            return
        self._done_btn.setEnabled(False)
        try:
            self.commit_pending_edit()
            if not self._excel_answers():
                QMessageBox.information(
                    self, "Finish the cell first",
                    "The sheet still has a cell open for editing, so it "
                    "cannot be exported yet.\n\nPress Enter in the panel to "
                    "finish that cell, then click Done again.",
                )
                return
            self._panel_pdf = self.export_panel_pdf(
                os.path.join(self._temp_dir, "panel_export.pdf")
            )
            self._log("success", "Side panel exported to PDF")
        except Exception as exc:
            self._panel_pdf = None
            self._log("error", f"Could not export the side panel: {exc}")
        finally:
            self._done_btn.setEnabled(True)
        self.generate_requested.emit()

    def commit_pending_edit(self) -> None:
        """Nudge Excel out of cell-edit mode so automation calls go through.

        A cell left half-typed blocks every COM call into Excel — clicking
        Done without pressing Enter first would otherwise read stale values
        or fail outright.  Posting Enter to whatever has the focus inside
        Excel ends the edit exactly as the architect pressing Enter would.
        """
        if not self._excel_ready or not WIN32_AVAILABLE or not self._hwnd:
            return
        if self._excel_answers():
            return
        focused = _excel_focus_window(self._hwnd)
        if not focused:
            return
        for _ in range(3):
            try:
                win32gui.PostMessage(focused, win32con.WM_KEYDOWN,
                                     win32con.VK_RETURN, 0)
                win32gui.PostMessage(focused, win32con.WM_CHAR, 13, 0)
                win32gui.PostMessage(focused, win32con.WM_KEYUP,
                                     win32con.VK_RETURN, 0)
            except Exception:
                return
            time.sleep(0.25)
            if self._excel_answers():
                return

    def _excel_answers(self) -> bool:
        """True when Excel is willing to take an automation call.

        A cell being edited makes every call fail with RPC_E_CALL_REJECTED,
        which is the one reliable way to tell from out here.
        """
        try:
            self._sheet.Range("A1").Row  # cheapest round trip there is
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def excel_path(self) -> str:
        """The workbook the panel was built from (the pristine template)."""
        return self._excel_path

    @property
    def working_path(self) -> str:
        """The copy Excel actually has open."""
        return self._working_path

    @property
    def panel_pdf(self) -> Optional[str]:
        """The most recent Done export, if any."""
        return self._panel_pdf

    @property
    def is_live(self) -> bool:
        """True when a real Excel instance is embedded and answering."""
        return self._excel_ready

    MIN_CELL_FONT_PT = 4.0
    CELL_PADDING_PT = 3.0
    LINE_SPACING = 1.18   # Excel's line pitch for a wrapped cell

    def _load_base_fonts(self) -> None:
        """Read each tracked cell's intended size from the template."""
        if self._base_font:
            return
        try:
            import openpyxl

            book = openpyxl.load_workbook(self.excel_path)
            sheet = book.active
            for coord in ALL_DATA_COORDS:
                cell = sheet[coord]
                size = float(cell.font.sz) if cell.font.sz else 10.0
                self._base_font[coord] = (size, bool(cell.font.bold))
                self._base_wrap[coord] = bool(cell.alignment.wrap_text)
            book.close()
        except Exception as exc:
            self._log("info", f"Could not read the panel's base fonts: {exc}")
            for coord in ALL_DATA_COORDS:
                self._base_font.setdefault(coord, (10.0, False))
                self._base_wrap.setdefault(coord, False)

    @staticmethod
    def _wrapped_extent(value: str, font: str, size: float, width: float):
        """(lines, widest line) for this value at this size in this width.

        The widest line matters as much as the count: a value with nothing
        to break on - a date, a drawing number - is always one line however
        narrow the cell, so judging a wrapping cell on height alone let it
        run straight past the border.
        """
        from reportlab.pdfbase.pdfmetrics import stringWidth

        total = 0
        widest = 0.0
        for paragraph in value.splitlines() or [""]:
            words = paragraph.split()
            if not words:
                total += 1
                continue
            lines = 1
            current = ""
            for word in words:
                trial = word if not current else current + " " + word
                if not current or stringWidth(trial, font, size) <= width:
                    current = trial
                else:
                    widest = max(widest, stringWidth(current, font, size))
                    lines += 1
                    current = word
            widest = max(widest, stringWidth(current, font, size))
            total += lines
        return max(1, total), widest

    def _fit_cell_font(self, coord: str, text: str) -> None:
        """Shrink one cell's type until its value fits inside it.

        Widths come from reportlab, which measures in points - the same
        unit Excel reports a merged area's width and height in - so no
        screen DPI creeps into the comparison.  A wrapping cell is judged
        on the height its lines need, not on one long line.
        """
        from reportlab.pdfbase.pdfmetrics import stringWidth

        base_size, bold = self._base_font.get(coord, (10.0, False))
        font = "Helvetica-Bold" if bold else "Helvetica"
        value = "" if text is None else str(text)

        size = base_size
        if value.strip():
            try:
                area = _com_retry(lambda: self._sheet.Range(coord).MergeArea)
                available = float(area.Width) - self.CELL_PADDING_PT
                height = float(area.Height) - 1.0
                wraps = bool(area.WrapText)
                # Null means the cell mixes formats - the copyright notice
                # has the studio name bold inside the sentence.  Writing one
                # size across it would flatten that, so leave it alone.
                if area.Cells(1, 1).Font.Size is None:
                    return
            except Exception:
                return
            if available > 0:
                while size > self.MIN_CELL_FONT_PT:
                    if wraps:
                        lines, widest = self._wrapped_extent(
                            value, font, size, available
                        )
                        # Both, not either: the height alone passes a value
                        # that cannot be broken, and it then overruns the
                        # side of the cell.
                        if (widest <= available
                                and lines * size * self.LINE_SPACING <= height):
                            break
                    elif stringWidth(value, font, size) <= available:
                        break
                    size -= 0.25
                size = max(self.MIN_CELL_FONT_PT, size)

        if abs(self._fitted_sizes.get(coord, base_size) - size) < 0.01:
            return
        try:
            _com_retry(lambda: setattr(
                self._sheet.Range(coord).MergeArea.Font, "Size", size
            ))
            self._fitted_sizes[coord] = size
        except Exception as exc:
            self._log("info", f"Could not resize {coord}: {exc}")

    def fit_cell_fonts(self, coords=None) -> None:
        """Re-fit the type in the given cells (all tracked cells by default)."""
        if not self._excel_ready or not self._sheet:
            return
        self._load_base_fonts()
        for coord in (coords if coords is not None else ALL_DATA_COORDS):
            self._fit_cell_font(coord, self._last_values.get(coord, ""))

    def get_data(self) -> Dict[str, str]:
        """Read the tracked cells from the live sheet."""
        if not self._excel_ready:
            return dict(self._last_values)
        try:
            self.commit_pending_edit()
            values = self._read_values()
        except Exception as exc:
            self._log("error", f"Could not read the side panel values: {exc}")
            return dict(self._last_values)
        if values != self._last_values:
            changed = [
                coord for coord, value in values.items()
                if self._last_values.get(coord) != value
            ]
            self._last_values = values
            # Only the cells that actually changed, so typing stays quick.
            self.fit_cell_fonts(changed)
            self.data_changed.emit()
        return values

    def cached_data(self) -> Dict[str, str]:
        """The last values read, without going near Excel.

        The autosave runs on a timer, and a COM call makes Excel busy for
        a moment — a busy Excel discards the keystroke that arrives while
        it is, so a minute-by-minute read would drop characters as they
        were typed.  These are whatever Done or Generate last saw, which
        is the right trade for a recovery copy.
        """
        return dict(self._last_values)

    def set_data(self, data: Dict[str, str]) -> None:
        """Write values into the live sheet (used when restoring a project).

        Every tracked cell holds a label or an identifier, so each one is
        formatted as text before it is written.  Otherwise Excel helpfully
        reads a scale of "1:100" as one minute past midnight.
        """
        if not self._excel_ready or not self._sheet:
            self._last_values.update({k: str(v) for k, v in data.items()})
            return
        sheet = self._sheet
        for coord, value in data.items():
            try:
                _com_retry(lambda c=coord, v=value: self._write_cell(sheet, c, v))
            except Exception:
                continue

    @staticmethod
    def _write_cell(sheet, coord: str, value) -> None:
        cell = sheet.Range(coord)
        text = str(value) if value not in (None, "") else None
        if text is not None:
            cell.NumberFormat = "@"
        cell.Value = text

    def refresh(self) -> None:
        """No-op — the sheet is live, there is nothing to redraw."""

    def export_panel_pdf(self, out_path: str) -> str:
        """Export the live sheet to PDF exactly as Excel would print it.

        Returns the path written.  Raises RuntimeError if Excel is not
        embedded or the export fails.
        """
        if not self._excel_ready or not self._sheet:
            raise RuntimeError("Excel is not running — nothing to export")

        out_path = os.path.abspath(out_path)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except OSError:
                pass

        previous_printer = self._use_responsive_printer()
        try:
            self._apply_print_setup()
            _com_retry(
                lambda: self._sheet.ExportAsFixedFormat(
                    _XL_TYPE_PDF, out_path, _XL_QUALITY_STANDARD
                ),
                tries=3,
                delay=0.4,
            )
        finally:
            if previous_printer:
                try:
                    self._excel.ActivePrinter = previous_printer
                except Exception:
                    pass

        if not os.path.isfile(out_path):
            raise RuntimeError("Excel reported success but wrote no PDF")
        return out_path

    def _use_responsive_printer(self) -> Optional[str]:
        """Point Excel at Print-to-PDF for the export; return the old printer.

        Returns None if the swap was not needed or not possible.
        """
        try:
            previous = str(self._excel.ActivePrinter)
        except Exception:
            return None
        if previous.startswith(_PDF_PRINTER_NAME):
            return None
        for port in _PDF_PRINTER_PORTS:
            try:
                self._excel.ActivePrinter = f"{_PDF_PRINTER_NAME} on {port}"
                return previous
            except Exception:
                continue
        self._log("info",
                  "Could not switch to Microsoft Print to PDF; the export may "
                  "be slow if the default printer is offline.")
        return None

    def _apply_print_setup(self) -> None:
        """Page setup for an export that is all panel and no paper.

        Every property is set with PrintCommunication off: each one
        otherwise round-trips to the printer driver, which is slow at best
        and hangs outright on an offline printer.
        """
        page = self._sheet.PageSetup
        try:
            self._excel.PrintCommunication = False
        except Exception:
            pass
        try:
            page.PrintArea = _absolute_range(SHEET_RANGE)
        except Exception:
            pass
        for name, value in (
            ("PaperSize", _XL_PAPER_A3),
            ("Orientation", _XL_PORTRAIT),
            ("LeftMargin", 0), ("RightMargin", 0),
            ("TopMargin", 0), ("BottomMargin", 0),
            ("HeaderMargin", 0), ("FooterMargin", 0),
            ("CenterHorizontally", False), ("CenterVertically", False),
            ("PrintGridlines", False), ("PrintHeadings", False),
            ("LeftHeader", ""), ("CenterHeader", ""), ("RightHeader", ""),
            ("LeftFooter", ""), ("CenterFooter", ""), ("RightFooter", ""),
            # 1:1 — A3 portrait is taller and wider than the panel, so the
            # sheet lands on one page at its true size and the compositor
            # can scale it once, precisely.
            ("Zoom", 100),
        ):
            try:
                setattr(page, name, value)
            except Exception:
                continue
        try:
            self._excel.PrintCommunication = True
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        widget = self.parent()
        while widget is not None:
            try:
                log = getattr(widget, "_log", None)
            except RuntimeError:
                # Walked into a widget whose C++ side has gone, which is
                # what happens on the way out of the app.
                break
            if callable(log):
                try:
                    log(level, message)
                    return
                except Exception:
                    pass
            try:
                widget = widget.parent()
            except RuntimeError:
                break

        # Never print.  A windowed PyInstaller build has no stdout, so
        # sys.stdout is None and print() raises AttributeError — and
        # raised from a slot, PySide ends the process without a word.
        # The one place this had to work is the packaged exe, which is
        # the one place it would have brought the app down.
        try:
            from app.core.crash_guard import get_logger

            getattr(get_logger(), "error" if level == "error" else "info")(
                "[side panel] %s", message
            )
        except Exception:  # noqa: BLE001 - logging must never be the fault
            pass


# ---------------------------------------------------------------------------
# Fallback renderer
# ---------------------------------------------------------------------------

def render_side_panel_merge(
    values: Dict[str, str],
    output_path: str,
    excel_path: Optional[str] = None,
    cropped_base: Optional[str] = None,
    full_base: Optional[str] = None,
) -> str:
    """Draw the panel from cell values without Excel.

    Used only when Excel is unavailable — the live panel exports itself.
    """
    from app.tools.render_side_panel import render_side_panel_merge as _render

    return _render(
        values=values,
        output_path=output_path,
        excel_path=excel_path or EXCEL_PATH,
        cropped_base=cropped_base or CROPPED_BASE,
        full_base=full_base or FULL_BASE,
    )


__all__ = ["ExcelStyleSidePanel", "render_side_panel_merge",
           "METADATA_COORDS", "ALL_DATA_COORDS", "DATE_COORDS"]
