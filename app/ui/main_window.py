"""
Main application window — fully wired WYSIWYG canvas.

Layout:
  - Central: CanvasView (WYSIWYG canvas showing real template PDF)
  - Top: toolbar with mode-switch, zoom, undo, and import controls
  - Left docks (tabified): Staging Tray, Callout Manager, Metadata
  - Bottom dock: Log panel
  - Status bar: current mode + last action
"""

import os
import tempfile
from typing import Optional

from PySide6.QtCore import Qt, QSize, QTimer, QRectF, QPointF, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import (
    QAction, QKeySequence, QPixmap, QImage, QIcon, QColor, QShortcut,
)
from PySide6.QtWidgets import (
    QMainWindow,
    QDialog,
    QFileDialog,
    QLabel,
    QStatusBar,
    QToolBar,
    QToolButton,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QPushButton,
    QMessageBox,
    QInputDialog,
    QMenu,
)

from app.ui.theme import THEME
from app.ui.canvas_scene import CanvasScene, PLACED_ITEM_BASE_Z
from app.ui.canvas_view import CanvasView, InteractionMode
from app.ui.log_panel import LogPanel
from app.ui.staging_tray import StagingTray
from app.ui.callout_manager import CalloutManager
from app.ui.background_task import BackgroundTask
from app.ui.image_bridge import PixmapCache, pil_to_qpixmap
from app.utils.paths import resource_path
from app.core import crash_guard, project_io
from app.core.project_state import ProjectState, PlacedItem, Annotation
from app.core.asset_importer import import_file, import_image
from app.core.cad_import import import_dwg, import_dxf, import_dwg_components, import_dxf_components, find_oda_converter
from app.core.template_registry import load_default_template
from app.core.pdf_compositor import generate_technical_sheet
from app.core.panel_compositor import attach_panel_pdf
from app.ui.excel_side_panel import (
    ExcelStyleSidePanel, render_side_panel_merge, METADATA_COORDS,
)
from app.core.image_processor import (
    crop_from_asset,
    apply_circular_mask,
    draw_circle_boundary,
)


# The view titles used across the firm's working drawings, ordered by
# how often they appear on the reference sheets.
VIEW_LABEL_NAMES = (
    "3D VIEW",
    "ELEVATION",
    "SECTION",
    "PLAN",
    "FRONT ELEVATION",
    "SIDE ELEVATION",
    "INTERNAL ELEVATION",
    "ISOMETRIC VIEW",
)


def _pil_to_qpixmap(pil_img) -> QPixmap:
    """Convert a PIL Image to QPixmap.

    See app.ui.image_bridge for why this is no longer done inline: the
    version that lived here left ``bytesPerLine`` to Qt, which pads every
    scanline to four bytes, so any RGB image whose width was not a
    multiple of four was read one to three bytes off per row — sheared
    across the frame, washed out, and reading past the end of the buffer
    on the last row.
    """
    return pil_to_qpixmap(pil_img)


# How often the sheet is written to the recovery file, in milliseconds.
# Short enough that a crash costs a minute of work at most; long enough
# that it never lands in the middle of a drag.
AUTOSAVE_INTERVAL_MS = 60_000


