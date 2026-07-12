import sys
from typing import List, Type, Any, get_args, get_origin, Union, Literal
from PySide6.QtWidgets import (
    QTableWidget, QTableWidgetItem, QDialog, QVBoxLayout, QFormLayout,
    QDialogButtonBox, QHeaderView, QMessageBox, QAbstractItemView, QMenu,
    QApplication, QLineEdit, QCheckBox, QComboBox, QLabel, QScrollArea, QWidget,
    QToolBar, QStyle
)
from PySide6.QtCore import Qt, QPoint, QUrl
from PySide6.QtGui import QAction, QCursor, QKeySequence, QImage, QPixmap

from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings

from pathlib import Path
from pydantic import BaseModel

# Try importing PyMuPDF (fitz)

import fitz


class ExcelTableWidget(QTableWidget):
    """
    A QTableWidget extended to support Ctrl+C (Copy), Ctrl+V (Paste), 
    and Delete keys, behaving like Excel.
    """
    def __init__(self, rows=10, columns=3):
        super().__init__(rows, columns)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)

    def keyPressEvent(self, event):
        # Fix: Use QKeySequence.Copy/Paste instead of Qt.Key_Copy/Paste for .matches()
        if event.matches(QKeySequence.Copy) or (event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_C):
            self.copy_selection()
        elif event.matches(QKeySequence.Paste) or (event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_V):
            self.paste_selection()
        elif event.key() == Qt.Key_Delete:
            self.delete_selection()
        else:
            super().keyPressEvent(event)

    def copy_selection(self):
        selection = self.selectedRanges()
        if not selection:
            return
            
        # For simplicity, we only copy the first selection range
        r = selection[0]
        rows = range(r.topRow(), r.bottomRow() + 1)
        columns = range(r.leftColumn(), r.rightColumn() + 1)
        
        s = ""
        for i in rows:
            for j in columns:
                try:
                    text = self.item(i, j).text()
                except AttributeError:
                    text = ""
                s += text + "\t"
            s = s[:-1] + "\n" # Remove trailing tab, add newline
            
        QApplication.clipboard().setText(s)

    def paste_selection(self):
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        if not text:
            return

        rows_text = text.split('\n')
        # Remove empty trailing line from split
        if rows_text and rows_text[-1] == '':
            rows_text.pop()
            
        current_row = self.currentRow()
        current_col = self.currentColumn()
        
        if current_row < 0: current_row = 0
        if current_col < 0: current_col = 0

        # Expand table if paste goes beyond current limits
        required_rows = current_row + len(rows_text)
        if required_rows > self.rowCount():
            self.setRowCount(required_rows)

        for i, row_text in enumerate(rows_text):
            columns_text = row_text.split('\t')
            
            # Expand columns if needed
            required_cols = current_col + len(columns_text)
            if required_cols > self.columnCount():
                self.setColumnCount(required_cols)

            for j, cell_text in enumerate(columns_text):
                self.setItem(current_row + i, current_col + j, QTableWidgetItem(cell_text))

    def delete_selection(self):
        for item in self.selectedItems():
            item.setText("")

    def show_context_menu(self, pos: QPoint):
        menu = QMenu()
        add_row = QAction("Add Row", self)
        add_row.triggered.connect(lambda: self.insertRow(self.rowCount()))
        
        remove_row = QAction("Remove Current Row", self)
        remove_row.triggered.connect(lambda: self.removeRow(self.currentRow()))
        
        clear_table = QAction("Clear All", self)
        clear_table.triggered.connect(lambda: self.setRowCount(0))
        
        menu.addAction(add_row)
        menu.addAction(remove_row)
        menu.addSeparator()
        menu.addAction(clear_table)
        menu.exec(QCursor.pos())


