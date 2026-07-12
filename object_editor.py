"""Object data-entry: the field widgets, the list/matrix grid editor, and the
ObjectEditorDialog that assembles them for adding/editing a class's objects."""
import os
import ast
import shutil
import sqlite3
import qtawesome as qta
from PySide6.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QScrollArea,
    QLabel, QLineEdit, QPushButton, QCheckBox, QComboBox, QTextEdit,
    QDateTimeEdit, QListWidget, QListWidgetItem, QFileDialog, QMessageBox,
    QTableWidgetItem, QHeaderView, QDialogButtonBox,
)
from PySide6.QtCore import Qt, QDateTime, QRegularExpression, QUrl
from PySide6.QtGui import QRegularExpressionValidator, QDesktopServices

from core import (
    get_app_icon, sanitize_name, qid,
    files_dir_for, make_stored_filename, display_file_name,
    resolve_file_path, trash_stored_file,
)
from custom_widgets import ExcelTableWidget


class NullableDateTimeEdit(QWidget):
    """A date/time editor that can represent 'no value' (NULL).

    A checkbox toggles whether a date is set. New objects default to checked
    with the current date/time; unchecking stores NULL.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.check = QCheckBox()
        self.check.setToolTip("Tick to set a date/time, untick for no date (empty / NULL).")
        self.edit = QDateTimeEdit()
        self.edit.setCalendarPopup(True)
        self.edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.edit.setDateTime(QDateTime.currentDateTime())

        lay.addWidget(self.check)
        lay.addWidget(self.edit, 1)

        self.check.toggled.connect(self.edit.setEnabled)
        self.check.setChecked(True)
        self.edit.setEnabled(True)

    def value(self):
        """Return the formatted string, or None when no date is set."""
        if not self.check.isChecked():
            return None
        return self.edit.dateTime().toString("yyyy-MM-dd HH:mm:ss")

    def set_value(self, val):
        if val is None or str(val).strip() == "":
            self.check.setChecked(False)
            self.edit.setEnabled(False)
        else:
            dt = QDateTime.fromString(str(val), "yyyy-MM-dd HH:mm:ss")
            if dt.isValid():
                self.edit.setDateTime(dt)
            self.check.setChecked(True)
            self.edit.setEnabled(True)


class FileAttributeWidget(QWidget):
    """Attach / open / clear a single file for a 'file' attribute.

    Keeps the existing stored reference until the user picks a new file or clears it.
    The actual copy into storage happens at save time (transaction-safe), via plan().
    """
    def __init__(self, db_path, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.current = None      # existing stored reference ("<uuid>__name") or None
        self.pending = None      # absolute path of a newly chosen file, or None
        self.cleared = False     # user explicitly cleared an existing file

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.label = QLabel()
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.btn_choose = QPushButton(" Choose...")
        self.btn_choose.setIcon(qta.icon('fa5s.folder-open'))
        self.btn_choose.clicked.connect(self._choose)

        self.btn_open = QPushButton()
        self.btn_open.setIcon(qta.icon('fa5s.external-link-alt'))
        self.btn_open.setToolTip("Open the current file")
        self.btn_open.setFixedWidth(32)
        self.btn_open.clicked.connect(self._open)

        self.btn_clear = QPushButton()
        self.btn_clear.setIcon(qta.icon('fa5s.times', color='#ff4c4c'))
        self.btn_clear.setToolTip("Remove the attached file")
        self.btn_clear.setFixedWidth(32)
        self.btn_clear.clicked.connect(self._clear)

        lay.addWidget(self.label, 1)
        lay.addWidget(self.btn_choose)
        lay.addWidget(self.btn_open)
        lay.addWidget(self.btn_clear)
        self._refresh()

    def set_existing(self, stored_value):
        self.current = stored_value or None
        self.pending = None
        self.cleared = False
        self._refresh()

    def _refresh(self):
        if self.pending:
            self.label.setText(f"<b>{os.path.basename(self.pending)}</b> <span style='color:#888;'>(new)</span>")
            self.btn_open.setEnabled(True)
            self.btn_clear.setEnabled(True)
            return
        if self.current and not self.cleared:
            name = display_file_name(self.current)
            path = resolve_file_path(self.db_path, self.current)
            if path and os.path.exists(path):
                self.label.setText(name)
            else:
                self.label.setText(f"{name} <span style='color:#cc8800;'>&#9888; missing</span>")
                self.label.setToolTip("The stored file is no longer in the files folder.")
            self.btn_open.setEnabled(True)
            self.btn_clear.setEnabled(True)
            return
        self.label.setText("<span style='color:#888;'>No file</span>")
        self.btn_open.setEnabled(False)
        self.btn_clear.setEnabled(False)

    def _choose(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file to attach", "", "All Files (*.*)")
        if path:
            self.pending = path
            self.cleared = False
            self._refresh()

    def _clear(self):
        self.pending = None
        self.cleared = True
        self._refresh()

    def _open(self):
        target = self.pending or (resolve_file_path(self.db_path, self.current) if (self.current and not self.cleared) else None)
        if target and os.path.exists(target):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(target)))
        else:
            QMessageBox.warning(self, "File Not Found", "The file could not be found on disk.")

    def is_empty(self):
        if self.pending:
            return False
        return self.cleared or not self.current

    def plan(self):
        """Return the save action: ('new', src, new_stored, old) | ('clear', old) | ('keep', current)."""
        if self.pending:
            new_stored = make_stored_filename(self.pending)
            return ('new', self.pending, new_stored, self.current)
        if self.cleared:
            return ('clear', self.current)
        return ('keep', self.current)


class GridInputDialog(QDialog):
    """Grid editor for a 'list' (single column) or 'matrix' (named columns).

    - list   -> flat Python list  ['A', 'B', 1]
    - matrix -> column-major list-of-lists  [[col0 rows...], [col1 rows...]]
                (exactly the shape the object editor already stores/validates)
    """
    def __init__(self, parent, kind, col_names=None, initial=None):
        super().__init__(parent)
        self.kind = kind  # 'list' | 'matrix'
        headers = ["Value"] if kind == "list" else list(col_names or [])
        self.ncols = max(1, len(headers))

        self.setWindowTitle("Edit List" if kind == "list" else "Edit Matrix")
        self.setWindowIcon(get_app_icon())
        self.resize(540, 440)

        layout = QVBoxLayout(self)
        if kind == "matrix":
            layout.addWidget(QLabel(
                f"Each row is one record across the {self.ncols} column(s). "
                "Completely empty rows are ignored."))
        else:
            layout.addWidget(QLabel("One value per row. Empty rows are ignored."))

        self.table = ExcelTableWidget(rows=0, columns=self.ncols)
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        self._seed(initial)

        hint = QLabel("Paste straight from Excel with Ctrl+V. Right-click for row options.")
        hint.setStyleSheet("color:#888;")
        layout.addWidget(hint)

        bottom = QHBoxLayout()
        btn_add = QPushButton(" Add Row")
        btn_add.setIcon(qta.icon('fa5s.plus'))
        btn_add.clicked.connect(lambda: self.table.insertRow(self.table.rowCount()))
        bottom.addWidget(btn_add)
        bottom.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bottom.addWidget(bb)
        layout.addLayout(bottom)

    def _seed(self, initial):
        if self.kind == "list":
            vals = initial if isinstance(initial, list) else []
            self.table.setRowCount(max(len(vals) + 1, 3))
            for r, v in enumerate(vals):
                self.table.setItem(r, 0, QTableWidgetItem("" if v is None else str(v)))
        else:
            # initial is column-major: cols[c] holds the rows of column c
            cols = initial if isinstance(initial, list) else []
            nrows = max((len(c) for c in cols if isinstance(c, list)), default=0)
            self.table.setRowCount(max(nrows + 1, 3))
            for c in range(self.ncols):
                inner = cols[c] if (c < len(cols) and isinstance(cols[c], list)) else []
                for r, v in enumerate(inner):
                    self.table.setItem(r, c, QTableWidgetItem("" if v is None else str(v)))

    @staticmethod
    def _coerce(text):
        """Interpret a cell like a spreadsheet: int, else float, else text; blank -> None."""
        s = (text or "").strip()
        if s == "":
            return None
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            return s

    def get_value(self):
        t = self.table
        if self.kind == "list":
            out = []
            for r in range(t.rowCount()):
                it = t.item(r, 0)
                v = self._coerce(it.text() if it else "")
                if v is not None:
                    out.append(v)
            return out
        # matrix: keep a row if any of its (first ncols) cells has a value, then
        # transpose to the column-major shape the editor stores. Reading only the
        # first ncols ignores any extra columns a wide paste may have added.
        included = []
        for r in range(t.rowCount()):
            cells, any_val = [], False
            for c in range(self.ncols):
                it = t.item(r, c)
                cv = self._coerce(it.text() if it else "")
                if cv is not None:
                    any_val = True
                cells.append(cv)
            if any_val:
                included.append(cells)
        return [[row[c] for row in included] for c in range(self.ncols)]


class ObjectEditorDialog(QDialog):
    def __init__(self, db_path, class_id, class_name, table_name, obj_id=None, parent=None):
        super().__init__(parent)
        self.obj_id = obj_id
        mode_text = "Edit" if obj_id else "Add New"
        self.setWindowTitle(f"{mode_text} {class_name}")
        self.setWindowIcon(get_app_icon())
        self.resize(500, 550) 
        
        self.db_path = db_path
        self.class_id = class_id
        self.table_name = table_name
        self.input_widgets = {} 
        self.rel_widgets = {}   
        self.attr_app_types = {} 
        self.attr_constraints = {}
        self.matrix_col_counts = {}
        self.matrix_col_names = {}  # safe_col -> [column names], for the grid editor

        layout = QVBoxLayout(self)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        
        scroll_widget = QWidget()
        form_layout = QFormLayout(scroll_widget)
        form_layout.setContentsMargins(10, 10, 10, 10)
        form_layout.setSpacing(15)
        
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            
            cur.execute("SELECT id, name, data_type, is_unique, is_required, lookup_query FROM attributes WHERE class_id = ? ORDER BY row_order", (self.class_id,))
            attributes = cur.fetchall()

            for attr_id, attr_name, attr_type, is_unique, is_required, lookup_query in attributes:
                safe_col_name = sanitize_name(attr_name)
                
                if attr_type == "look-through": continue
                    
                self.attr_app_types[safe_col_name] = attr_type
                self.attr_constraints[safe_col_name] = {
                    'name': attr_name,
                    'unique': bool(is_unique),
                    'required': bool(is_required)
                }
                
                if attr_type == "int":
                    # Plain text + validator instead of QSpinBox: QSpinBox is limited to
                    # 32-bit and silently clamps, whereas SQLite INTEGER is 64-bit. Parsing
                    # in Python keeps the full range with no silent data loss.
                    widget = QLineEdit()
                    widget.setValidator(QRegularExpressionValidator(QRegularExpression(r'^-?\d*$')))
                    widget.setPlaceholderText("Whole number")
                elif attr_type == "float":
                    # Likewise avoids QDoubleSpinBox's range cap and 4-decimal rounding.
                    widget = QLineEdit()
                    widget.setValidator(QRegularExpressionValidator(QRegularExpression(r'^-?\d*\.?\d*([eE][-+]?\d+)?$')))
                    widget.setPlaceholderText("Number")
                elif attr_type == "boolean":
                    widget = QCheckBox("Yes / True")
                elif attr_type == "date":
                    widget = NullableDateTimeEdit()
                elif attr_type == "file":
                    widget = FileAttributeWidget(self.db_path)
                elif attr_type == "discrete":
                    widget = QComboBox()
                    widget.addItem("(none)", None)  # blank choice; required check rejects it
                    try:
                        type_id = int(lookup_query)
                        for (opt,) in cur.execute("SELECT value FROM discrete_options WHERE type_id = ? ORDER BY row_order", (type_id,)).fetchall():
                            widget.addItem(opt, opt)
                    except (ValueError, TypeError, sqlite3.OperationalError):
                        pass
                elif attr_type == "long string":
                    widget = QTextEdit()
                    widget.setMaximumHeight(100) 
                elif attr_type in ("list", "matrix"):
                    widget = QLineEdit("[]")
                    if attr_type == "matrix":
                        cur.execute("SELECT column_name FROM matrix_columns WHERE attribute_id = ? ORDER BY column_index", (attr_id,))
                        names = [r[0] for r in cur.fetchall()]
                        self.matrix_col_counts[safe_col_name] = len(names)
                        self.matrix_col_names[safe_col_name] = names
                        widget.setToolTip(f"Provide exactly {len(names)} list(s) inside the main list, e.g. [[1,2], [3,4]] — or use the Grid button.")
                    else:
                        widget.setToolTip("A Python list, e.g. ['A', 'B'] or [1, 2] — or use the Grid button.")
                else:
                    widget = QLineEdit()

                self.input_widgets[safe_col_name] = widget
                label_text = attr_name + (" <span style='color:red;'>*</span>" if is_required else "") + ":"
                if attr_type in ("list", "matrix"):
                    # Keep the line edit as the source of truth (save/validation unchanged);
                    # the Grid button just fills it from a spreadsheet-style dialog.
                    row_wrap = QWidget()
                    row_hl = QHBoxLayout(row_wrap)
                    row_hl.setContentsMargins(0, 0, 0, 0)
                    row_hl.setSpacing(6)
                    row_hl.addWidget(widget, 1)
                    btn_grid = QPushButton(" Grid")
                    btn_grid.setIcon(qta.icon('fa5s.table'))
                    btn_grid.setToolTip("Enter values in a spreadsheet-style grid")
                    grid_names = self.matrix_col_names.get(safe_col_name) if attr_type == "matrix" else None
                    btn_grid.clicked.connect(
                        lambda _=False, w=widget, k=attr_type, nm=grid_names: self._open_grid_editor(w, k, nm))
                    row_hl.addWidget(btn_grid)
                    form_layout.addRow(QLabel(label_text), row_wrap)
                else:
                    form_layout.addRow(QLabel(label_text), widget)

            cur.execute("""
                SELECT c.id, c.name, r.rel_type, r.is_required
                FROM relationships r JOIN classes c ON r.target_class = c.id
                WHERE r.source_class = ? ORDER BY r.row_order
            """, (self.class_id,))
            relationships = cur.fetchall()

            for target_class_id, target_name, rel_type, rel_required in relationships:
                safe_target_name = sanitize_name(target_name)
                junc_table = f"rel_{self.table_name}_to_objects_{safe_target_name}"
                
                rel_container = QVBoxLayout()
                rel_container.setContentsMargins(0, 0, 0, 0)
                rel_container.setSpacing(5)
                
                search_box = QLineEdit()
                search_box.setPlaceholderText(f"Search {target_name}...")
                rel_container.addWidget(search_box)
                
                list_widget = QListWidget()
                # Strong selection highlight — the default (esp. when the list is not
                # focused) is a very light gray that's hard to see.
                list_widget.setStyleSheet(
                    "QListWidget::item:selected, QListWidget::item:selected:!active"
                    " { background:#2d7dd2; color:white; }")
                if rel_type == "one_to_many":
                    list_widget.setSelectionMode(QListWidget.SingleSelection)
                else:
                    list_widget.setSelectionMode(QListWidget.MultiSelection)
                list_widget.setMinimumHeight(120)
                
                cur.execute("SELECT name FROM attributes WHERE class_id = ? AND is_title = 1 ORDER BY row_order", (target_class_id,))
                title_rows = cur.fetchall()
                
                try:
                    if title_rows:
                        title_cols = [r[0] for r in title_rows] 
                        escaped_cols = ", ".join([qid(c) for c in title_cols])
                        cur.execute(f"SELECT [ID], {escaped_cols} FROM {qid('base_view_objects_' + safe_target_name)}")
                        
                        for row in cur.fetchall():
                            t_id = row[0]
                            display_vals = ["None" if val is None or str(val).strip() == "" else str(val) for val in row[1:]]
                            combined_titles = " --- ".join(display_vals)
                            display_text = f"{combined_titles} (ID: {t_id})"
                                
                            item = QListWidgetItem(display_text)
                            item.setData(Qt.UserRole, t_id)
                            item.setData(Qt.UserRole + 1, combined_titles.lower())
                            list_widget.addItem(item)
                    else:
                        cur.execute(f"SELECT [ID] FROM {qid('base_view_objects_' + safe_target_name)}")
                        for row in cur.fetchall():
                            t_id = row[0]
                            item = QListWidgetItem(f"{target_name} #{t_id}")
                            item.setData(Qt.UserRole, t_id)
                            item.setData(Qt.UserRole + 1, target_name.lower()) 
                            list_widget.addItem(item)
                except sqlite3.OperationalError:
                    pass 
                    
                rel_container.addWidget(list_widget)
                search_box.textChanged.connect(lambda text, lw=list_widget: self.filter_list_items(text, lw))
                    
                self.rel_widgets[junc_table] = {"widget": list_widget, "rel_type": rel_type,
                                                "target_name": target_name, "required": bool(rel_required)}
                rel_label = QLabel(f"Rel: {target_name}" + (" <span style='color:red;'>*</span>" if rel_required else "") + ":")
                form_layout.addRow(rel_label, rel_container)

            if self.obj_id:
                cols = list(self.input_widgets.keys())
                if cols:
                    escaped_cols = [qid(c) for c in cols]
                    cur.execute(f"SELECT {', '.join(escaped_cols)} FROM {qid(self.table_name)} WHERE id = ?", (self.obj_id,))
                    row = cur.fetchone()
                    if row:
                        for idx, col in enumerate(cols):
                            val = row[idx]
                            widget = self.input_widgets[col]

                            if isinstance(widget, NullableDateTimeEdit):
                                # Always call (even for None) so a stored NULL unticks the box.
                                widget.set_value(val)
                            elif isinstance(widget, FileAttributeWidget):
                                widget.set_existing(val)
                            elif isinstance(widget, QComboBox):  # discrete
                                pos = widget.findData(val) if val is not None else 0
                                if pos < 0:
                                    # Value no longer in the option set: show it so we don't lose it.
                                    widget.addItem(str(val), val)
                                    pos = widget.count() - 1
                                widget.setCurrentIndex(pos)
                            elif val is not None:
                                if isinstance(widget, QCheckBox):
                                    widget.setChecked(bool(val))
                                elif isinstance(widget, QTextEdit):
                                    widget.setPlainText(str(val))
                                else:
                                    widget.setText(str(val))
                                    
                for junc_table, data in self.rel_widgets.items():
                    cur.execute(f"SELECT target_id FROM {qid(junc_table)} WHERE source_id = ?", (self.obj_id,))
                    existing_targets = [r[0] for r in cur.fetchall()]
                    lw = data["widget"]
                    for i in range(lw.count()):
                        item = lw.item(i)
                        if item.data(Qt.UserRole) in existing_targets: item.setSelected(True)

        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)

        btn_layout = QHBoxLayout()
        btn_save = QPushButton(" Save Object")
        btn_save.setIcon(qta.icon('fa5s.save'))
        btn_save.clicked.connect(self.save_record)
        
        btn_cancel = QPushButton(" Cancel")
        btn_cancel.setIcon(qta.icon('fa5s.times', color='#ff4c4c'))
        btn_cancel.clicked.connect(self.reject)
        
        btn_layout.addStretch() 
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_save)
        layout.addLayout(btn_layout)

    def filter_list_items(self, search_text, list_widget):
        search_text = search_text.lower()
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            search_target = item.data(Qt.UserRole + 1)
            if search_target is None: search_target = item.text().lower() 
            item.setHidden(search_text not in search_target)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter: return
        super().keyPressEvent(event)

    def _open_grid_editor(self, line_edit, kind, col_names):
        """Open the spreadsheet grid seeded from the field's current text; write back on OK."""
        raw = line_edit.text().strip()
        initial = None
        if raw:
            try:
                parsed = ast.literal_eval(raw)
                if isinstance(parsed, list):
                    initial = parsed
            except (ValueError, SyntaxError):
                initial = None  # unparseable text -> start from a blank grid
        dlg = GridInputDialog(self, kind, col_names, initial)
        if dlg.exec():
            line_edit.setText(str(dlg.get_value()))

    def save_record(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA foreign_keys = 1")
                cur = conn.cursor()
                
                columns = []
                values = []
                placeholders = []
                file_copies = []      # (source_path, stored_name) to copy into storage on commit
                files_to_trash = []   # old stored names to trash after a successful commit

                for col_name, widget in self.input_widgets.items():
                    app_type = self.attr_app_types.get(col_name)
                    constraints = self.attr_constraints.get(col_name)
                    attr_display_name = constraints['name']

                    if isinstance(widget, FileAttributeWidget):
                        action = widget.plan()
                        is_empty = widget.is_empty()
                        if action[0] == 'new':
                            _, src, new_stored, old = action
                            val = new_stored
                            file_copies.append((src, new_stored))
                            if old:
                                files_to_trash.append(old)
                        elif action[0] == 'clear':
                            val = None
                            if action[1]:
                                files_to_trash.append(action[1])
                        else:  # keep
                            val = action[1]
                    elif isinstance(widget, QComboBox):  # discrete attribute
                        val = widget.currentData()
                        is_empty = (val is None)
                    elif isinstance(widget, QCheckBox):
                        val = 1 if widget.isChecked() else 0
                        is_empty = False
                    elif isinstance(widget, NullableDateTimeEdit):
                        val = widget.value()
                        is_empty = (val is None)
                    elif isinstance(widget, QTextEdit):
                        raw_text = widget.toPlainText().strip()
                        is_empty = (raw_text == "")
                        val = None if is_empty else raw_text
                    else:
                        raw_text = widget.text().strip()
                        is_empty = (raw_text == "")
                        if is_empty:
                            val = None
                        else:
                            if app_type in ("list", "matrix"):
                                try:
                                    parsed = ast.literal_eval(raw_text)
                                    if not isinstance(parsed, list): raise ValueError("Not a list.")

                                    if app_type == "matrix":
                                        expected_cols = self.matrix_col_counts.get(col_name, 0)
                                        if len(parsed) != expected_cols:
                                            raise ValueError(f"Matrix expects exactly {expected_cols} column list(s) inside the main list.")
                                        for inner in parsed:
                                            if not isinstance(inner, list): raise ValueError("Each matrix column must be a list.")
                                    val = str(parsed)
                                except Exception as e:
                                    err_msg = str(e) if str(e) else "Invalid Python syntax."
                                    QMessageBox.warning(self, "Validation Error", f"'{attr_display_name}' parsing failed: {err_msg}\n\nExample: [['A', 'B'], [1, 2]]")
                                    return
                            elif app_type == "int":
                                try:
                                    val = int(raw_text)
                                except ValueError:
                                    QMessageBox.warning(self, "Validation Error", f"'{attr_display_name}' must be a whole number.")
                                    return
                            elif app_type == "float":
                                try:
                                    val = float(raw_text)
                                except ValueError:
                                    QMessageBox.warning(self, "Validation Error", f"'{attr_display_name}' must be a number.")
                                    return
                            else: val = raw_text

                    if constraints['required'] and is_empty:
                        QMessageBox.warning(self, "Validation Error", f"'{attr_display_name}' is a required field.")
                        return

                    if constraints['unique'] and not is_empty:
                        if self.obj_id:
                            cur.execute(f"SELECT id FROM {qid(self.table_name)} WHERE {qid(col_name)} = ? AND id != ?", (val, self.obj_id))
                        else:
                            cur.execute(f"SELECT id FROM {qid(self.table_name)} WHERE {qid(col_name)} = ?", (val,))
                            
                        if cur.fetchone():
                            QMessageBox.warning(self, "Validation Error", f"'{attr_display_name}' must be unique. The value '{val}' already exists.")
                            return

                    columns.append(col_name)
                    placeholders.append("?")
                    values.append(val)

                # Required relationships: at least one linked target must be selected.
                for data in self.rel_widgets.values():
                    if data.get("required") and not data["widget"].selectedItems():
                        QMessageBox.warning(self, "Validation Error",
                            f"'{data['target_name']}' is a required relationship — please link at least one.")
                        return

                copied_abs = []
                try:
                    # Copy new attachments into storage first; if the DB write fails we
                    # delete them again so nothing is stranded.
                    if file_copies:
                        files_dir = files_dir_for(self.db_path, create=True)
                        for src, dest_name in file_copies:
                            dest_abs = os.path.join(files_dir, dest_name)
                            shutil.copy2(src, dest_abs)
                            copied_abs.append(dest_abs)

                    conn.execute("BEGIN IMMEDIATE")
                    active_obj_id = self.obj_id

                    if columns:
                        if self.obj_id:
                            set_clause = ", ".join([f"{qid(c)} = ?" for c in columns])
                            query = f"UPDATE {qid(self.table_name)} SET {set_clause} WHERE id = ?"
                            cur.execute(query, tuple(values) + (self.obj_id,))
                        else:
                            escaped_columns = [qid(c) for c in columns]
                            query = f"INSERT INTO {qid(self.table_name)} ({', '.join(escaped_columns)}) VALUES ({', '.join(placeholders)})"
                            cur.execute(query, tuple(values))
                            active_obj_id = cur.lastrowid
                    else:
                        if not self.obj_id:
                            cur.execute(f"INSERT INTO {qid(self.table_name)} DEFAULT VALUES")
                            active_obj_id = cur.lastrowid
                        
                    for junc_table, data in self.rel_widgets.items():
                        if self.obj_id:
                            cur.execute(f"DELETE FROM {qid(junc_table)} WHERE source_id = ?", (active_obj_id,))
                        selected_ids = [int(item.data(Qt.UserRole)) for item in data["widget"].selectedItems()]
                        for target_id in selected_ids:
                            cur.execute(f"INSERT INTO {qid(junc_table)} (source_id, target_id) VALUES (?, ?)", (active_obj_id, target_id))
                    
                    conn.commit()
                    # Commit succeeded: retire replaced/cleared files to the trash folder.
                    for old in files_to_trash:
                        trash_stored_file(self.db_path, old)
                    self.accept()
                except Exception as e:
                    conn.rollback()
                    for path in copied_abs:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                    raise e

        except Exception as e:
            QMessageBox.critical(self, "Database Error", str(e))
