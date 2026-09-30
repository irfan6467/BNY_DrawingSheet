"""
Phase 1: Internal template calibration utility.

Loads a template PDF, rasterizes it, and provides a UI to draw rectangles
defining slot regions. Exports the calibrated slot map to a JSON file.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import fitz  # PyMuPDF
from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QBrush, QColor, QPen, QPixmap, QImage, QPainter
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QGraphicsView, QGraphicsScene, QGraphicsRectItem,
    QGraphicsPixmapItem, QVBoxLayout, QHBoxLayout, QWidget, QPushButton,
    QListWidget, QListWidgetItem, QMessageBox, QLabel, QSpinBox, QFormLayout
)

# Add the project root to sys.path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.ui.theme import load_theme, THEME


class CalibrationScene(QGraphicsScene):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackgroundBrush(QBrush(QColor(THEME["bg_canvas"])))
        self.image_item = None
        self.current_rect_item = None
        
        self.drag_start = None
        self.is_drawing = False

        self.slot_rects = {}  # slot_id -> list of QRectF (most have 1, view_3d_row_2 has up to 3)
        self.rect_items = []  # list of all drawn QGraphicsRectItem for cleanup

        self.current_slot_id = None
        self.multi_rect_mode = False

    def set_image(self, pixmap):
        self.clear()
        self.rect_items.clear()
        self.image_item = self.addPixmap(pixmap)
        self.setSceneRect(QRectF(pixmap.rect()))
        
        # Re-draw existing rects
        self._redraw_all_rects()

    def set_current_slot(self, slot_id, multi_rect=False):
        self.current_slot_id = slot_id
        self.multi_rect_mode = multi_rect

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.current_slot_id:
            self.drag_start = event.scenePos()
            self.is_drawing = True
            self.current_rect_item = QGraphicsRectItem(QRectF(self.drag_start, self.drag_start))
            
            pen = QPen(QColor(THEME["accent_selection"]), 2, Qt.SolidLine)
            self.current_rect_item.setPen(pen)
            brush_color = QColor(THEME["accent_selection"])
            brush_color.setAlpha(60)
            self.current_rect_item.setBrush(QBrush(brush_color))
            
            self.addItem(self.current_rect_item)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.is_drawing and self.current_rect_item:
            current_pos = event.scenePos()
            rect = QRectF(self.drag_start, current_pos).normalized()
            self.current_rect_item.setRect(rect)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.is_drawing:
            self.is_drawing = False
            if self.current_rect_item:
                rect = self.current_rect_item.rect()
                if rect.width() > 10 and rect.height() > 10:
                    # Save rect
                    if self.current_slot_id not in self.slot_rects or not self.multi_rect_mode:
                        self.slot_rects[self.current_slot_id] = []
                    
                    if self.multi_rect_mode and len(self.slot_rects[self.current_slot_id]) >= 3:
                        # Max 3 for view_3d_row_2
                        QMessageBox.warning(None, "Limit Reached", "You can only draw 3 rectangles for this slot.")
                        self.removeItem(self.current_rect_item)
                    else:
                        self.slot_rects[self.current_slot_id].append(rect)
                        self.rect_items.append(self.current_rect_item)
                else:
                    self.removeItem(self.current_rect_item)
                
                self.current_rect_item = None
                self._redraw_all_rects()
                
        super().mouseReleaseEvent(event)

    def clear_current_slot(self):
        if self.current_slot_id in self.slot_rects:
            self.slot_rects.pop(self.current_slot_id)
            self._redraw_all_rects()

    def _redraw_all_rects(self):
        # Remove all
        for item in self.rect_items:
            if item.scene() == self:
                self.removeItem(item)
        self.rect_items.clear()
        
        # Redraw
        for slot_id, rects in self.slot_rects.items():
            for rect in rects:
                item = QGraphicsRectItem(rect)
                
                color = QColor(THEME["accent_primary"] if slot_id == self.current_slot_id else THEME["status_info"])
                
                pen = QPen(color, 2, Qt.SolidLine)
                item.setPen(pen)
                brush_color = QColor(color)
                brush_color.setAlpha(60 if slot_id == self.current_slot_id else 30)
                item.setBrush(QBrush(brush_color))
                
                # Add text label
                text = self.addText(slot_id)
                text.setDefaultTextColor(color)
                text.setPos(rect.topLeft())
                text.setParentItem(item)
                
                self.addItem(item)
                self.rect_items.append(item)


class CalibrationWindow(QMainWindow):
    def __init__(self, pdf_path):
        super().__init__()
        self.setWindowTitle("Template Calibrator")
        self.resize(1400, 900)
        
        self.pdf_path = pdf_path
        self.pdf_doc = fitz.open(pdf_path)
        self.pdf_page = self.pdf_doc[0]
        
        # PDF dimensions
        self.pdf_rect = self.pdf_page.rect
        self.pdf_w_pts = self.pdf_rect.width
        self.pdf_h_pts = self.pdf_rect.height
        
        # Render PDF to image
        self.dpi = 150
        zoom = self.dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = self.pdf_page.get_pixmap(matrix=matrix)
        
        # Convert fitz pixmap to QPixmap
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
        self.qpixmap = QPixmap.fromImage(img)
        
        # Conversion factor: pixel to points
        self.px_to_pt = 72.0 / self.dpi
        
        self._setup_ui()
        
        self.scene.set_image(self.qpixmap)

    def _setup_ui(self):
        main_widget = QWidget()
        layout = QHBoxLayout(main_widget)
        
        # Left Panel (Tools)
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_panel.setFixedWidth(300)
        
        self.slot_list = QListWidget()
        slots = [
            "section",
            "plan",
            "elevation",
            "view_3d_main_1",
            "view_3d_main_2",
            "view_3d_row_2 (Multi)",
            "callout_column"
        ]
        self.slot_list.addItems(slots)
        self.slot_list.currentItemChanged.connect(self._on_slot_selected)
        left_layout.addWidget(QLabel("Select slot to define:"))
        left_layout.addWidget(self.slot_list)
        
        # Callout config
        callout_config_layout = QFormLayout()
        self.max_details_spin = QSpinBox()
        self.max_details_spin.setRange(1, 20)
        self.max_details_spin.setValue(8)
        callout_config_layout.addRow("Max details:", self.max_details_spin)
        left_layout.addLayout(callout_config_layout)
        
        btn_clear = QPushButton("Clear Current Slot")
        btn_clear.clicked.connect(self._clear_current)
        left_layout.addWidget(btn_clear)
        
        left_layout.addStretch()
        
        btn_save = QPushButton("Save Calibration JSON")
        btn_save.setObjectName("primaryButton")
        btn_save.clicked.connect(self._save_json)
        left_layout.addWidget(btn_save)
        
        layout.addWidget(left_panel)
        
        # Right Panel (Canvas)
        self.scene = CalibrationScene()
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        layout.addWidget(self.view)
        
        self.setCentralWidget(main_widget)
        
    def _on_slot_selected(self, current, previous):
        if not current:
            return
        slot_name = current.text()
        slot_id = slot_name.split(" ")[0]
        multi_rect = "Multi" in slot_name
        
        self.scene.set_current_slot(slot_id, multi_rect)
        self.scene._redraw_all_rects()
        
    def _clear_current(self):
        self.scene.clear_current_slot()
        
    def _save_json(self):
        # Convert drawn rects to PDF points
        slots_data = {}
        callout_data = None
        
        for slot_id, rects in self.scene.slot_rects.items():
            converted_rects = []
            for r in rects:
                x = r.x() * self.px_to_pt
                y = r.y() * self.px_to_pt
                w = r.width() * self.px_to_pt
                h = r.height() * self.px_to_pt
                converted_rects.append({"x": x, "y": y, "w": w, "h": h})
            
            if slot_id == "callout_column":
                if converted_rects:
                    callout_data = converted_rects[0]
                    callout_data["max_details"] = self.max_details_spin.value()
            elif slot_id == "view_3d_row_2":
                slots_data[slot_id] = converted_rects
            else:
                if converted_rects:
                    slots_data[slot_id] = converted_rects[0]
                    
        # Check requirements
        if not callout_data:
            QMessageBox.warning(self, "Missing Region", "Please define the callout_column region.")
            return
            
        json_data = {
            "template_name": "bny_standard_a1",
            "base_pdf_path": os.path.relpath(self.pdf_path, start=Path(__file__).parent.parent),
            "paper_size": "A1",
            "orientation": "landscape" if self.pdf_w_pts > self.pdf_h_pts else "portrait",
            "slots": slots_data,
            "callout_column": callout_data
        }
        
        out_path = Path(__file__).parent.parent / "app" / "resources" / "templates" / "bny_standard_a1.json"
        
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2)
            
        QMessageBox.information(self, "Success", f"Calibration saved to:\n{out_path}")


def main():
    parser = argparse.ArgumentParser(description="Template Calibrator")
    parser.add_argument("--template-pdf", required=True, help="Path to the blank template PDF")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyleSheet(load_theme())
    
    win = CalibrationWindow(args.template_pdf)
    win.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