class ReadTabularDialog(QDialog):
    """
    A dialog that inspects a Pydantic model, creates table columns 
    matching the fields, and allows the user to paste data.
    """
    def __init__(self, parent=None, pydantic_model: Type[BaseModel] = None, title="Enter Data"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(800, 400)
        
        self.layout = QVBoxLayout(self)
        
        # 1. Introspect Pydantic Model to get headers
        # Filter out internal fields (starting with _) or auto-generated ones like UniqueID
        self.field_names = []
        for name, field in pydantic_model.model_fields.items():
            if name not in ['UniqueID', 'Status', 'type', 'Visible']:
                self.field_names.append(name)
        
        # 2. Setup Table
        self.table = ExcelTableWidget(rows=5, columns=len(self.field_names))
        self.table.setHorizontalHeaderLabels(self.field_names)
        
        # Allow interactive resizing of columns
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        
        self.layout.addWidget(self.table)
        
        # 3. Buttons
        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.layout.addWidget(self.button_box)

    def get_data(self) -> List[dict]:
        """Scrapes the table and returns a list of dictionaries."""
        data_list = []
        rows = self.table.rowCount()
        cols = self.table.columnCount()
        
        for r in range(rows):
            row_data = {}
            is_empty = True
            
            for c in range(cols):
                item = self.table.item(r, c)
                header = self.field_names[c]
                value = item.text() if item else ""
                
                # Check if value is not just empty whitespace
                if value.strip():
                    is_empty = False
                    row_data[header] = value
            
            # Only add the row if at least one cell has data
            if not is_empty:
                data_list.append(row_data)
                
        return data_list


class PDFViewerWidget(QWidget):
    """
    A lightweight, embedded PDF viewer using PyMuPDF (fitz).
    Renders all PDF pages in a continuous vertical scroll view.
    """
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window)
        
        self.doc = None
        self.zoom_level = 1.3  # Set a reasonable default zoom for readability
        
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Toolbar ---
        self.toolbar = QToolBar()
        self.toolbar.setStyleSheet("QToolBar { background: #f0f0f0; border-bottom: 1px solid #ccc; }")
        layout.addWidget(self.toolbar)

        # Zoom Actions
        self.act_zoom_out = QAction("Zoom Out", self)
        self.act_zoom_out.triggered.connect(self.zoom_out)
        self.toolbar.addAction(self.act_zoom_out)

        self.act_zoom_in = QAction("Zoom In", self)
        self.act_zoom_in.triggered.connect(self.zoom_in)
        self.toolbar.addAction(self.act_zoom_in)

        # --- Scroll Area for Continuous Page Display ---
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.scroll_area.setStyleSheet("background-color: #555;") # Dark background for contrast
        
        # Container widget to hold the vertical layout of pages
        self.pages_container = QWidget()
        self.pages_container.setStyleSheet("background-color: transparent;")
        
        # Layout for the pages (Vertical Stack)
        self.pages_layout = QVBoxLayout(self.pages_container)
        self.pages_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.pages_layout.setSpacing(20) # Visual gap between pages
        self.pages_layout.setContentsMargins(30, 30, 30, 30)
        
        self.scroll_area.setWidget(self.pages_container)
        layout.addWidget(self.scroll_area)

    def load_file(self, file_path: str | Path):
        """Load a PDF file."""
        path = Path(file_path)
        if not path.exists():
            self._show_error(f"File not found: {path.name}")
            return

        try:
            self.doc = fitz.open(str(path))
            self.setWindowTitle(path.name)
            self._render_all_pages()
        except Exception as e:
            self._show_error(f"Failed to open PDF:\n{e}")

    def _render_all_pages(self):
        """Render all pages to the layout."""
        if not self.doc:
            return

        # 1. Clear existing pages from layout
        while self.pages_layout.count():
            item = self.pages_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 2. Render all pages
        mat = fitz.Matrix(self.zoom_level, self.zoom_level)
        
        for page in self.doc:
            pix = page.get_pixmap(matrix=mat)
            
            # Convert to QImage
            img_format = QImage.Format.Format_RGB888
            if pix.alpha:
                img_format = QImage.Format.Format_RGBA8888
            
            qimg = QImage(pix.samples, pix.width, pix.height, pix.stride, img_format)
            
            # Create Label
            lbl = QLabel()
            lbl.setPixmap(QPixmap.fromImage(qimg))
            lbl.setStyleSheet("border: 1px solid black; background-color: white;")
            
            # Add to vertical layout
            self.pages_layout.addWidget(lbl)

    def zoom_in(self):
        self.zoom_level += 0.2
        self._render_all_pages()

    def zoom_out(self):
        if self.zoom_level > 0.4:
            self.zoom_level -= 0.2
            self._render_all_pages()

    def _show_error(self, msg):
        # Create a temp label in the scroll area to show error
        while self.pages_layout.count():
            item = self.pages_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
        err_lbl = QLabel(msg)
        err_lbl.setStyleSheet("color: red; background: white; padding: 20px; font-size: 14px;")
        self.pages_layout.addWidget(err_lbl)