@crash_guard.guard_all
class MainWindow(QMainWindow):
    """Primary application window — WYSIWYG canvas with drag-and-drop."""

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Technical Drawing Sheet Automation")
        self.setMinimumSize(1200, 800)

        # ── Project state (§4 data model) ────────────────────────────
        self._project_state = ProjectState()

        # Where this sheet is saved, and how much of it is already on
        # disk.  ``_saved_revision`` is compared against the state's own
        # counter to decide whether there is anything to warn about.
        self._project_path: Optional[str] = None
        self._saved_revision: int = 0
        self._autosaved_revision: int = -1

        # Converted pixmaps, so rebuilding the canvas after an undo does
        # not re-encode every placed image from PIL.
        self._pixmaps = PixmapCache()

        # ── Canvas (central widget) ──────────────────────────────────
        self._scene = CanvasScene()
        self._canvas = CanvasView(self._scene)

        # ── Log panel (create first so other panels can log) ──────────
        self._log_panel = LogPanel()
        self._log = self._log_panel.log_callback

        # ── Load default template ─────────────────────────────────────
        template = load_default_template()
        if template:
            self._project_state.init_from_template(template)
            self._log("success", f"Loaded template: {template.template_name} "
                      f"({template.paper_size} {template.orientation})")
            # Rasterize template PDF and set as canvas background
            self._load_template_background(template)
        else:
            self._log("warning", "No default template found. Use File → Load Template "
                      "or run the calibrator tool first.")

        # Crop-in-progress state for the selected-item Crop button
        self._crop_target_id: str | None = None
        self._pending_item_crop: QRectF | None = None
        self._callout_crop_active: bool = False

        # Enter applies the crop wherever focus happens to be - relying on
        # the key bubbling up to the window let other widgets swallow it.
        # Kept disabled until there is actually a crop to apply, so Return
        # keeps its normal meaning everywhere else.
        self._apply_crop_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Return), self
        )
        self._apply_crop_shortcut.setContext(
            Qt.ShortcutContext.WindowShortcut
        )
        self._apply_crop_shortcut.activated.connect(self._apply_item_crop)
        self._apply_crop_shortcut.setEnabled(False)

        self._apply_crop_shortcut_kp = QShortcut(
            QKeySequence(Qt.Key.Key_Enter), self
        )
        self._apply_crop_shortcut_kp.setContext(
            Qt.ShortcutContext.WindowShortcut
        )
        self._apply_crop_shortcut_kp.activated.connect(self._apply_item_crop)
        self._apply_crop_shortcut_kp.setEnabled(False)

        # Type size for selected lettering, without cluttering the bar.
        for keys, step in (("Ctrl+]", 1.0), ("Ctrl+[", -1.0)):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(
                lambda s=step: self._resize_annotation_text(s)
            )

        # ── Panels ───────────────────────────────────────────────────
        self._staging_tray = StagingTray(self._project_state)
        self._callout_panel = CalloutManager(self._project_state)
        # Excel-style side panel for dynamic data entry (separate from PDF)
        self._excel_panel = ExcelStyleSidePanel()
        self._setup_workspace()

        # ── Wire signals ─────────────────────────────────────────────
        # Staging tray → canvas drop
        self._scene.item_placed.connect(self._on_item_dropped_on_canvas)
        self._scene.item_moved.connect(self._on_item_geometry_changed)
        self._scene.item_resized.connect(self._on_item_geometry_changed)

        # Staging tray → remove asset
        self._staging_tray.asset_remove_requested.connect(self._remove_asset)

        # Callout manager → crop mode + callout placement
        self._callout_panel.request_crop_mode.connect(self._enter_callout_crop_mode)
        self._scene.crop_box_created.connect(self._on_crop_box_created)
        self._callout_panel.callout_created.connect(self._on_callout_placed)

        # Grouping
        self._scene.group_requested.connect(self._on_group_requested)
        self._scene.item_ungroup_requested.connect(self._on_item_ungroup_requested)
        self._scene.group_moved.connect(self._on_group_moved)
        self._scene.group_rotated.connect(self._on_group_rotated)
        self._scene.selectionChanged.connect(self._on_selection_changed)
        self._scene.annotation_moved.connect(self._on_annotation_moved)
        self._scene.annotation_double_clicked.connect(self._edit_annotation_text)
        self._scene.annotation_edit_began.connect(self._on_annotation_edit_began)
        self._scene.annotation_edit_finished.connect(
            self._on_annotation_edit_finished)


        # ── Menu bar ─────────────────────────────────────────────────
        self._setup_menus()

        # ── Toolbar ──────────────────────────────────────────────────
        self._setup_toolbar()

        # ── Dock panels ──────────────────────────────────────────────
        # Panels live in the workspace as contextual overlays, not permanent docks.

        # Wire Excel panel signals
        self._excel_panel.data_changed.connect(self._on_excel_data_changed)
        self._excel_panel.generate_requested.connect(self._generate_pdf)

        # ── Status bar ───────────────────────────────────────────────
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready — Select mode active")

        # Default mode
        self._set_interaction_mode(InteractionMode.SELECT)

        # Initial panel refresh
        self._refresh_panels()
        self._update_history_buttons()
        self._update_window_title()

        # ── Keeping the work ─────────────────────────────────────────
        # Loading the template counted as an edit, so the baseline has to
        # be taken after it — otherwise a session in which nothing
        # happened still asked to be saved on the way out.
        self._saved_revision = self._project_state.revision

        # An unhandled error is now reported here instead of ending the
        # process; see app.core.crash_guard.
        crash_guard.set_report_hook(self._report_unhandled_error)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(AUTOSAVE_INTERVAL_MS)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start()
        project_io.mark_session_open()

        # Run startup health check, and offer back anything the last
        # session did not get to save.
        QTimer.singleShot(0, self._offer_recovery)
        QTimer.singleShot(0, self._run_startup_health_check)

    # ── Template background ───────────────────────────────────────────

    def _load_template_background(self, template) -> None:
        """Rasterize the template PDF and set it as the canvas background."""
        import pymupdf as fitz

        pdf_path = template.base_pdf_path
        if not os.path.isfile(pdf_path):
            self._log("warning", f"Template PDF not found: {pdf_path}")
            return

        try:
            doc = fitz.open(pdf_path)
            page = doc[0]
            page_rect = page.rect
            page_w_pts = page_rect.width
            page_h_pts = page_rect.height

            # Rasterize at 200 DPI for clear on-screen display
            dpi = 200
            zoom = dpi / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=matrix)

            # Convert to QPixmap
            qimg = QImage(pix.samples, pix.width, pix.height, pix.stride,
                          QImage.Format.Format_RGB888)
            qpixmap = QPixmap.fromImage(qimg)

            doc.close()

            # Set as canvas background
            self._scene.set_template_background(
                qpixmap, (page_w_pts, page_h_pts), dpi
            )

            # Load guide rectangles from template
            if template.guide_rects:
                self._scene.load_guide_rects(template.guide_rects)

            self._canvas.zoom_to_fit()
            self._log("info", f"Template background loaded: "
                      f"{page_w_pts:.0f}×{page_h_pts:.0f} pts at {dpi} DPI")
        except Exception as e:
            self._log("error", f"Failed to load template background: {e}")

    # ── Startup health check ──────────────────────────────────────────

    def _run_startup_health_check(self) -> None:
        """Check for ODA and the VC++ runtime without freezing the window.

        The check runs ODA once to see whether it starts, with a five
        second timeout.  On the UI thread that is five seconds of a dead
        window before the first sheet is even visible — and if ODA is
        broken rather than missing, it is five seconds every launch.
        """
        from app.core.startup_check import verify_prerequisites

        def work(report):
            report("Checking DWG support…")
            # The worker must not touch the log panel's widgets, so the
            # messages are collected and replayed on the UI thread.
            messages: list[tuple] = []
            status = verify_prerequisites(
                log=lambda sev, msg: messages.append((sev, msg))
            )
            return status, messages

        task = BackgroundTask(
            self,
            "Starting up",
            "Checking DWG support…",
            work,
            on_done=self._on_health_check_done,
            on_error=lambda msg: self._log(
                "warning", f"Could not check DWG support: {msg}"
            ),
        )
        task.start()

    def _on_health_check_done(self, result) -> None:
        status, messages = result
        for severity, message in messages:
            self._log(severity, message)

        incomplete = (
            status.get("oda") in ("missing", "broken")
            or status.get("vc_redist") in ("missing", "broken")
        )
        if not incomplete:
            return

        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("DWG Support Incomplete")
        msg.setText("DWG import isn't fully set up on this machine.")
        msg.setInformativeText(
            "DXF files still import normally.  Click Repair to install the "
            "missing components, or continue without DWG support."
        )
        repair_btn = msg.addButton("Repair", QMessageBox.ButtonRole.ActionRole)
        msg.addButton("Continue", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        if msg.clickedButton() is repair_btn:
            self._repair_dwg_support()

    def _repair_dwg_support(self) -> None:
        """Install the DWG prerequisites, off the UI thread.

        The repair waits on an elevated installer for up to three minutes.
        Run inline — as it was — Windows greys out the title bar and
        offers to close the program partway through, which is exactly
        what the architect must not do while an installer is running.
        """
        from app.core.startup_check import repair_dwg_support

        def work(report):
            messages: list[tuple] = []
            ok = repair_dwg_support(
                log=lambda sev, msg: messages.append((sev, msg)),
                report=report,
            )
            return ok, messages

        def done(result):
            ok, messages = result
            for severity, message in messages:
                self._log(severity, message)
            if ok:
                QMessageBox.information(
                    self, "Repair Complete",
                    "DWG support has been repaired and is now ready to use.",
                )
            else:
                QMessageBox.critical(
                    self, "Repair Failed",
                    "Could not complete DWG support repair. "
                    "See the activity log for details.",
                )

        task = BackgroundTask(
            self, "Repairing DWG support",
            "Requesting administrator permission…",
            work,
            on_done=done,
            on_error=lambda msg: QMessageBox.critical(
                self, "Repair Failed", f"The repair could not run:\n\n{msg}"
            ),
        )
        task.start()

    # ── Menu bar setup ────────────────────────────────────────────────

    def _setup_menus(self) -> None:
        menu_bar = self.menuBar()

        # File menu
        file_menu = menu_bar.addMenu("&File")

        # Opening and saving the sheet itself.  Until now the only thing
        # the app could write was the finished PDF, so quitting - or any
        # of the crashes this release fixes - took the whole arrangement
        # with it.
        new_action = QAction("&New Sheet", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self._new_project)
        file_menu.addAction(new_action)

        open_action = QAction("&Open Project…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._open_project)
        file_menu.addAction(open_action)

        save_action = QAction("&Save Project", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._save_project)
        file_menu.addAction(save_action)

        save_as_action = QAction("Save Project &As…", self)
        save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        save_as_action.triggered.connect(self._save_project_as)
        file_menu.addAction(save_as_action)

        self._recent_menu = file_menu.addMenu("Open &Recent")
        self._rebuild_recent_menu()

        file_menu.addSeparator()

        import_action = QAction("&Import File…", self)
        import_action.setShortcut(QKeySequence("Ctrl+I"))
        import_action.triggered.connect(self._import_file)
        file_menu.addAction(import_action)

        import_cad_action = QAction("Import CAD as &Single Drawing…", self)
        import_cad_action.triggered.connect(self._import_cad_file)
        file_menu.addAction(import_cad_action)

        import_cad_components_action = QAction("&Extract CAD Components…", self)
        import_cad_components_action.setToolTip(
            "Extract individual views/details from a CAD file as separate placeable assets"
        )
        import_cad_components_action.triggered.connect(self._import_cad_components)
        file_menu.addAction(import_cad_components_action)

        file_menu.addSeparator()

        generate_action = QAction("&Generate Technical Sheet (PDF)…", self)
        generate_action.setShortcut(QKeySequence("Ctrl+G"))
        generate_action.triggered.connect(self._generate_pdf)
        file_menu.addAction(generate_action)

        file_menu.addSeparator()

        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Edit menu
        edit_menu = menu_bar.addMenu("&Edit")

        undo_action = QAction("&Undo", self)
        undo_action.setShortcut(QKeySequence("Ctrl+Z"))
        undo_action.triggered.connect(self._undo)
        edit_menu.addAction(undo_action)

        redo_action = QAction("&Redo", self)
        redo_action.setShortcuts([QKeySequence("Ctrl+Y"),
                                  QKeySequence("Ctrl+Shift+Z")])
        redo_action.triggered.connect(self._redo)
        edit_menu.addAction(redo_action)

        # View menu
        view_menu = menu_bar.addMenu("&View")

        zoom_fit_action = QAction("Zoom to &Fit", self)
        zoom_fit_action.setShortcut(QKeySequence("Ctrl+0"))
        zoom_fit_action.triggered.connect(self._canvas.zoom_to_fit)
        view_menu.addAction(zoom_fit_action)

        zoom_reset_action = QAction("&Reset Zoom (1:1)", self)
        zoom_reset_action.setShortcut(QKeySequence("Ctrl+1"))
        zoom_reset_action.triggered.connect(self._canvas.reset_zoom)
        view_menu.addAction(zoom_reset_action)

        clear_overlays_action = QAction("&Clear Overlays", self)
        clear_overlays_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        clear_overlays_action.triggered.connect(self._scene.clear_overlays)
        view_menu.addAction(clear_overlays_action)

        # Help menu
        help_menu = menu_bar.addMenu("&Help")
        repair_action = QAction("&Repair DWG Support...", self)
        repair_action.triggered.connect(self._repair_dwg_support)
        help_menu.addAction(repair_action)

        log_action = QAction("Open the &Diagnostics Folder", self)
        log_action.setToolTip(
            "The session log, the crash log and the recovery copy all live "
            "here — send this folder on when reporting a problem."
        )
        log_action.triggered.connect(self._open_diagnostics_folder)
        help_menu.addAction(log_action)

    # ── Toolbar setup ─────────────────────────────────────────────────

    def _setup_toolbar(self) -> None:
        toolbar = QToolBar("Tools")
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        import_btn = QToolButton()
        import_btn.setIcon(QIcon(resource_path(os.path.join("app", "resources", "icons", "import.svg"))))
        import_btn.setIconSize(QSize(20, 20))
        import_btn.setToolTip("Import images/PDFs, or extract components from DWG/DXF (Ctrl+I)")
        import_btn.clicked.connect(self._import_file)
        toolbar.addWidget(import_btn)

        cad_extract_btn = QToolButton()
        cad_extract_btn.setText("Extract CAD")
        cad_extract_btn.setToolTip("Extract individual views/details from a DWG or DXF")
        cad_extract_btn.clicked.connect(self._import_cad_components)
        toolbar.addWidget(cad_extract_btn)

        # Mode-switch buttons
        self._select_btn = QToolButton()
        self._select_btn.setText("Select")
        self._select_btn.setCheckable(True)
        self._select_btn.setChecked(True)
        self._select_btn.setToolTip("Select, drag, and resize placed items (S)")
        self._select_btn.setShortcut(QKeySequence("S"))
        self._select_btn.clicked.connect(
            lambda: self._set_interaction_mode(InteractionMode.SELECT)
        )
        toolbar.addWidget(self._select_btn)

        self._crop_btn = QToolButton()
        self._crop_btn.setText("Crop")
        self._crop_btn.setCheckable(True)
        self._crop_btn.setToolTip(
            "Rectangle-drag to define a crop box (C)\n"
            "Alt-drag grows from the centre - Esc clears"
        )
        self._crop_btn.setShortcut(QKeySequence("C"))
        self._crop_btn.clicked.connect(
            lambda: self._set_interaction_mode(InteractionMode.CROP_BOX)
        )
        toolbar.addWidget(self._crop_btn)

        self._label_btn = QToolButton()
        self._label_btn.setText("View Label")
        self._label_btn.setToolTip(
            "Add a view title and pick which one (L).\n"
            "Drag it under the view; double-click to retype."
        )
        self._label_btn.setShortcut(QKeySequence("L"))
        self._label_btn.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        label_menu = QMenu(self._label_btn)
        for name in VIEW_LABEL_NAMES:
            action = label_menu.addAction(name)
            action.triggered.connect(
                lambda _checked=False, n=name: self._add_view_label(n)
            )
        label_menu.addSeparator()
        detail_action = label_menu.addAction("DETAIL (next letter)")
        detail_action.triggered.connect(
            lambda _checked=False: self._add_view_label(self._next_detail_label())
        )
        custom_action = label_menu.addAction("Custom...")
        custom_action.triggered.connect(
            lambda _checked=False: self._add_view_label(None)
        )
        self._label_btn.setMenu(label_menu)
        toolbar.addWidget(self._label_btn)

        note_btn = QToolButton()
        note_btn.setText("Detail Note")
        note_btn.setToolTip(
            "Add a note with a leader and a dot (N)\n"
            "Drag the wording and the dot separately"
        )
        note_btn.setShortcut(QKeySequence("N"))
        note_btn.clicked.connect(lambda: self._add_annotation("detail_note"))
        toolbar.addWidget(note_btn)

        title_btn = QToolButton()
        title_btn.setText("Sheet Title")
        title_btn.setToolTip("Add the sheet title across the bottom (T)")
        title_btn.setShortcut(QKeySequence("T"))
        title_btn.clicked.connect(lambda: self._add_annotation("sheet_title"))
        toolbar.addWidget(title_btn)

        self._circular_crop_btn = QToolButton()
        self._circular_crop_btn.setText("◎ Circular Crop")
        self._circular_crop_btn.setCheckable(True)
        self._circular_crop_btn.setToolTip(
            "Draw a circular crop area for callouts (Shift+C)\n"
            "Alt-drag grows from the centre - Esc clears"
        )
        self._circular_crop_btn.setShortcut(QKeySequence("Shift+C"))
        self._circular_crop_btn.clicked.connect(
            lambda: self._set_interaction_mode(InteractionMode.CIRCULAR_CROP)
        )
        toolbar.addWidget(self._circular_crop_btn)

        toolbar.addSeparator()

        # Undo button
        undo_btn = QToolButton()
        undo_btn.setText("Undo")
        undo_btn.setToolTip("Undo the last change (Ctrl+Z)")
        undo_btn.clicked.connect(self._undo)
        self._undo_btn = undo_btn
        toolbar.addWidget(undo_btn)

        redo_btn = QToolButton()
        redo_btn.setText("Redo")
        redo_btn.setToolTip("Redo the change you just undid (Ctrl+Y)")
        redo_btn.clicked.connect(self._redo)
        toolbar.addWidget(redo_btn)
        self._redo_btn = redo_btn

        toolbar.addSeparator()

        # Zoom controls
        zoom_fit_btn = QToolButton()
        zoom_fit_btn.setText("Fit")
        zoom_fit_btn.setToolTip("Zoom to fit (Ctrl+0)")
        zoom_fit_btn.clicked.connect(self._canvas.zoom_to_fit)
        toolbar.addWidget(zoom_fit_btn)

        zoom_reset_btn = QToolButton()
        zoom_reset_btn.setText("1:1")
        zoom_reset_btn.setToolTip("Reset zoom to 1:1 (Ctrl+1)")
        zoom_reset_btn.clicked.connect(self._canvas.reset_zoom)
        toolbar.addWidget(zoom_reset_btn)

        toolbar.addSeparator()

        # Clear overlays
        clear_btn = QToolButton()
        clear_btn.setText("Clear Overlays")
        clear_btn.setToolTip("Remove all crop boxes and anchor points")
        clear_btn.clicked.connect(self._scene.clear_overlays)
        toolbar.addWidget(clear_btn)

        toolbar.addSeparator()

        # Generate PDF button — the CTA
        self._generate_btn = QPushButton("Generate PDF")
        self._generate_btn.setObjectName("generateButton")
        self._generate_btn.setToolTip("Generate final A1 PDF: cropped canvas + editable Excel side panel (Ctrl+G)")
        self._generate_btn.clicked.connect(self._generate_pdf)
        toolbar.addWidget(self._generate_btn)

    _CROP_MODES = (InteractionMode.CROP_BOX, InteractionMode.CIRCULAR_CROP)

    def _set_interaction_mode(self, mode: InteractionMode) -> None:
        """Switch the canvas interaction mode and update toolbar state."""
        # Leaving either crop mode tears down the crop overlay.  Circular
        # crop used to be left out, which stranded the preview image on
        # top of the sheet with no way to dismiss it.
        if self._canvas.mode in self._CROP_MODES and mode not in self._CROP_MODES:
            self._scene.hide_preview_image()
            self._scene.clear_crop_boxes()
            self._cancel_item_crop(restore_mode=False)

        self._canvas.set_mode(mode)
        self._select_btn.setChecked(mode == InteractionMode.SELECT)
        self._crop_btn.setChecked(mode == InteractionMode.CROP_BOX)
        self._circular_crop_btn.setChecked(mode == InteractionMode.CIRCULAR_CROP)


        mode_labels = {
            InteractionMode.SELECT: "Select",
            InteractionMode.CROP_BOX: "Crop Box",
            InteractionMode.CIRCULAR_CROP: "Circular Crop",
            InteractionMode.LEADER_DRAG: "Leader Drag",
        }
        if mode in self._CROP_MODES:
            self._status_bar.showMessage(
                f"{mode_labels.get(mode, mode.name)} - drag to select, "
                "drag the box or its handles to adjust, Esc to clear"
            )
        else:
            self._status_bar.showMessage(
                f"{mode_labels.get(mode, mode.name)} mode active"
            )

    # ── Dock panels ───────────────────────────────────────────────────

    def _setup_docks(self) -> None:
        """Compatibility hook: panels are now contextual workspace overlays."""
        return

    def _icon_button(self, icon_name: str, tip: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("railButton")
        button.setCheckable(True)
        button.setIcon(QIcon(resource_path(os.path.join("app", "resources", "icons", icon_name))))
        button.setIconSize(QSize(20, 20))
        button.setToolTip(tip)
        return button

    def _floating_panel(self, content: QWidget) -> QFrame:
        panel = QFrame(self._workspace)
        panel.setObjectName("floatingPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(content)
        shadow = QGraphicsDropShadowEffect(panel)
        shadow.setBlurRadius(28); shadow.setOffset(0, 6); shadow.setColor(QColor(0, 0, 0, 110))
        panel.setGraphicsEffect(shadow)
        panel.hide()
        return panel

    def _metadata_panel(self) -> QWidget:
        panel = QFrame()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(20, 20, 20, 20)
        title = QLabel("Sheet details"); title.setObjectName("panelTitle")
        self._metadata_summary = QLabel("Project metadata is preserved with the current sheet.\n\nOpen a template and import views to begin composing.")
        self._metadata_summary.setObjectName("helpText"); self._metadata_summary.setWordWrap(True)
        layout.addWidget(title); layout.addWidget(self._metadata_summary); layout.addStretch(1)
        return panel

    def _setup_workspace(self) -> None:
        self._workspace = QFrame(); self._workspace.setObjectName("workspace")
        layout = QHBoxLayout(self._workspace)
        layout.setContentsMargins(12, 12, 12, 12); layout.setSpacing(12)
        self._rail = QFrame(); self._rail.setObjectName("leftRail"); self._rail.setFixedWidth(52)
        rail_layout = QVBoxLayout(self._rail); rail_layout.setContentsMargins(8, 10, 8, 10); rail_layout.setSpacing(8)
        self._rail_buttons = {}
        for key, icon, label in (("assets", "assets.svg", "Assets"), ("callouts", "callout.svg", "Callouts"), ("metadata", "info.svg", "Sheet details"), ("log", "log.svg", "Activity log")):
            button = self._icon_button(icon, label)
            button.clicked.connect(lambda checked, name=key: self._toggle_panel(name))
            self._rail_buttons[key] = button
            rail_layout.addWidget(button)
        rail_layout.addStretch(1)
        layout.addWidget(self._rail)

        # Central: Canvas (PDF template preview WITHOUT side panel)
        layout.addWidget(self._canvas, 1)

        # Right: Excel-style side panel (separate from PDF canvas)
        # This panel mimics the right-side of the base PDF template where
        # clients enter dynamic details. It stays separate during editing,
        # and gets merged into the final PDF at generation time.
        self._excel_panel_wrapper = QFrame()
        self._excel_panel_wrapper.setObjectName("excelPanelWrapper")
        excel_layout = QVBoxLayout(self._excel_panel_wrapper)
        excel_layout.setContentsMargins(0, 0, 0, 0)
        excel_layout.setSpacing(0)
        excel_layout.addWidget(self._excel_panel)
        layout.addWidget(self._excel_panel_wrapper)

        self.setCentralWidget(self._workspace)
        self._panels = {"assets": self._floating_panel(self._staging_tray), "callouts": self._floating_panel(self._callout_panel), "metadata": self._floating_panel(self._metadata_panel()), "log": self._floating_panel(self._log_panel)}
        self._active_panel = None
        self._canvas.canvas_background_clicked.connect(self._hide_active_panel)
        self._canvas.crop_cancelled.connect(self._cancel_item_crop)

        self._zoom_pill = QFrame(self._workspace); self._zoom_pill.setObjectName("zoomPill")
        zoom = QHBoxLayout(self._zoom_pill); zoom.setContentsMargins(8, 5, 8, 5)
        minus = QToolButton(); minus.setText("−"); minus.clicked.connect(lambda: self._zoom_by(1 / self._canvas.ZOOM_FACTOR))
        plus = QToolButton(); plus.setText("+"); plus.clicked.connect(lambda: self._zoom_by(self._canvas.ZOOM_FACTOR))
        fit = QToolButton(); fit.setText("Fit"); fit.setToolTip("Zoom to fit (Ctrl+0)"); fit.clicked.connect(self._canvas.zoom_to_fit)
        self._zoom_label = QLabel("100%")
        for widget in (minus, self._zoom_label, plus, fit): zoom.addWidget(widget)
        self._canvas.zoom_changed.connect(
            lambda z: self._zoom_label.setText(f"{z * 100:.0f}%")
        )

        self._selection_toolbar = QFrame(self._workspace); self._selection_toolbar.setObjectName("selectionToolbar")
        selected = QHBoxLayout(self._selection_toolbar); selected.setContentsMargins(8, 5, 8, 5)
        crop = QToolButton(); crop.setText("Crop"); crop.setToolTip("Crop this image against its full-size source"); crop.clicked.connect(self._start_item_crop)
        group_btn = QToolButton(); group_btn.setText("Group"); group_btn.setToolTip("Group the selected items (Ctrl+G)"); group_btn.clicked.connect(self._group_selection)
        ungroup_btn = QToolButton(); ungroup_btn.setText("Ungroup"); ungroup_btn.setToolTip("Break up the group (Ctrl+Shift+G)"); ungroup_btn.clicked.connect(self._ungroup_selection)
        front = QToolButton(); front.setText("Front"); front.clicked.connect(self._bring_selection_to_front)
        delete = self._icon_button("delete.svg", "Delete selected item"); delete.clicked.connect(self._delete_selected)
        for widget in (crop, group_btn, ungroup_btn, front, delete):
            selected.addWidget(widget)
        self._annotation_only_buttons = ()
        self._placed_only_buttons = (crop,)
        self._group_button = group_btn
        self._ungroup_button = ungroup_btn
        self._selection_toolbar.hide()
        self._scene.selectionChanged.connect(self._update_selection_toolbar)

        self._crop_hint = QLabel("", self._workspace)
        self._crop_hint.setObjectName("cropHint")
        self._crop_hint.hide()

    def _show_crop_hint(self, text: str) -> None:
        """Show what the crop keys do, anchored under the toolbar."""
        self._crop_hint.setText(text)
        self._crop_hint.adjustSize()
        self._crop_hint.move(
            max(12, (self._workspace.width() - self._crop_hint.width()) // 2), 70
        )
        self._crop_hint.show()
        self._crop_hint.raise_()

    def _hide_crop_hint(self) -> None:
        self._crop_hint.hide()

    def _zoom_by(self, factor: float) -> None:
        """Zoom from the pill's + / - buttons.

        Routed through the view's own stepper so it obeys ZOOM_MIN and
        ZOOM_MAX.  Applied directly - as it was - the buttons had no
        limits at all: holding + ran the scale into the thousands, where
        the scene transform loses precision and the canvas stops
        responding to anything.
        """
        import math

        steps = math.log(factor, self._canvas.ZOOM_FACTOR)
        self._canvas._zoom_by_steps(steps)
        self._update_zoom_label()

    def _update_zoom_label(self) -> None:
        if hasattr(self, "_zoom_label"):
            self._zoom_label.setText(f"{self._canvas._current_zoom * 100:.0f}%")

    def _toggle_panel(self, name: str) -> None:
        if self._active_panel == name:
            self._hide_active_panel(); return
        self._hide_active_panel()
        panel = self._panels[name]; self._active_panel = name; self._rail_buttons[name].setChecked(True)
        self._position_overlays(); end = panel.geometry(); start = QRectF(end).toRect(); start.moveLeft(-start.width())
        panel.setGeometry(start); panel.show()
        animation = QPropertyAnimation(panel, b"geometry", panel); animation.setDuration(180); animation.setEasingCurve(QEasingCurve.Type.OutCubic); animation.setStartValue(start); animation.setEndValue(end)
        panel._slide_animation = animation; animation.start()

    def _hide_active_panel(self) -> None:
        if self._active_panel:
            self._panels[self._active_panel].hide(); self._rail_buttons[self._active_panel].setChecked(False); self._active_panel = None

    def _position_overlays(self) -> None:
        if not hasattr(self, "_panels"): return
        x = self._rail.width() + 24; height = max(300, self._workspace.height() - 24); width = min(380, max(300, self._workspace.width() - x - 24))
        for panel in self._panels.values(): panel.setGeometry(x, 12, width, height)
        # Anchor the zoom pill to the canvas, not the workspace — the side
        # panel owns the right of the workspace and it is a native Excel
        # window, which would paint straight over the pill.
        canvas_area = self._canvas.geometry()
        self._zoom_pill.adjustSize()
        self._zoom_pill.move(
            canvas_area.right() - self._zoom_pill.width() - 24,
            canvas_area.bottom() - self._zoom_pill.height() - 24,
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._position_overlays()

    def _update_selection_toolbar(self) -> None:
        selected = self._scene.selectedItems()
        if not selected:
            self._selection_toolbar.hide(); return
        has_annotation = any(
            getattr(i, "annotation_id", None) for i in selected
        )
        has_placed = any(getattr(i, "placed_item_id", None) for i in selected)
        for widget in getattr(self, "_annotation_only_buttons", ()):
            widget.setVisible(has_annotation)
        for widget in getattr(self, "_placed_only_buttons", ()):
            widget.setVisible(has_placed)

        # Offer Group only when there is more than one thing to group, and
        # Ungroup only when the selection is actually in one.
        # Lettering counts as well as drawings.
        groupable = self._selected_groupable()
        if hasattr(self, "_group_button"):
            self._group_button.setVisible(len(groupable) > 1)
        if hasattr(self, "_ungroup_button"):
            self._ungroup_button.setVisible(
                any(getattr(r, "group_id", None) for r in groupable)
            )
        point = self._canvas.mapTo(self._workspace, self._canvas.mapFromScene(selected[0].sceneBoundingRect().topLeft()))
        self._selection_toolbar.adjustSize(); self._selection_toolbar.move(max(70, point.x()), max(12, point.y() - self._selection_toolbar.height() - 8)); self._selection_toolbar.show()

    def _bring_selection_to_front(self) -> None:
        """Raise the selection above the rest, on canvas and on the sheet.

        Setting only the canvas z-value left the model at its old order,
        so the generated PDF drew the item back underneath.
        """
        selected = self._selected_placed_items()
        if not selected:
            for item in self._scene.selectedItems():
                item.setZValue(500)
            return

        highest = max(
            (pi.z_order for pi in self._project_state.placed_items), default=0
        )
        with self._project_state.change("bring to front"):
            for offset, placed in enumerate(selected, start=1):
                placed.z_order = highest + offset
                gfx = self._scene.get_placed_item_gfx(placed.id)
                if gfx is not None:
                    gfx.setZValue(PLACED_ITEM_BASE_Z + placed.z_order)
        self._update_history_buttons()

    def _delete_selected(self) -> None:
        selected = list(self._scene.selectedItems())
        if not selected:
            return

        # Collect the ids before removing anything.  Reading an attribute
        # off a graphics item part-way through the loop can land on one
        # whose C++ side has already gone - a group member taken out with
        # its sibling - and that raises straight out of the slot, which
        # PySide treats as fatal.
        placed_ids, annotation_ids = [], []
        for item in selected:
            try:
                item_id = getattr(item, "placed_item_id", None)
                annotation_id = getattr(item, "annotation_id", None)
            except RuntimeError:
                continue
            if item_id:
                placed_ids.append(item_id)
            elif annotation_id:
                annotation_ids.append(annotation_id)

        if not placed_ids and not annotation_ids:
            return

        self._project_state.begin_change("delete")
        for item_id in placed_ids:
            self._scene.remove_placed_item(item_id)
            self._project_state.remove_placed_item(item_id, record_undo=False)
            # The cached pixmap stays: it is keyed by asset and crop, the
            # asset is still in the tray, and undoing this delete wants it
            # back immediately.
        for annotation_id in annotation_ids:
            self._scene.remove_annotation_item(annotation_id)
            self._project_state.remove_annotation(annotation_id)
        self._project_state.commit_change()

        self._update_history_buttons()
        self._refresh_panels()

    # ── Opening and saving the sheet ──────────────────────────────────

    def _has_content(self) -> bool:
        """Whether there is anything on the sheet worth keeping."""
        state = self._project_state
        return bool(state.assets or state.placed_items or state.annotations)

    def _is_dirty(self) -> bool:
        """Whether there are edits that are not on disk.

        An empty sheet is never dirty, whatever the counter says: loading
        the template bumps it, so without this the app asked to save on
        the way out of a session in which nothing had been done.
        """
        if not self._has_content():
            return False
        return self._project_state.revision != self._saved_revision

    def _update_window_title(self) -> None:
        name = (os.path.basename(self._project_path) if self._project_path
                else "Untitled sheet")
        marker = "*" if self._is_dirty() else ""
        self.setWindowTitle(
            f"{name}{marker} — Technical Drawing Sheet Automation"
        )

    def _confirm_discard(self, action: str) -> bool:
        """Ask before throwing away unsaved work.  True means carry on."""
        if not self._is_dirty():
            return True

        choice = QMessageBox.question(
            self,
            "Save this sheet?",
            f"This sheet has changes that are not saved.\n\n"
            f"Save before {action}?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Cancel:
            return False
        if choice == QMessageBox.StandardButton.Save:
            return self._save_project()
        return True

    def _new_project(self) -> None:
        if not self._confirm_discard("starting a new sheet"):
            return

        template = self._project_state.template
        self._project_state = ProjectState()
        if template is not None:
            self._project_state.init_from_template(template)

        self._project_path = None
        self._pixmaps.clear()
        self._scene.clear_placed_items()
        self._scene.clear_annotation_items()
        self._scene.clear_overlays()
        self._rebind_state()

        self._saved_revision = self._project_state.revision
        self._refresh_panels()
        self._update_history_buttons()
        self._update_window_title()
        self._log("info", "Started a new sheet")

    def _rebind_state(self) -> None:
        """Point the panels at the current ProjectState object.

        Replacing the state - on New, Open or a recovery - leaves the
        panels holding the old one, so the tray would keep showing the
        previous sheet's assets while the canvas showed the new one.
        """
        self._staging_tray._state = self._project_state
        self._callout_panel._state = self._project_state

    def _open_project(self, path: Optional[str] = None) -> None:
        if not self._confirm_discard("opening another sheet"):
            return

        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "Open Project", "", project_io.PROJECT_FILTER
            )
        if not path:
            return

        try:
            state, panel_values = project_io.load_project(
                path, log=self._log, vector_dir=self._session_vector_dir()
            )
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self._log("error", f"Could not open {os.path.basename(path)}: {exc}")
            QMessageBox.critical(
                self, "Could Not Open",
                f"That project could not be opened:\n\n{exc}",
            )
            return

        # A project saved on another machine names its template by a path
        # that may not exist here; fall back to the bundled one rather
        # than opening onto a blank canvas.
        if state.template is None or not os.path.isfile(state.template.base_pdf_path):
            fallback = load_default_template()
            if fallback is not None:
                state.template = fallback
                self._log("info",
                          "The saved template was not found on this machine; "
                          "using the bundled one.")

        self._project_state = state
        self._project_path = path
        self._pixmaps.clear()
        self._rebind_state()

        self._scene.clear_overlays()
        if state.template is not None:
            self._load_template_background(state.template)
        self._rebuild_canvas_items()

        if panel_values:
            try:
                self._excel_panel.set_data(panel_values)
            except Exception as exc:  # noqa: BLE001
                self._log("warning", f"Could not restore the side panel: {exc}")

        self._saved_revision = self._project_state.revision
        self._remember_recent(path)
        self._refresh_panels()
        self._update_history_buttons()
        self._update_window_title()
        self._canvas.zoom_to_fit()
        self._status_bar.showMessage(f"Opened {os.path.basename(path)}")

    def _save_project(self) -> bool:
        """Save to the known path, asking for one the first time."""
        if not self._project_path:
            return self._save_project_as()
        return self._write_project(self._project_path)

    def _save_project_as(self) -> bool:
        suggested = self._project_path or os.path.join(
            os.path.expanduser("~"),
            (self._project_state.metadata.sheet_title or "Untitled sheet")
            + project_io.PROJECT_EXTENSION,
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project As", suggested, project_io.PROJECT_FILTER
        )
        if not path:
            return False
        if not path.lower().endswith(project_io.PROJECT_EXTENSION):
            path += project_io.PROJECT_EXTENSION
        return self._write_project(path)

    def _write_project(self, path: str) -> bool:
        self._sync_canvas_to_state()
        try:
            project_io.save_project(
                self._project_state, path,
                panel_values=self._panel_values(),
                log=self._log,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self._log("error", f"Could not save the project: {exc}")
            QMessageBox.critical(
                self, "Could Not Save",
                f"The project could not be saved:\n\n{exc}",
            )
            return False

        self._project_path = path
        self._saved_revision = self._project_state.revision
        self._remember_recent(path)
        self._update_window_title()
        self._status_bar.showMessage(f"Saved {os.path.basename(path)}")
        return True

    def _panel_values(self) -> dict:
        """The side panel's cell values, if it can be asked for them."""
        try:
            return self._excel_panel.get_data()
        except Exception as exc:  # noqa: BLE001 - Excel may be mid-edit
            self._log("warning", f"Could not read the side panel: {exc}")
            return {}

    # ── Autosave and recovery ─────────────────────────────────────────

    def _autosave(self) -> None:
        """Write the sheet to the recovery file, if it has changed.

        Quiet by design: the only thing it says is a warning, and only
        when it fails.  A visible autosave that interrupts the architect
        every minute is worse than no autosave.
        """
        revision = self._project_state.revision
        if revision == self._autosaved_revision:
            return
        if not self._project_state.placed_items and not self._project_state.assets:
            return

        try:
            self._sync_canvas_to_state()
            project_io.save_project(
                self._project_state,
                project_io.autosave_path(),
                # The cached values, not a fresh read: a COM call makes
                # Excel busy for a moment and a busy Excel drops the
                # keystroke that arrives while it is, so a timer must not
                # reach into it.  Last-known is the right trade here.
                panel_values=self._excel_panel.cached_data(),
                log=lambda *_args: None,
            )
            self._autosaved_revision = revision
        except Exception as exc:  # noqa: BLE001 - autosave must never bite
            crash_guard.get_logger().warning("Autosave failed: %s", exc)

    def _offer_recovery(self) -> None:
        """Offer back a sheet from a session that ended badly."""
        if not project_io.has_unclean_recovery():
            return

        choice = QMessageBox.question(
            self,
            "Recover Your Sheet",
            "The last session did not close properly.\n\n"
            "There is a recovered copy of the sheet you were working on. "
            "Would you like to open it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if choice != QMessageBox.StandardButton.Yes:
            project_io.clear_autosave()
            project_io.mark_session_open()
            return

        self._open_project(project_io.autosave_path())
        # It came from the recovery file, not from a project of its own:
        # clear the path so Save asks where it should live.
        self._project_path = None
        self._saved_revision = -1
        self._update_window_title()

    # ── Recent files ──────────────────────────────────────────────────

    def _recent_store_path(self) -> str:
        return os.path.join(crash_guard.app_data_dir(), "recent.json")

    def _recent_projects(self) -> list:
        import json

        try:
            with open(self._recent_store_path(), encoding="utf-8") as handle:
                entries = json.load(handle)
        except (OSError, ValueError):
            return []
        return [p for p in entries if isinstance(p, str) and os.path.isfile(p)][:8]

    def _remember_recent(self, path: str) -> None:
        import json

        entries = [path] + [p for p in self._recent_projects() if p != path]
        try:
            with open(self._recent_store_path(), "w", encoding="utf-8") as handle:
                json.dump(entries[:8], handle)
        except OSError:
            pass
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        menu = getattr(self, "_recent_menu", None)
        if menu is None:
            return
        menu.clear()
        entries = self._recent_projects()
        if not entries:
            action = menu.addAction("Nothing yet")
            action.setEnabled(False)
            return
        for path in entries:
            action = menu.addAction(os.path.basename(path))
            action.setToolTip(path)
            action.triggered.connect(
                lambda _checked=False, p=path: self._open_project(p)
            )

    # ── Error reporting ───────────────────────────────────────────────

    def _report_unhandled_error(self, summary: str, detail: str) -> None:
        """Tell the architect about an error instead of dying on it.

        PySide6 ends the process when an exception escapes a slot, which
        is why this app used to vanish without a word.  crash_guard
        catches those; this is where one surfaces.  The sheet is written
        to the recovery file first, because whatever just failed may well
        fail again on the next click.
        """
        try:
            self._autosaved_revision = -1
            self._autosave()
        except Exception:  # noqa: BLE001
            pass

        self._log("error", summary)

        if not detail:
            # Something already reported, firing repeatedly - say so in
            # the log and leave the architect alone.
            self._status_bar.showMessage(f"Error: {summary}")
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Something Went Wrong")
        box.setText("An error interrupted that action.")
        box.setInformativeText(
            f"{summary}\n\nYour sheet is intact and has been saved to the "
            "recovery file. You can carry on working, but saving now is "
            "worth doing."
        )
        box.setDetailedText(detail)
        box.exec()

    def _open_diagnostics_folder(self) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        folder = crash_guard.app_data_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        self._log("info", f"Diagnostics folder: {folder}")

    # ── Import actions ────────────────────────────────────────────────

    def _import_file(self) -> None:
        """Import images/PDFs, routing CAD files to component extraction."""
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Import Files",
            "",
            "Supported Files (*.png *.jpg *.jpeg *.bmp *.tiff *.pdf *.dwg *.dxf);;Images & PDFs (*.png *.jpg *.jpeg *.bmp *.tiff *.pdf);;CAD Drawings (*.dwg *.dxf);;All Files (*)",
        )
        if not paths:
            return

        cad_paths = [p for p in paths
                     if os.path.splitext(p)[1].lower() in {".dwg", ".dxf"}]
        raster_paths = [p for p in paths if p not in cad_paths]

        if raster_paths:
            self._import_raster_paths(raster_paths)

        # CAD files each open their own review dialog, so they are run one
        # at a time rather than all at once.
        for path in cad_paths:
            self._import_cad_components_path(path)

    def _import_raster_paths(self, paths: list) -> None:
        """Import images and PDFs off the UI thread.

        Rasterising a multi-page PDF at 200 DPI takes seconds per page.
        Done inline, the window stops repainting for the duration and
        Windows offers to close it.
        """
        def work(report):
            messages: list[tuple] = []
            log = lambda sev, msg: messages.append((sev, msg))  # noqa: E731
            assets = []
            for index, path in enumerate(paths, start=1):
                report(f"Importing {os.path.basename(path)} "
                       f"({index} of {len(paths)})…")
                assets.extend(import_file(path, log=log))
            return assets, messages

        def done(result):
            assets, messages = result
            for severity, message in messages:
                self._log(severity, message)
            for asset in assets:
                self._project_state.add_asset(asset)
            self._refresh_panels()
            self._update_window_title()
            if assets:
                self._status_bar.showMessage(
                    f"{len(assets)} asset(s) ready — drag them onto the sheet"
                )
            else:
                self._status_bar.showMessage("Nothing could be imported")

        task = BackgroundTask(
            self, "Importing", "Reading files…", work,
            on_done=done,
            on_error=lambda msg: self._import_failed(msg),
        )
        task.start()

    def _import_failed(self, message: str) -> None:
        self._log("error", f"Import failed: {message}")
        QMessageBox.warning(
            self, "Import Failed",
            f"Those files could not be imported:\n\n{message}",
        )


    def _import_cad_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import CAD as Single Drawing", "",
            "CAD Drawings (*.dwg *.dxf);;All Files (*)",
        )
        if path:
            self._import_cad_path(path)

    def _import_cad_path(self, cad_path: str) -> None:
        """Import a whole CAD drawing as one asset, off the UI thread.

        Converting a .dwg runs ODA as a child process with a two-minute
        timeout.  On the UI thread that is two minutes of a frozen window
        and a "not responding" title bar — this app's single worst hang.
        """
        ext = os.path.splitext(cad_path)[1].lower()
        oda_exe = None
        if ext == ".dwg":
            oda_exe = find_oda_converter(log=self._log)
            if not oda_exe:
                err_msg = ("DWG import unavailable — ODA File Converter could not be found. "
                           "You can still import DXF files directly.")
                self._log("error", err_msg)
                QMessageBox.warning(self, "DWG Import Unavailable", err_msg)
                return

        def work(report):
            messages: list[tuple] = []
            log = lambda sev, msg: messages.append((sev, msg))  # noqa: E731
            report(f"Converting {os.path.basename(cad_path)}…")
            if ext == ".dwg":
                asset = import_dwg(cad_path, oda_path=oda_exe, log=log)
            else:
                asset = import_dxf(cad_path, log=log)
            return asset, messages

        def done(result):
            asset, messages = result
            for severity, message in messages:
                self._log(severity, message)
            if asset:
                self._project_state.add_asset(asset)
                self._refresh_panels()
                self._update_window_title()
                self._status_bar.showMessage(
                    "Drawing imported — drag it onto the sheet"
                )
            else:
                QMessageBox.warning(
                    self, "Import Failed",
                    "That CAD file could not be imported. "
                    "See the activity log for details.",
                )

        task = BackgroundTask(
            self, "Importing CAD",
            f"Converting {os.path.basename(cad_path)}…",
            work, on_done=done, on_error=self._import_failed,
        )
        task.start()

    def _import_cad_components(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Extract CAD Components…", "",
            "CAD Drawings (*.dwg *.dxf);;All Files (*)",
        )
        if not path:
            return

        self._import_cad_components_path(path)

    def _import_cad_components_path(self, path: str) -> None:
        """Detect the views in a CAD file, then review and render them.

        The detection half — ODA conversion, parsing, bounding-box work —
        runs on a worker thread, because on a large drawing it takes long
        enough for Windows to declare the window dead.  The review dialog
        and the rendering that follows it stay on the UI thread, since
        both need widgets.
        """
        ext = os.path.splitext(path)[1].lower()
        oda_exe = None
        if ext == ".dwg":
            oda_exe = find_oda_converter(log=self._log)
            if not oda_exe:
                err_msg = ("DWG import unavailable — ODA File Converter could not be found. "
                           "You can still import DXF files directly.")
                self._log("error", err_msg)
                QMessageBox.warning(self, "DWG Import Unavailable", err_msg)
                return

        def work(report):
            messages: list[tuple] = []
            log = lambda sev, msg: messages.append((sev, msg))  # noqa: E731
            report(f"Reading {os.path.basename(path)}…")
            if ext == ".dwg":
                result = import_dwg_components(path, oda_path=oda_exe, log=log)
            else:
                result = import_dxf_components(path, log=log)
            return result, messages

        def done(payload):
            result, messages = payload
            for severity, message in messages:
                self._log(severity, message)
            if result is None:
                QMessageBox.warning(
                    self, "Import Failed",
                    "That CAD file could not be read. "
                    "See the activity log for details.",
                )
                return
            self._review_cad_components(result, ext)

        task = BackgroundTask(
            self, "Extracting CAD components",
            f"Reading {os.path.basename(path)}…",
            work, on_done=done, on_error=self._import_failed,
        )
        task.start()

    def _session_vector_dir(self) -> Optional[str]:
        """Where this session keeps the vector copies of extracted views.

        Not the folder the DWG came from — that may be a read-only share,
        and writing next to someone's source drawing is not ours to do.
        Not the extraction temp folder either: that is cleaned up as soon
        as the import finishes, and these have to last the session.
        """
        existing = getattr(self, "_vector_dir", None)
        if existing and os.path.isdir(existing):
            return existing
        try:
            self._vector_dir = tempfile.mkdtemp(prefix="bny_vectors_")
            return self._vector_dir
        except OSError as exc:
            self._log("warning",
                      f"No place to keep vector line art ({exc}); views will "
                      "print from their raster copies.")
            return None

    def _review_cad_components(self, result, ext: str) -> None:
        """Show the detected components and import whichever are kept."""
        doc, msp, detection_result, cache, tmp_dir_obj, orig_path = result

        if not detection_result.components:
            QMessageBox.information(
                self, "No Components Detected",
                "No distinct view/detail components were detected in this CAD file.\n\n"
                "Try importing it as a whole drawing using File → Import CAD as Single Drawing instead.",
            )
            tmp_dir_obj.cleanup()
            return

        from app.ui.component_review_dialog import ComponentReviewDialog
        dialog = ComponentReviewDialog(
            doc, msp, detection_result, cache, orig_path,
            log_callback=self._log, parent=self,
        )

        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._log("info", "Component extraction cancelled by user")
            tmp_dir_obj.cleanup()
            return

        included = dialog.get_included_components()
        if not included:
            self._log("info", "No components selected for import")
            tmp_dir_obj.cleanup()
            return

        from app.core.dwg_components import render_component
        from app.core.project_state import ImportedAsset

        self._log("info", f"Rendering {len(included)} components at full quality...")
        source_type = "dwg_render" if ext == ".dwg" else "dxf_render"

        # Rendering is matplotlib at 300 DPI, once per component.  Ten
        # views is comfortably half a minute of a window that will not
        # repaint, so it goes to a worker with a progress dialog.
        from app.core.dwg_components import (
            COMPONENT_RENDER_DPI, render_component_vector,
        )

        vector_dir = self._session_vector_dir()

        def work(report):
            messages: list[tuple] = []
            log = lambda sev, msg: messages.append((sev, msg))  # noqa: E731
            rendered = []
            for index, comp in enumerate(included, start=1):
                report(f"Rendering {comp.id} ({index} of {len(included)})…")
                img = render_component(
                    doc, msp, comp, target_dpi=COMPONENT_RENDER_DPI,
                    cache=cache, log=log,
                )
                if img is None:
                    log("warning", f"Failed to render component: {comp.id}")
                    continue

                # And a vector copy, which is what actually reaches the
                # printed sheet.  Extracted views used to be raster only,
                # so a drawing blown up on the sheet printed its pixels;
                # the compositor prefers this whenever it is present.
                vector_path = None
                if vector_dir:
                    vector_path = render_component_vector(
                        doc, msp, comp,
                        os.path.join(vector_dir, f"{comp.id}.pdf"),
                        cache=cache, log=log,
                    )
                rendered.append((comp.id, img, vector_path))
            return rendered, messages

        def done(payload):
            rendered, messages = payload
            for severity, message in messages:
                self._log(severity, message)

            vectors = 0
            for comp_id, img, vector_path in rendered:
                if vector_path:
                    vectors += 1
                self._project_state.add_asset(ImportedAsset(
                    id=ImportedAsset.make_id(),
                    source_path=orig_path,
                    source_type=source_type,
                    image=img,
                    vector_source_path=vector_path,
                    dwg_source_path=orig_path,
                    dwg_component_id=comp_id,
                ))
            if vectors:
                self._log("success",
                          f"{vectors} of {len(rendered)} views carry vector "
                          "line art — those print without any pixelation")

            # Only once the renders are done: the temp folder holds the
            # converted DXF the document was read from, and clearing it
            # while the worker was still using it was a race waiting to
            # be noticed.
            tmp_dir_obj.cleanup()
            self._refresh_panels()
            self._update_window_title()
            self._log("success",
                      f"Imported {len(rendered)} components into the staging tray")
            self._status_bar.showMessage(
                f"{len(rendered)} components ready - drag them onto the sheet"
            )

        def failed(message: str) -> None:
            tmp_dir_obj.cleanup()
            self._import_failed(message)

        task = BackgroundTask(
            self, "Rendering components",
            f"Rendering {len(included)} components…",
            work, on_done=done, on_error=failed,
        )
        task.start()

    # ── Asset removal ─────────────────────────────────────────────────

    def _remove_asset(self, asset_id: str) -> None:
        """Remove an asset from the project, including any placed items."""
        asset = self._project_state.get_asset(asset_id)
        if asset is None:
            return

        placed_count = sum(
            1 for pi in self._project_state.placed_items
            if pi.source_asset_id == asset_id
        )
        if placed_count:
            confirm = QMessageBox.question(
                self, "Remove Asset",
                f"That asset is placed on the sheet {placed_count} time(s).\n\n"
                "Removing it takes those placements with it. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return

        # Remove any placed items that reference this asset from the canvas
        items_to_remove = [
            pi.id for pi in self._project_state.placed_items
            if pi.source_asset_id == asset_id
        ]
        with self._project_state.change("remove asset"):
            for pid in items_to_remove:
                self._scene.remove_placed_item(pid)
            self._project_state.remove_asset(asset_id)

        # Drop the cached conversions too, or a later asset reusing the id
        # would show this one's pixels.
        self._pixmaps.discard_asset(asset_id)

        self._refresh_panels()
        self._update_history_buttons()
        self._update_window_title()
        self._log("info", f"Removed asset: {asset_id}")

    # ── Drag-and-drop: item placed on canvas ──────────────────────────

    def _on_item_dropped_on_canvas(self, asset_id: str) -> None:
        """Handle an asset being dropped onto the canvas from the tray."""
        drop_info = self._scene.get_pending_drop_info()
        if not drop_info:
            return

        asset_id_from_drop, scene_pos = drop_info
        asset = self._project_state.get_asset(asset_id_from_drop)
        if not asset or not asset.image:
            return

        # Convert scene position to PDF points
        drop_x_pts, drop_y_pts = self._scene.scene_to_pts(scene_pos.x(), scene_pos.y())

        # Calculate default size: fit within ~25% of page width.
        # Fall back on the A1 default whenever the page size is not known
        # yet, not merely when there is no template: a template whose PDF
        # failed to rasterise leaves _page_size_pts at (0, 0), and the
        # item was then placed with a width of zero - dropped onto the
        # sheet and invisible, with nothing to grab to resize it.
        page_w_pts = 1684.0  # A1 landscape default
        if self._scene._page_size_pts[0] > 1.0:
            page_w_pts = float(self._scene._page_size_pts[0])

        img_w, img_h = asset.image.width, asset.image.height
        if img_w < 1 or img_h < 1:
            self._log("error", "That image has no pixels and cannot be placed.")
            return

        target_w = page_w_pts * 0.25
        scale = target_w / img_w
        target_h = img_h * scale

        # Center on drop point
        rect_x = drop_x_pts - target_w / 2
        rect_y = drop_y_pts - target_h / 2

        # Create PlacedItem
        placed_item = PlacedItem(
            id=PlacedItem.make_id(),
            item_type="image",
            source_asset_id=asset_id_from_drop,
            crop_box=None,
            page_rect=(rect_x, rect_y, target_w, target_h),
        )

        # Convert before committing: a file that turns out to be
        # unreadable should leave the sheet as it was, not add an item
        # with nothing in it.
        pixmap = self._item_pixmap(placed_item)
        if pixmap.isNull():
            self._log("error",
                      f"{asset.source_type} could not be drawn — the image "
                      "could not be read.")
            QMessageBox.warning(
                self, "Could Not Place Image",
                "That image could not be read. It may be damaged or in a "
                "format the app does not support.",
            )
            return

        try:
            self._project_state.add_placed_item(placed_item)
        except KeyError as e:
            self._log("error", f"Failed to place item: {e}")
            return

        self._scene.add_placed_item(pixmap, placed_item.page_rect, placed_item.id)

        self._log("info", f"Placed {asset.source_type} on sheet at "
                  f"({rect_x:.0f}, {rect_y:.0f})")
        self._status_bar.showMessage(
            f"Placed: {asset_id_from_drop} — drag to reposition, handles to resize"
        )
        self._update_history_buttons()
        self._update_window_title()

    def _on_item_geometry_changed(self, placed_item_id: str) -> None:
        """Handle when a PlacedItemGraphicsItem is resized or moved on the canvas."""
        graphics_item = self._scene._placed_items.get(placed_item_id)
        if not graphics_item:
            return

        new_rect = graphics_item.get_page_rect_pts(self._scene._scale_factor)
        
        # Find and update in the state
        # The signal arrives on mouse-up, so the model still holds where the
        # item was - snapshot first and the whole drag becomes one undo step.
        new_rotation = float(graphics_item.rotation())
        for item in self._project_state.placed_items:
            if item.id == placed_item_id:
                turned = abs(new_rotation - float(item.rotation)) > 1e-6
                label = "rotate" if turned and item.page_rect == new_rect \
                    else "move/resize"
                with self._project_state.change(label):
                    item.page_rect = new_rect
                    # Rotation lived only on the canvas item, so a turned
                    # view printed straight and snapped back on any rebuild.
                    item.rotation = new_rotation
                self._log("info", f"Updated {placed_item_id} bounds: {new_rect}")
                break
        self._update_history_buttons()

    # ── Sheet lettering ───────────────────────────────────────────────

    _ANNOTATION_DEFAULTS = {
        "view_label": ("PLAN", 13.0, 110.0),
        "detail_note": ("DETAIL NOTE", 12.0, 0.0),
        "sheet_title": ("SHEET TITLE", 26.0, 0.0),
    }

    def _pts_per_scene_unit(self) -> float:
        """Scene units per PDF point, for sizing lettering on canvas."""
        x0 = self._scene.pts_to_scene(0.0, 0.0)
        x1 = self._scene.pts_to_scene(1.0, 0.0)
        span = x1.x() - x0.x()
        return span if span > 1e-6 else 1.0

    def _visible_centre_pts(self) -> tuple:
        """Middle of what the architect is looking at, in PDF points."""
        view_centre = self._canvas.mapToScene(
            self._canvas.viewport().rect().center()
        )
        return self._scene.scene_to_pts(view_centre.x(), view_centre.y())

    def _next_detail_label(self) -> str:
        """The next unused DETAIL letter, the way the sheets number them."""
        used = {
            a.text.strip().upper()
            for a in self._project_state.annotations
            if a.kind == "view_label"
        }
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            candidate = f"DETAIL {letter}"
            if candidate not in used:
                return candidate
        return "DETAIL"

    def _add_view_label(self, name: Optional[str]) -> None:
        """Add a view title; ``None`` asks for the wording."""
        if name is None:
            name, ok = QInputDialog.getText(
                self, "View label", "Title:", text="PLAN"
            )
            if not ok or not name.strip():
                return
            name = name.strip().upper()
        self._add_annotation("view_label", text=name)

    def _add_annotation(self, kind: str, text: Optional[str] = None) -> None:
        """Place a new piece of sheet lettering in view, ready to drag."""
        default_text, font_size, rule_width = self._ANNOTATION_DEFAULTS.get(
            kind, ("TEXT", 12.0, 0.0)
        )
        text = text or default_text
        cx, cy = self._visible_centre_pts()

        if kind == "sheet_title":
            # The sheet title belongs along the bottom edge, so start it
            # there rather than wherever the view happens to be scrolled.
            sheet = self._scene.sceneRect()
            if not sheet.isEmpty():
                left, bottom = self._scene.scene_to_pts(
                    sheet.left(), sheet.bottom()
                )
                cx = left + 55.0
                cy = bottom - 45.0

        annotation = Annotation(
            id=Annotation.make_id(),
            kind=kind,
            text=text,
            page_pos=(cx, cy),
            font_size=font_size,
            rule_width=rule_width,
        )
        if kind == "detail_note":
            annotation.target_page_pos = (cx + 90.0, cy + 70.0)

        self._project_state.begin_change(
            f"add {kind.replace('_', ' ')}"
        )
        self._project_state.add_annotation(annotation)
        self._project_state.commit_change()
        item = self._scene.add_annotation_item(
            annotation, lambda v: v * self._pts_per_scene_unit()
        )
        if item is not None:
            self._scene.clearSelection()
            item.setSelected(True)

        self._log("info", f"Added {kind.replace('_', ' ')}: {text}")
        self._status_bar.showMessage(
            "Drag it into place - double-click to change the wording"
        )

    def _resize_annotation_text(self, step: float) -> None:
        """Nudge the type size of the selected lettering.

        The corner grips resize an annotation geometry; this is the
        keyboard route for the type size itself, bound to Ctrl+[ and
        Ctrl+] so there is no need for buttons on the selection bar.
        """
        changed = 0
        for item in self._scene.selectedItems():
            annotation_id = getattr(item, "annotation_id", None)
            if not annotation_id:
                continue
            annotation = self._project_state.get_annotation(annotation_id)
            if annotation is None:
                continue
            with self._project_state.change("type size"):
                annotation.font_size = max(
                    4.0, min(96.0, annotation.font_size + step)
                )
            if hasattr(item, "set_font_size"):
                item.set_font_size(
                    annotation.font_size * self._pts_per_scene_unit()
                )
            if hasattr(item, "sync_handles"):
                item.sync_handles()
            changed += 1
        if changed:
            self._status_bar.showMessage(f"Type size changed on {changed} item(s)")

    def _group_selection(self) -> None:
        """Group the selected drawings together."""
        self._project_state.begin_change("group")
        if not self._scene.group_selection():
            self._project_state.abandon_change()
            self._status_bar.showMessage(
                "Shift-click a second item first, then group"
            )
            return
        self._project_state.commit_change()
        self._update_history_buttons()

    def _selected_groupable(self) -> list:
        """Every selected thing that can be grouped, of either kind."""
        records = []
        for item_id in self._scene.selected_group_ids():
            record = (self._project_state.get_placed_item(item_id)
                      or self._project_state.get_annotation(item_id))
            if record is not None:
                records.append(record)
        return records

    def _selected_placed_items(self) -> list:
        """The PlacedItem records behind the current canvas selection."""
        items = []
        for graphic in self._scene.selectedItems():
            item_id = getattr(graphic, "placed_item_id", None)
            if not item_id:
                continue
            placed = self._project_state.get_placed_item(item_id)
            if placed is not None:
                items.append(placed)
        return items

    def _ungroup_selection(self) -> None:
        """Break up the group the selection belongs to."""
        if not any(getattr(r, "group_id", None)
                   for r in self._selected_groupable()):
            self._status_bar.showMessage("Nothing selected is in a group")
            return
        self._project_state.begin_change("ungroup")
        self._scene.ungroup_selection()
        self._project_state.commit_change()
        self._update_selection_toolbar()
        self._update_history_buttons()

    def _on_annotation_edit_began(self, annotation_id: str) -> None:
        """Remember the sheet before a piece of lettering is dragged."""
        self._project_state.begin_change("move lettering")

    def _on_annotation_edit_finished(self, annotation_id: str) -> None:
        """Bank the drag as one undo step, or drop it if nothing moved."""
        if self._project_state.commit_change():
            self._update_history_buttons()

    def _on_annotation_moved(self, annotation_id: str) -> None:
        """Write a dragged annotation's position back to the model."""
        annotation = self._project_state.get_annotation(annotation_id)
        item = self._scene.get_annotation_item(annotation_id)
        if annotation is None or item is None:
            return

        pos = item.pos()
        annotation.page_pos = self._scene.scene_to_pts(pos.x(), pos.y())

        if annotation.kind == "detail_note" and hasattr(item, "target_scene_pos"):
            target = item.target_scene_pos()
            annotation.target_page_pos = self._scene.scene_to_pts(
                target.x(), target.y()
            )
        elif annotation.kind == "view_label" and hasattr(item, "rule_width"):
            # The rule is dragged in scene units; the sheet stores points.
            annotation.rule_width = self._scene.scene_to_pts(
                item.rule_width, 0
            )[0]

    def _edit_annotation_text(self, annotation_id: str) -> None:
        """Retype an annotation's wording."""
        annotation = self._project_state.get_annotation(annotation_id)
        item = self._scene.get_annotation_item(annotation_id)
        if annotation is None or item is None:
            return

        multiline = annotation.kind == "detail_note"
        if multiline:
            text, ok = QInputDialog.getMultiLineText(
                self, "Detail note", "Wording (one line per row):", annotation.text
            )
        else:
            text, ok = QInputDialog.getText(
                self, "Sheet lettering", "Text:", text=annotation.text
            )
        if not ok:
            return

        with self._project_state.change("retype lettering"):
            annotation.text = text
        item.set_text(text)
        if annotation.kind == "view_label":
            # Keep the rule the architect sees and the one that prints
            # in step with the new wording.
            item.set_rule_width(annotation.rule_width * self._pts_per_scene_unit())
        self._log("info", f"Relabelled annotation: {text}")

    def _rebuild_annotation_items(self) -> None:
        self._scene.clear_annotation_items()
        scale = self._pts_per_scene_unit()
        for annotation in self._project_state.annotations:
            self._scene.add_annotation_item(annotation, lambda v: v * scale)

    # ── Callout crop workflow ─────────────────────────────────────────

    def _enter_callout_crop_mode(self) -> None:
        """Enter circular crop mode and show the selected crop source as a preview."""
        source_id = self._callout_panel.get_crop_source_id()
        if not source_id:
            return

        asset = self._project_state.get_asset(source_id)
        if not asset or not asset.image:
            return

        # Show the asset as a preview overlay
        self._scene.show_preview_image(
            _pil_to_qpixmap(asset.image),
            source_size=(asset.image.width, asset.image.height),
        )

        # Switch to CIRCULAR crop mode for callouts (like DINING BENCH LEG)
        self._set_interaction_mode(InteractionMode.CIRCULAR_CROP)
        self._callout_crop_active = True
        self._status_bar.showMessage(f"Draw circular crop on preview: {source_id}")

    def _on_crop_box_created(self, rect: QRectF) -> None:
        """Route a crop box to whatever asked for it.

        The Crop button on a selected item and the callout workflow both
        draw boxes; previously every box went to the callout panel, so
        cropping a placed image did nothing at all.
        """
        self._pending_item_crop = rect

        # The callout panel stores the box as source pixels, so a preview
        # that was scaled down to fit has to be undone first - otherwise
        # the detail circle is cut from the wrong part of the drawing.
        scale = self._scene.preview_scale() or 1.0
        self._callout_panel.receive_crop_box(
            QRectF(rect.x() * scale, rect.y() * scale,
                   rect.width() * scale, rect.height() * scale)
            if self._callout_crop_active and scale != 1.0 else rect
        )

        if self._crop_target(rect):
            shape = "Circular crop" if self._scene.is_circular_crop() else "Crop"
            self._show_crop_hint(
                f"{shape} {rect.width():.0f} x {rect.height():.0f} - "
                "Enter to apply, Esc to cancel"
            )
            self._set_crop_shortcut_enabled(True)
        elif not self._callout_crop_active:
            self._show_crop_hint(
                "Draw the crop box over an image, then press Enter"
            )

    # ── Cropping a placed image ───────────────────────────────────────

    def _start_item_crop(self) -> None:
        """Crop the selected placed image against its full-size source."""
        target_id = None
        for item in self._scene.selectedItems():
            target_id = getattr(item, "placed_item_id", None)
            if target_id:
                break

        placed_item = (
            self._project_state.get_placed_item(target_id) if target_id else None
        )
        if not placed_item:
            self._status_bar.showMessage("Select an image on the canvas to crop")
            return
        if placed_item.item_type == "callout_circle":
            self._status_bar.showMessage(
                "Re-crop a detail circle from the Callouts panel"
            )
            return

        asset = self._project_state.get_asset(placed_item.source_asset_id)
        if not asset or not asset.image:
            self._status_bar.showMessage("That item has no source image to crop")
            return

        self._crop_target_id = target_id
        self._pending_item_crop = None
        # The preview may be shown smaller than the asset — see
        # image_bridge.MAX_PIXMAP_EDGE — so the scene is told the real
        # pixel size and _apply_item_crop converts the box back.
        self._scene.show_preview_image(
            _pil_to_qpixmap(asset.image),
            source_size=(asset.image.width, asset.image.height),
        )
        self._set_interaction_mode(InteractionMode.CROP_BOX)

        # Start from the crop already on the item, so re-cropping adjusts
        # rather than starting from nothing.
        if placed_item.crop_box:
            scale = self._scene.preview_scale() or 1.0
            x1, y1, x2, y2 = (v / scale for v in placed_item.crop_box)
            self._scene.add_crop_box(QRectF(x1, y1, x2 - x1, y2 - y1))

        self._show_crop_hint(
            "Drag a crop box on the image - Enter to apply, Esc to cancel"
        )

    def _apply_item_crop(self) -> bool:
        """Commit the crop box on the canvas to its target item.

        Returns True when a crop was applied, so the caller knows whether
        to swallow the key press.
        """
        rect = self._scene.get_last_crop_box()
        if rect is None or rect.isEmpty():
            return False

        target_id = self._crop_target(rect)
        if target_id is None:
            self._status_bar.showMessage(
                "Draw the crop box over an image, then press Enter"
            )
            return False

        placed_item = self._project_state.get_placed_item(target_id)
        if placed_item is None:
            return False

        if self._crop_target_id:
            # Drawn over the source preview.  The preview is shown at
            # most MAX_PIXMAP_EDGE across, so a large asset appears
            # scaled down and the box has to be scaled back up into the
            # asset's own pixel space before it is stored.
            scale = self._scene.preview_scale() or 1.0
            crop_box = (
                rect.x() * scale, rect.y() * scale,
                (rect.x() + rect.width()) * scale,
                (rect.y() + rect.height()) * scale,
            )
        else:
            # Drawn straight onto the sheet - translate page to pixels.
            crop_box = self._crop_to_source_pixels(placed_item, rect)
            if crop_box is None:
                self._status_bar.showMessage(
                    "That crop box does not overlap the image"
                )
                return False

        circular = self._scene.is_circular_crop()
        if circular:
            # A circle needs a square source region or it comes out oval.
            x0, y0, x1, y1 = crop_box
            side = min(x1 - x0, y1 - y0)
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            crop_box = (cx - side / 2.0, cy - side / 2.0,
                        cx + side / 2.0, cy + side / 2.0)

        with self._project_state.change("crop"):
            placed_item.crop_box = crop_box
            placed_item.circular_mask = circular
            self._reframe_to_crop(placed_item, crop_box)

        width = crop_box[2] - crop_box[0]
        height = crop_box[3] - crop_box[1]
        self._log(
            "info",
            f"Cropped {placed_item.id} to {width:.0f} x {height:.0f} px"
            + (" (circular)" if circular else ""),
        )
        self._status_bar.showMessage(
            f"Cropped to {width:.0f} x {height:.0f} px"
            + (" - circular" if circular else "")
        )

        self._finish_crop()
        self._rebuild_canvas_items()

        # Keep it selected so the crop can be adjusted straight away.
        gfx = self._scene._placed_items.get(target_id)
        if gfx is not None:
            gfx.setSelected(True)
        return True

    def _set_crop_shortcut_enabled(self, enabled: bool) -> None:
        self._apply_crop_shortcut.setEnabled(enabled)
        self._apply_crop_shortcut_kp.setEnabled(enabled)

    def _finish_crop(self) -> None:
        """Tear down crop mode however it was entered."""
        self._set_crop_shortcut_enabled(False)
        self._crop_target_id = None
        self._pending_item_crop = None
        self._callout_crop_active = False
        self._hide_crop_hint()
        self._scene.hide_preview_image()
        self._scene.clear_crop_boxes()
        self._set_interaction_mode(InteractionMode.SELECT)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._apply_item_crop():
                event.accept()
                return
        super().keyPressEvent(event)

    def _crop_target(self, rect: QRectF | None = None) -> str | None:
        """Which placed item a crop should apply to.

        The Crop button names its own target.  Otherwise the crop was
        drawn straight onto the sheet, so use the selected item - or,
        failing that, whatever the box was drawn over.  Requiring a
        selection first is needless: the box is already on the image.
        """
        if self._crop_target_id:
            return self._crop_target_id
        if self._callout_crop_active:
            # The callout panel owns this crop; its own button creates it.
            return None

        if rect is None:
            rect = self._scene.get_last_crop_box()
        if rect is None or rect.isEmpty():
            return None

        # A selection is deliberate, but only if the box is actually on it;
        # otherwise go by what the box was drawn over.
        for item in self._scene.selectedItems():
            target_id = getattr(item, "placed_item_id", None)
            if target_id and not item.sceneBoundingRect().intersected(rect).isEmpty():
                return target_id

        return self._item_under_crop(rect)

    def _item_under_crop(self, rect: QRectF) -> str | None:
        """The frontmost placed item the crop box overlaps."""
        best_id = None
        best_key = None
        for item_id, gfx in self._scene._placed_items.items():
            overlap = gfx.sceneBoundingRect().intersected(rect)
            if overlap.isEmpty():
                continue
            key = (gfx.zValue(), overlap.width() * overlap.height())
            if best_key is None or key > best_key:
                best_id, best_key = item_id, key
        return best_id

    def _crop_to_source_pixels(
        self, placed_item, rect: QRectF
    ) -> tuple | None:
        """Convert a crop drawn on the canvas into source-image pixels.

        A box dragged over a placed item is in page coordinates, while
        ``crop_box`` is in the asset's own pixel space - without this the
        stored crop bears no relation to what was drawn.
        """
        gfx = self._scene._placed_items.get(placed_item.id)
        asset = self._project_state.get_asset(placed_item.source_asset_id)
        if gfx is None or asset is None or asset.image is None:
            return None

        # Into the item's own frame, so a rotated item still maps sanely.
        local = gfx.mapFromScene(rect).boundingRect()
        shown = gfx.rect()
        if shown.width() < 1 or shown.height() < 1:
            return None

        # The region of the source the item currently displays.
        if placed_item.crop_box:
            sx0, sy0, sx1, sy1 = placed_item.crop_box
        else:
            sx0, sy0 = 0.0, 0.0
            sx1, sy1 = float(asset.image.width), float(asset.image.height)
        src_w = sx1 - sx0
        src_h = sy1 - sy0

        def to_source(lx: float, ly: float) -> tuple[float, float]:
            u = (lx - shown.left()) / shown.width()
            v = (ly - shown.top()) / shown.height()
            return sx0 + u * src_w, sy0 + v * src_h

        # Clip to the item: a box dragged past the edge should crop to
        # the edge, not stretch content that is not there.
        local = local.intersected(shown)
        if local.isEmpty():
            return None

        x0, y0 = to_source(local.left(), local.top())
        x1, y1 = to_source(local.right(), local.bottom())

        # Clamp to the region actually on offer.
        x0 = max(sx0, min(x0, sx1))
        x1 = max(sx0, min(x1, sx1))
        y0 = max(sy0, min(y0, sy1))
        y1 = max(sy0, min(y1, sy1))
        if x1 - x0 < 1 or y1 - y0 < 1:
            return None
        return (x0, y0, x1, y1)

    def _reframe_to_crop(self, placed_item, crop_box: tuple) -> None:
        """Resize the item's page frame to match its new crop.

        The pixmap is stretched to fill page_rect, so leaving the old
        frame behind would squash the crop - a circular one would print
        as an oval.
        """
        asset = self._project_state.get_asset(placed_item.source_asset_id)
        if asset is None or asset.image is None:
            return
        crop_w = crop_box[2] - crop_box[0]
        crop_h = crop_box[3] - crop_box[1]
        if crop_w <= 0 or crop_h <= 0:
            return

        x, y, w, h = placed_item.page_rect
        # Keep the area roughly as it was, so the item neither leaps in
        # size nor shrinks away when re-cropped.
        area = max(w * h, 1.0)
        scale = (area / (crop_w * crop_h)) ** 0.5
        new_w = crop_w * scale
        new_h = crop_h * scale
        placed_item.page_rect = (
            x + (w - new_w) / 2.0,
            y + (h - new_h) / 2.0,
            new_w,
            new_h,
        )

    def _cancel_item_crop(self, restore_mode: bool = True) -> None:
        """Leave item-crop mode without applying anything."""
        was_cropping = bool(self._crop_target_id)
        self._set_crop_shortcut_enabled(False)
        self._crop_target_id = None
        self._pending_item_crop = None
        self._callout_crop_active = False
        self._hide_crop_hint()
        if was_cropping and restore_mode:
            self._scene.hide_preview_image()
            self._scene.clear_crop_boxes()
            self._set_interaction_mode(InteractionMode.SELECT)

    def _on_callout_placed(self, placed_item_id: str) -> None:
        """Handle a new callout being placed on the canvas."""
        placed_item = self._project_state.get_placed_item(placed_item_id)
        if not placed_item:
            return

        # Get the crop source and render the callout circle
        asset = self._project_state.get_asset(placed_item.source_asset_id)
        if not asset or not asset.image:
            return

        # Render the callout circle
        if placed_item.crop_box:
            cropped = crop_from_asset(asset.image, placed_item.crop_box)
        else:
            cropped = asset.image

        circle_size = int(placed_item.page_rect[2])  # width as circle diameter
        masked = apply_circular_mask(cropped, output_size=circle_size * 2)
        ringed = draw_circle_boundary(
            masked, leader_style=placed_item.leader_style,
            sheet_diameter_pts=float(placed_item.page_rect[2]),
        )

        # Convert to QPixmap and add to canvas
        pixmap = _pil_to_qpixmap(ringed)
        self._scene.add_placed_item(pixmap, placed_item.page_rect, placed_item.id)

        # Add leader line
        cx, cy, cw, ch = placed_item.page_rect
        circle_center = (cx + cw / 2, cy + ch / 2)
        if placed_item.leader_target_page_pos:
            self._scene.add_leader_line(
                placed_item.id,
                circle_center,
                placed_item.leader_target_page_pos,
                placed_item.leader_style,
            )

        # Return to select mode
        self._scene.hide_preview_image()
        self._scene.clear_crop_boxes()
        self._set_interaction_mode(InteractionMode.SELECT)

        self._log("info", f"Callout placed: {placed_item.callout_id} — "
                  f"drag the circle and leader line to final position")


    # ── Grouping ──────────────────────────────────────────────────────

    def _on_selection_changed(self) -> None:
        """When an item is selected, also select its group members if any."""
        if not self._scene.views():
            return
        if self._scene.views()[0].dragMode() == self._scene.views()[0].DragMode.RubberBandDrag:
            return # Let the user drag a box without interference
            
        # Picking one member of a group brings the whole group with it, so
        # dragging any part of it moves the lot - lettering included.
        for item_id in self._scene.selected_group_ids():
            group_id = self._project_state.group_id_for(item_id)
            if not group_id:
                continue
            for member_id in self._project_state.get_group_member_ids(group_id):
                member = (self._scene.get_placed_item_gfx(member_id)
                          or self._scene.get_annotation_item(member_id))
                if member is not None and not member.isSelected():
                    member.setSelected(True)

    def _on_group_requested(self, item_ids: list[str]) -> None:
        group_id = self._project_state.create_group(item_ids)
        self._status_bar.showMessage(f"Grouped {len(item_ids)} items")
        self._log("info", f"Grouped {len(item_ids)} items")

    def _on_item_ungroup_requested(self, item_id: str) -> None:
        group_id = self._project_state.group_id_for(item_id)
        if group_id:
            self._project_state.ungroup(group_id)
            self._status_bar.showMessage("Ungrouped items")
            self._log("info", "Ungrouped items")

    def _on_group_moved(self, placed_item_id: str) -> None:
        """Move all other members of the group proportionally."""
        pi = self._project_state.get_placed_item(placed_item_id)
        if not pi or not pi.group_id:
            return
            
        # We rely on Qt's built-in multiple selection dragging for the actual movement.
        # Since _on_selection_changed selects all group members together, 
        # dragging one visually drags them all simultaneously.
        pass

    def _on_group_rotated(self, placed_item_id: str, angle: float) -> None:
        """Apply the same rotation to all group members (around their own centers).

        Written straight to the model.  Re-emitting ``item_resized`` per
        member - what this used to do - routed each one through
        _on_item_geometry_changed, which opens an undo step of its own:
        turning a group of four cost four presses of Ctrl+Z to undo, and
        the nested begin_change calls lost the first member's snapshot.
        """
        pi = self._project_state.get_placed_item(placed_item_id)
        if not pi or not pi.group_id:
            return

        with self._project_state.change("rotate group"):
            for member_pi in self._project_state.get_group_members(pi.group_id):
                if member_pi.id == placed_item_id:
                    continue
                member_pi.rotation = angle
                gfx = self._scene.get_placed_item_gfx(member_pi.id)
                if gfx is not None:
                    gfx.setRotation(angle)
                    member_pi.page_rect = gfx.get_page_rect_pts(
                        self._scene._scale_factor
                    )
        self._update_history_buttons()
    # ── Undo ──────────────────────────────────────────────────────────

    def _undo(self) -> None:
        """Step back one edit."""
        label = self._project_state.undo()
        if label is None:
            self._status_bar.showMessage("Nothing to undo")
            return
        self._after_history_step(f"Undid: {label}")

    def _redo(self) -> None:
        """Step forward again after an undo."""
        label = self._project_state.redo()
        if label is None:
            self._status_bar.showMessage("Nothing to redo")
            return
        self._after_history_step(f"Redid: {label}")

    def _after_history_step(self, message: str) -> None:
        self._rebuild_canvas_items()
        self._refresh_panels()
        self._update_history_buttons()
        self._log("info", message)
        self._status_bar.showMessage(message)

    def _update_history_buttons(self) -> None:
        """Grey out undo/redo when there is nothing on that side.

        Also the one place every edit passes through, so the title bar's
        unsaved-changes marker is refreshed from here rather than being
        remembered at each of the two dozen call sites.
        """
        self._update_window_title()

        if hasattr(self, "_undo_btn"):
            self._undo_btn.setEnabled(self._project_state.can_undo())
            label = self._project_state.undo_label()
            self._undo_btn.setToolTip(
                f"Undo {label} (Ctrl+Z)" if label else "Nothing to undo"
            )
        if hasattr(self, "_redo_btn"):
            self._redo_btn.setEnabled(self._project_state.can_redo())
            label = self._project_state.redo_label()
            self._redo_btn.setToolTip(
                f"Redo {label} (Ctrl+Y)" if label else "Nothing to redo"
            )

    def _item_pixmap(self, pi) -> QPixmap:
        """The pixmap for one placed item, converting it at most once.

        This runs for every item on every undo, redo and crop.  Doing the
        crop, the mask and the PIL-to-Qt conversion afresh each time meant
        hundreds of megabytes of churn per keystroke on a sheet carrying
        CAD renders, which is what made Ctrl+Z feel like the app had
        stopped responding.
        """
        asset = self._project_state.get_asset(pi.source_asset_id)
        if asset is None or asset.image is None:
            return QPixmap()

        key = (
            pi.source_asset_id,
            pi.item_type,
            tuple(pi.crop_box) if pi.crop_box else None,
            bool(getattr(pi, "circular_mask", False)),
            pi.leader_style if pi.item_type == "callout_circle" else None,
            # The ring's weight is set from the printed diameter, so a
            # resized callout genuinely needs redrawing — rounded, so a
            # one-point drag does not invalidate it.
            round(float(pi.page_rect[2])) if pi.item_type == "callout_circle" else None,
        )
        cached = self._pixmaps.get(key)
        if cached is not None:
            return cached

        if pi.item_type == "callout_circle" and pi.crop_box:
            # Render callout circle
            cropped = crop_from_asset(asset.image, pi.crop_box)
            circle_size = int(pi.page_rect[2]) * 2
            masked = apply_circular_mask(
                cropped, output_size=max(min(circle_size, 2048), 50)
            )
            image = draw_circle_boundary(
                masked, leader_style=pi.leader_style,
                sheet_diameter_pts=float(pi.page_rect[2]),
            )
        elif pi.crop_box:
            # The PDF has always honoured crop_box for images; the
            # canvas did not, so a crop looked like it had done nothing.
            image = crop_from_asset(asset.image, pi.crop_box)
            if getattr(pi, "circular_mask", False):
                image = apply_circular_mask(
                    image, output_size=min(image.width, image.height)
                )
        else:
            image = asset.image

        return self._pixmaps.put(key, _pil_to_qpixmap(image))

    def _rebuild_canvas_items(self) -> None:
        """Rebuild all canvas placed items from the project state.
        Used after undo operations."""
        self._scene.clear_placed_items()
        self._rebuild_annotation_items()

        # Draw in the order the sheet records, so bring-to-front survives.
        for pi in sorted(self._project_state.placed_items,
                         key=lambda item: item.z_order):
            asset = self._project_state.get_asset(pi.source_asset_id)
            if not asset or not asset.image:
                continue

            pixmap = self._item_pixmap(pi)
            if pixmap.isNull():
                self._log("warning",
                          f"Could not draw {pi.id} — its image could not be read.")
                continue

            gfx = self._scene.add_placed_item(pixmap, pi.page_rect, pi.id)
            gfx.setZValue(PLACED_ITEM_BASE_Z + pi.z_order)
            if pi.rotation:
                gfx.setRotation(pi.rotation)

            # Re-add leader lines for callouts
            if pi.item_type == "callout_circle" and pi.leader_target_page_pos:
                cx, cy, cw, ch = pi.page_rect
                self._scene.add_leader_line(
                    pi.id,
                    (cx + cw / 2, cy + ch / 2),
                    pi.leader_target_page_pos,
                    pi.leader_style,
                )

    # ── Generate PDF ──────────────────────────────────────────────────

    def _generate_pdf(self) -> None:
        """Generate the technical drawing sheet PDF with merged side panel.

        The workflow:
        1. Sync Excel panel data to project state metadata.
        2. Generate the base PDF from placed items (template + assets).
        3. Merge the Excel side panel data onto the base PDF at the correct
           positions (matching the base template's right-side panel area).
        4. The result is the complete original-dimension PDF with the side
           panel populated.
        """
        # Sync placed items from canvas to project state
        self._sync_canvas_to_state()

        # Sync Excel panel data to project state metadata so readiness check works
        self._sync_excel_to_metadata()

        # Readiness check
        ready, issues = self._project_state.is_ready_to_generate()
        if not ready:
            for issue in issues:
                self._log("warning", f"Not ready: {issue}")
            QMessageBox.warning(
                self,
                "Cannot Generate",
                "The project is not ready to generate:\n\n• " +
                "\n• ".join(issues),
            )
            return

        # Ask for output path
        output_path, _ = QFileDialog.getSaveFileName(
            self, "Save Technical Sheet PDF", "",
            "PDF Files (*.pdf);;All Files (*)",
        )
        if not output_path:
            return

        if not output_path.lower().endswith(".pdf"):
            output_path += ".pdf"

        self._log("info", f"Generating PDF: {output_path}")

        # The intermediate goes in the temp folder, keyed by this process.
        # Deriving it with output_path.replace(".pdf", ...) put it beside
        # the finished sheet - visible to the architect if anything went
        # wrong before the cleanup - and mangled the path outright when a
        # parent folder happened to contain ".pdf".
        base_output = os.path.join(
            tempfile.gettempdir(), f"bny_sheet_base_{os.getpid()}.pdf"
        )

        try:
            # Step 1: Generate the base PDF (template + placed assets)
            result = generate_technical_sheet(self._project_state, base_output)
            self._log("info", f"Base PDF generated: {result}")

            # Step 2: Attach the side panel to the right of the cropped
            # drawing -> full-size sheet.
            final_output = output_path
            panel_pdf = self._export_side_panel()
            if panel_pdf:
                attach_panel_pdf(
                    cropped_pdf=base_output,
                    panel_pdf=panel_pdf,
                    output_path=final_output,
                    full_base=self._excel_panel.full_base,
                )
                self._log("info", "Side panel attached from the live Excel sheet")
            else:
                # Excel is not available — draw the panel from cell values.
                side_panel_data = self._excel_panel.get_data()
                self._log("warning",
                          "Excel panel unavailable; drawing the panel from "
                          f"{len(side_panel_data)} stored fields instead")
                render_side_panel_merge(
                    values=side_panel_data,
                    output_path=final_output,
                    excel_path=self._excel_panel.excel_path,
                    cropped_base=base_output,
                    full_base=self._excel_panel.full_base,
                )

            self._log("success", f"PDF generated successfully: {final_output}")
            QMessageBox.information(
                self, "Success",
                f"Technical sheet generated with merged side panel:\n{final_output}",
            )
        except (ValueError, FileNotFoundError) as e:
            self._log("error", f"PDF generation failed: {e}")
            QMessageBox.critical(self, "Generation Failed",
                                 f"Could not generate the PDF:\n\n{e}")
        except Exception as e:
            self._log("error", f"Unexpected error during PDF generation: {e}")
            crash_guard.get_logger().exception("PDF generation failed")
            QMessageBox.critical(self, "Generation Failed",
                                 f"An unexpected error occurred:\n\n{e}")
        finally:
            # On every path, including the failures: a half-written
            # intermediate left in temp was never cleaned up before, and
            # the next run would merge the stale one.
            try:
                if os.path.exists(base_output):
                    os.remove(base_output)
            except OSError:
                pass

    def _export_side_panel(self) -> Optional[str]:
        """Return a PDF of the live side panel, or None if Excel is not up.

        Done already exports on click; reuse that file when it is still
        there so clicking Done then Generate does not export twice.
        """
        if not self._excel_panel.is_live:
            return None
        existing = self._excel_panel.panel_pdf
        if existing and os.path.isfile(existing):
            return existing
        try:
            return self._excel_panel.export_panel_pdf(
                os.path.join(tempfile.gettempdir(), "bny_side_panel.pdf")
            )
        except Exception as e:
            self._log("error", f"Could not export the side panel: {e}")
            return None

    # Which side-panel cell holds which title-block field.
    #
    # This used to name the cells one row above these - B26, B24, B30 -
    # which are the sheet's *labels* ("CLIENT TITLE", "PROJECT TITLE"),
    # not the boxes underneath that the architect types into.  Those
    # label cells are not tracked by the panel either, so the mapping
    # never fired: client name, project title and sheet title stayed
    # empty however much was typed, and Generate refused every time with
    # "Client name is empty".  The G-column entries were off by one in
    # the same direction, so scale was filled with the paper size and
    # revision number with the scale.
    #
    # METADATA_COORDS in excel_side_panel is the same list in the same
    # order; it is imported here so the two cannot drift apart again.
    _METADATA_FIELDS = (
        "client_name", "project_title", "sheet_title", "job_no",
        "drawing_no", "scale", "paper_size", "rev_no",
    )

    def _sync_excel_to_metadata(self) -> None:
        """Sync Excel side panel data to ProjectState metadata.

        This ensures the readiness check and PDF generation can access
        the data entered in the Excel panel.
        """
        excel_data = self._panel_values()
        metadata = self._project_state.metadata

        for coord, field in zip(METADATA_COORDS, self._METADATA_FIELDS):
            value = (excel_data.get(coord) or "").strip()
            if value:
                setattr(metadata, field, value)

        # The description box, which the sheet title falls back to when
        # the drawing-title cell is left empty.
        description = (excel_data.get("B29") or "").strip()
        if description:
            metadata.description = description
            if not metadata.sheet_title:
                metadata.sheet_title = description

    def _on_excel_data_changed(self) -> None:
        """Handle changes in the Excel side panel data."""
        self._log("info", "Sheet data updated - ready for PDF generation")
        self._status_bar.showMessage("Sheet data updated")

    def _sync_canvas_to_state(self) -> None:
        """Sync canvas item positions back to project state placed items.
        This ensures the PDF matches what's on screen."""
        for pi in self._project_state.placed_items:
            page_rect = self._scene.get_placed_item_page_rect(pi.id)
            if page_rect:
                pi.page_rect = page_rect

            # Sync leader line endpoints
            if pi.item_type == "callout_circle":
                leader_end = self._scene.get_leader_end_pts(pi.id)
                if leader_end:
                    pi.leader_target_page_pos = leader_end

    # ── Panel refresh ─────────────────────────────────────────────────

    def _refresh_panels(self) -> None:
        """Refresh all panels from the current project state."""
        self._staging_tray.refresh()
        self._callout_panel.refresh()
        # Refresh Excel panel (preserves user-entered data)
        self._excel_panel.refresh()

    # ── Shutdown ──────────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        """Offer to save, then quit the side panel's Excel instance.

        Qt only delivers closeEvent to top-level widgets, so the embedded
        panel never hears about this on its own — and a reparented Excel
        outlives its parent process quite happily.
        """
        if not self._confirm_discard("closing"):
            event.ignore()
            return

        try:
            self._autosave_timer.stop()
        except Exception:  # noqa: BLE001
            pass

        # The canvas coalesces its level-of-detail work on a timer; one
        # left pending fires against a scene that is being torn down.
        try:
            self._canvas.shutdown()
        except Exception:  # noqa: BLE001
            pass

        # A worker still inside an ODA conversion or a component render
        # must be let finish: a QThread destroyed while running takes the
        # process down with it, which would turn a clean quit into the
        # sort of silent crash this release is about.
        from app.ui import background_task

        background_task.wait_for_all()

        try:
            self._excel_panel.shutdown()
        except Exception as e:  # noqa: BLE001 - never block the close
            self._log("warning", f"Side panel did not shut down cleanly: {e}")

        # The vector line art for this session's extracted views.  It has
        # been copied into any project that was saved, so what is left
        # here is scratch — and left behind it accumulates a folder of
        # PDFs in temp on every run.
        vector_dir = getattr(self, "_vector_dir", None)
        if vector_dir:
            import shutil

            shutil.rmtree(vector_dir, ignore_errors=True)
            self._vector_dir = None

        # Closing properly means the recovery copy is no longer news, so
        # the next launch must not offer it back.
        project_io.mark_session_closed()
        crash_guard.set_report_hook(None)
        crash_guard.get_logger().info("Session closed cleanly")

        super().closeEvent(event)

    # ── Accessors ─────────────────────────────────────────────────────

    @property
    def project_state(self) -> ProjectState:
        return self._project_state

    @property
    def canvas_scene(self) -> CanvasScene:
        return self._scene

    @property
    def canvas_view(self) -> CanvasView:
        return self._canvas
