import sys
import sqlite3
import os
import csv
import ast
import datetime
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QTreeWidget, QTreeWidgetItem, QPushButton, QLineEdit, QLabel, QTableView, 
    QHeaderView, QMessageBox, QFileDialog, QListWidget, QListWidgetItem, QDialog,
    QSplitter, QScrollArea, QComboBox, QInputDialog, QFormLayout, QSpinBox, 
    QDoubleSpinBox, QTextEdit, QDateTimeEdit, QCheckBox
)
from PySide6.QtCore import Qt, QSettings, QDateTime
from PySide6.QtSql import QSqlDatabase, QSqlQueryModel, QSqlQuery
from PySide6.QtGui import QAction, QFontDatabase, QFont

from class_builder_dialog import ClassBuilderDialog, init_db, get_app_icon

# ==========================================
# PHYSICAL SCHEMA SYNCHRONIZATION
# ==========================================
def sync_physical_table(db_path, class_id, class_name, parent_widget=None):
    """
    Reads the metadata schema from Class Builder and physically creates/updates 
    the actual SQLite tables (e.g. objects_materials).
    Also generates a massive SQL View that joins all the junction tables 
    so you can view relationships easily.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    safe_table_name = f"objects_{class_name.replace(' ', '_').lower()}"
    
    # 1. FETCH METADATA
    cur.execute("SELECT name, data_type, show_in_table, is_title FROM attributes WHERE class_id = ? ORDER BY row_order", (class_id,))
    attributes = cur.fetchall()
    
    required_cols = []
    attr_app_types = {}
    
    select_clause = ["m.id AS [ID]"]
    
    for attr_name, attr_type, show_in_table, is_title in attributes:
        safe_col_name = attr_name.replace(' ', '_').lower()
        
        # MAP APP DATA TYPES TO SQLITE DATA TYPES
        if attr_type in ("int", "boolean"):
            sql_type = "INTEGER"
        elif attr_type == "float":
            sql_type = "REAL"
        else:
            sql_type = "TEXT"
            
        required_cols.append((safe_col_name, sql_type))
        attr_app_types[safe_col_name] = attr_type
        
        if show_in_table:
            select_clause.append(f"m.[{safe_col_name}] AS [{attr_name}]")

    # 2. CREATE OR UPDATE PRIMARY TABLE
    cur.execute(f"SELECT count(name) FROM sqlite_master WHERE type='table' AND name='{safe_table_name}'")
    if cur.fetchone()[0] == 0:
        cols_def = ", ".join([f"[{c}] {t}" for c, t in required_cols])
        if cols_def:
            cur.execute(f"CREATE TABLE {safe_table_name} (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols_def})")
        else:
            cur.execute(f"CREATE TABLE {safe_table_name} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    else:
        # Check for type changes and missing columns
        cur.execute(f"PRAGMA table_info({safe_table_name})")
        existing_cols = {row[1]: row[2] for row in cur.fetchall()}
        
        type_mismatches = []
        for col_name, sql_type in required_cols:
            if col_name in existing_cols and existing_cols[col_name] != sql_type:
                type_mismatches.append(col_name)
                
        if type_mismatches:
            type_mismatches_escaped = ", ".join([f"[{c}]" for c in type_mismatches])
            cur.execute(f"SELECT id, {type_mismatches_escaped} FROM {safe_table_name}")
            rows = cur.fetchall()
            conversion_failures = 0
            
            for row in rows:
                for idx, col in enumerate(type_mismatches):
                    val = row[idx+1]
                    if val is not None and str(val).strip() != "":
                        app_type = attr_app_types[col]
                        success = True
                        if app_type in ("int", "boolean"):
                            try: int(float(val))
                            except: success = False
                        elif app_type == "float":
                            try: float(val)
                            except: success = False
                        elif app_type in ("list", "matrix"):
                            try: ast.literal_eval(str(val))
                            except: success = False
                                
                        if not success:
                            conversion_failures += 1
                            
            if conversion_failures > 0:
                if parent_widget:
                    reply = QMessageBox.question(
                        parent_widget,
                        "Data Type Conversion Warning",
                        f"{conversion_failures} object values cannot be converted safely to the newly selected data types.\n"
                        f"If you continue, these incompatible values will be cleared (set to None).\n\n"
                        "Do you want to continue?",
                        QMessageBox.Yes | QMessageBox.No
                    )
                    if reply == QMessageBox.No:
                        conn.close()
                        return None, None
                        
            conn.commit()
            conn.execute("PRAGMA foreign_keys = OFF")
            
            view_name = f"view_{safe_table_name}"
            cur.execute(f"DROP VIEW IF EXISTS {view_name}")
            
            new_table = f"new_{safe_table_name}"
            cur.execute(f"DROP TABLE IF EXISTS {new_table}")
            
            cols_def = ", ".join([f"[{c}] {t}" for c, t in required_cols])
            if cols_def:
                cur.execute(f"CREATE TABLE {new_table} (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols_def})")
            else:
                cur.execute(f"CREATE TABLE {new_table} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            
            common_cols = list(set(existing_cols.keys()).intersection([c for c, t in required_cols]))
            if common_cols:
                escaped_common_cols = ", ".join([f"[{c}]" for c in common_cols])
                cur.execute(f"SELECT id, {escaped_common_cols} FROM {safe_table_name}")
                old_data = cur.fetchall()
                
                for row in old_data:
                    row_id = row[0]
                    new_values = [row_id]
                    insert_cols = ["id"]
                    placeholders = ["?"]
                    
                    for idx, col in enumerate(common_cols):
                        val = row[idx+1]
                        new_val = val
                        if col in type_mismatches and val is not None and str(val).strip() != "":
                            app_type = attr_app_types[col]
                            try:
                                if app_type in ("int", "boolean"): new_val = int(float(val))
                                elif app_type == "float": new_val = float(val)
                                elif app_type in ("list", "matrix"):
                                    parsed = ast.literal_eval(str(val))
                                    if isinstance(parsed, list): new_val = str(parsed)
                                    else: new_val = None
                                else: new_val = str(val)
                            except:
                                new_val = None
                        
                        new_values.append(new_val)
                        insert_cols.append(f"[{col}]")
                        placeholders.append("?")
                        
                    cur.execute(f"INSERT INTO {new_table} ({', '.join(insert_cols)}) VALUES ({', '.join(placeholders)})", new_values)
                    
            cur.execute(f"DROP TABLE {safe_table_name}")
            cur.execute(f"ALTER TABLE {new_table} RENAME TO {safe_table_name}")
            
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")
            
        else:
            for col_name, col_type in required_cols:
                if col_name not in existing_cols:
                    cur.execute(f"ALTER TABLE {safe_table_name} ADD COLUMN [{col_name}] {col_type}")

    # 3. GENERATE JUNCTION TABLES
    cur.execute("""
        SELECT c.name, r.rel_type, c.id, r.show_in_table 
        FROM relationships r JOIN classes c ON r.target_class = c.id 
        WHERE r.source_class = ? ORDER BY r.row_order
    """, (class_id,))
    outgoing_rels = cur.fetchall()
    
    for target_name, rel_type, target_class_id, show_in_table in outgoing_rels:
        safe_target_name = target_name.replace(' ', '_').lower()
        cur.execute(f"CREATE TABLE IF NOT EXISTS objects_{safe_target_name} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        junc_table = f"rel_{safe_table_name}_to_objects_{safe_target_name}"
        
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {junc_table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                target_id INTEGER,
                FOREIGN KEY(source_id) REFERENCES {safe_table_name}(id) ON DELETE CASCADE,
                FOREIGN KEY(target_id) REFERENCES objects_{safe_target_name}(id) ON DELETE CASCADE
            )
        """)
        
        if show_in_table:
            cur.execute("SELECT name, data_type FROM attributes WHERE class_id = ? AND is_title = 1 ORDER BY row_order", (target_class_id,))
            title_rows = cur.fetchall()
            if title_rows:
                for t_name, t_type in title_rows:
                    target_title_col = t_name.replace(' ', '_').lower()
                    display_label = f"[{target_name} ({t_name})]"
                    
                    # FIX: Ensure target table physically has the title column before querying it
                    sql_type = "TEXT"
                    if t_type in ("int", "boolean"): sql_type = "INTEGER"
                    elif t_type == "float": sql_type = "REAL"
                    try:
                        cur.execute(f"ALTER TABLE objects_{safe_target_name} ADD COLUMN [{target_title_col}] {sql_type}")
                    except sqlite3.OperationalError:
                        pass # Column already exists
                        
                    select_clause.append(f"(SELECT GROUP_CONCAT(t.[{target_title_col}]) FROM {junc_table} j JOIN objects_{safe_target_name} t ON j.target_id = t.id WHERE j.source_id = m.id) AS {display_label}")
            else:
                target_title_col = "id"
                display_label = f"[{target_name} (IDs)]"
                
                select_clause.append(f"(SELECT GROUP_CONCAT(t.[{target_title_col}]) FROM {junc_table} j JOIN objects_{safe_target_name} t ON j.target_id = t.id WHERE j.source_id = m.id) AS {display_label}")

    cur.execute("""
        SELECT c.name, r.rel_type, c.id, r.show_in_table 
        FROM relationships r JOIN classes c ON r.source_class = c.id 
        WHERE r.target_class = ? ORDER BY r.row_order
    """, (class_id,))
    incoming_rels = cur.fetchall()

    for source_name, rel_type, source_class_id, show_in_table in incoming_rels:
        safe_source_name = source_name.replace(' ', '_').lower()
        cur.execute(f"CREATE TABLE IF NOT EXISTS objects_{safe_source_name} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        junc_table = f"rel_objects_{safe_source_name}_to_{safe_table_name}"
        
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {junc_table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                target_id INTEGER,
                FOREIGN KEY(source_id) REFERENCES objects_{safe_source_name}(id) ON DELETE CASCADE,
                FOREIGN KEY(target_id) REFERENCES {safe_table_name}(id) ON DELETE CASCADE
            )
        """)
        
        if show_in_table:
            cur.execute("SELECT name, data_type FROM attributes WHERE class_id = ? AND is_title = 1 ORDER BY row_order", (source_class_id,))
            title_rows = cur.fetchall()
            if title_rows:
                for t_name, t_type in title_rows:
                    source_title_col = t_name.replace(' ', '_').lower()
                    display_label = f"[From {source_name} ({t_name})]"
                    
                    # FIX: Ensure source table physically has the title column before querying it
                    sql_type = "TEXT"
                    if t_type in ("int", "boolean"): sql_type = "INTEGER"
                    elif t_type == "float": sql_type = "REAL"
                    try:
                        cur.execute(f"ALTER TABLE objects_{safe_source_name} ADD COLUMN [{source_title_col}] {sql_type}")
                    except sqlite3.OperationalError:
                        pass # Column already exists
                        
                    select_clause.append(f"(SELECT GROUP_CONCAT(t.[{source_title_col}]) FROM {junc_table} j JOIN objects_{safe_source_name} t ON j.source_id = t.id WHERE j.target_id = m.id) AS {display_label}")
            else:
                source_title_col = "id"
                display_label = f"[From {source_name} (IDs)]"
                
                select_clause.append(f"(SELECT GROUP_CONCAT(t.[{source_title_col}]) FROM {junc_table} j JOIN objects_{safe_source_name} t ON j.source_id = t.id WHERE j.target_id = m.id) AS {display_label}")
            
    # 4. RE-GENERATE DYNAMIC SQL VIEW 
    view_name = f"view_{safe_table_name}"
    cur.execute(f"DROP VIEW IF EXISTS {view_name}")
    
    view_sql = f"CREATE VIEW {view_name} AS SELECT {', '.join(select_clause)} FROM {safe_table_name} m"
    cur.execute(view_sql)

    conn.commit()
    conn.close()
    
    return view_name, safe_table_name


# ==========================================
# OBJECT EDITOR DIALOG (ADD & EDIT)
# ==========================================
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
        
        layout = QVBoxLayout(self)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        
        scroll_widget = QWidget()
        form_layout = QFormLayout(scroll_widget)
        form_layout.setContentsMargins(10, 10, 10, 10)
        form_layout.setSpacing(15)
        
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        # --- Add Attributes to Form ---
        cur.execute("SELECT id, name, data_type, is_unique, is_required FROM attributes WHERE class_id = ? ORDER BY row_order", (self.class_id,))
        attributes = cur.fetchall()

        for attr_id, attr_name, attr_type, is_unique, is_required in attributes:
            safe_col_name = attr_name.replace(' ', '_').lower()
            self.attr_app_types[safe_col_name] = attr_type
            self.attr_constraints[safe_col_name] = {
                'name': attr_name,
                'unique': bool(is_unique),
                'required': bool(is_required)
            }
            
            if attr_type == "int":
                widget = QSpinBox()
                widget.setRange(-2147483648, 2147483647) 
            elif attr_type == "float":
                widget = QDoubleSpinBox()
                widget.setRange(-1e9, 1e9)
                widget.setDecimals(4)
            elif attr_type == "boolean":
                widget = QCheckBox()
                widget.setText("Yes / True")
            elif attr_type == "date":
                widget = QDateTimeEdit()
                widget.setCalendarPopup(True) 
                widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            elif attr_type == "long string":
                widget = QTextEdit()
                widget.setMaximumHeight(100) 
            elif attr_type in ("list", "matrix"):
                widget = QLineEdit()
                widget.setText("[]") 
                if attr_type == "matrix":
                    cur.execute("SELECT count(*) FROM matrix_columns WHERE attribute_id = ?", (attr_id,))
                    col_count = cur.fetchone()[0]
                    self.matrix_col_counts[safe_col_name] = col_count
                    widget.setToolTip(f"Provide exactly {col_count} list(s) inside the main list. e.g. [[1,2], [3,4]]")
                else:
                    widget.setToolTip("Must be a valid python list e.g. ['A', 'B'] or [1, 2]")
            else:
                widget = QLineEdit() 
                
            self.input_widgets[safe_col_name] = widget
            
            label_text = attr_name + (" <span style='color:red;'>*</span>" if is_required else "") + ":"
            label = QLabel(label_text)
            form_layout.addRow(label, widget)

        # --- Add Relationships to Form ---
        cur.execute("""
            SELECT c.id, c.name, r.rel_type 
            FROM relationships r JOIN classes c ON r.target_class = c.id 
            WHERE r.source_class = ? ORDER BY r.row_order
        """, (self.class_id,))
        relationships = cur.fetchall()

        for target_class_id, target_name, rel_type in relationships:
            safe_target_name = target_name.replace(' ', '_').lower()
            junc_table = f"rel_{self.table_name}_to_objects_{safe_target_name}"
            
            rel_container = QVBoxLayout()
            rel_container.setContentsMargins(0, 0, 0, 0)
            rel_container.setSpacing(5)
            
            search_box = QLineEdit()
            search_box.setPlaceholderText(f"Search {target_name}...")
            rel_container.addWidget(search_box)
            
            list_widget = QListWidget()
            if rel_type == "one_to_many":
                list_widget.setSelectionMode(QListWidget.SingleSelection)
            else:
                list_widget.setSelectionMode(QListWidget.MultiSelection)
                
            list_widget.setMinimumHeight(120) 
            target_table = f"objects_{safe_target_name}"
            
            cur.execute("SELECT name FROM attributes WHERE class_id = ? AND is_title = 1 ORDER BY row_order", (target_class_id,))
            title_rows = cur.fetchall()
            
            try:
                if title_rows:
                    title_cols = [r[0].replace(' ', '_').lower() for r in title_rows]
                    escaped_cols = ", ".join([f"[{c}]" for c in title_cols])
                    cur.execute(f"SELECT id, {escaped_cols} FROM {target_table}")
                    
                    for row in cur.fetchall():
                        t_id = row[0]
                        display_vals = []
                        
                        # Process each title part and replace empty ones with "None"
                        for val in row[1:]:
                            if val is None or str(val).strip() == "":
                                display_vals.append("None")
                            else:
                                display_vals.append(str(val))
                                
                        combined_titles = " --- ".join(display_vals)
                        display_text = f"{combined_titles} (ID: {t_id})"
                        search_text_data = combined_titles.lower()
                            
                        item = QListWidgetItem(display_text)
                        item.setData(Qt.UserRole, t_id)
                        item.setData(Qt.UserRole + 1, search_text_data)
                        list_widget.addItem(item)
                else:
                    cur.execute(f"SELECT id FROM {target_table}")
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
                
            self.rel_widgets[junc_table] = {
                "widget": list_widget,
                "rel_type": rel_type,
                "target_name": target_name
            }
            form_layout.addRow(f"Rel: {target_name}:", rel_container)

        # --- PRE-FILL DATA ---
        if self.obj_id:
            cols = list(self.input_widgets.keys())
            if cols:
                escaped_cols = [f"[{c}]" for c in cols]
                cur.execute(f"SELECT {', '.join(escaped_cols)} FROM {self.table_name} WHERE id = ?", (self.obj_id,))
                row = cur.fetchone()
                if row:
                    for idx, col in enumerate(cols):
                        val = row[idx]
                        widget = self.input_widgets[col]
                        
                        if val is not None:
                            if isinstance(widget, QSpinBox) or isinstance(widget, QDoubleSpinBox):
                                widget.setValue(val)
                            elif isinstance(widget, QCheckBox):
                                widget.setChecked(bool(val))
                            elif isinstance(widget, QDateTimeEdit):
                                dt = QDateTime.fromString(str(val), "yyyy-MM-dd HH:mm:ss")
                                if dt.isValid(): widget.setDateTime(dt)
                            elif isinstance(widget, QTextEdit):
                                widget.setPlainText(str(val))
                            else:
                                widget.setText(str(val))
                                
            for junc_table, data in self.rel_widgets.items():
                cur.execute(f"SELECT target_id FROM {junc_table} WHERE source_id = ?", (self.obj_id,))
                existing_targets = [r[0] for r in cur.fetchall()]
                lw = data["widget"]
                for i in range(lw.count()):
                    item = lw.item(i)
                    if item.data(Qt.UserRole) in existing_targets:
                        item.setSelected(True)

        conn.close()
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)

        btn_layout = QHBoxLayout()
        btn_save = QPushButton("Save Object")
        btn_save.clicked.connect(self.save_record)
        btn_cancel = QPushButton("Cancel")
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
            if search_target is None:
                search_target = item.text().lower() 
            item.setHidden(search_text not in search_target)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            return 
        super().keyPressEvent(event)

    def save_record(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = 1")
        cur = conn.cursor()
        
        try:
            columns = []
            values = []
            placeholders = []
            
            for col_name, widget in self.input_widgets.items():
                app_type = self.attr_app_types.get(col_name)
                constraints = self.attr_constraints.get(col_name)
                attr_display_name = constraints['name']
                
                if isinstance(widget, QSpinBox) or isinstance(widget, QDoubleSpinBox):
                    val = widget.value()
                    is_empty = False 
                elif isinstance(widget, QCheckBox):
                    val = 1 if widget.isChecked() else 0
                    is_empty = False
                elif isinstance(widget, QDateTimeEdit):
                    val = widget.dateTime().toString("yyyy-MM-dd HH:mm:ss")
                    is_empty = False
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
                                if not isinstance(parsed, list):
                                    raise ValueError("Not a list.")
                                    
                                if app_type == "matrix":
                                    expected_cols = self.matrix_col_counts.get(col_name, 0)
                                    if len(parsed) != expected_cols:
                                        raise ValueError(f"Matrix expects exactly {expected_cols} column list(s) inside the main list.")
                                    for inner in parsed:
                                        if not isinstance(inner, list):
                                            raise ValueError("Each matrix column must be a list.")
                                
                                val = str(parsed)
                            except Exception as e:
                                err_msg = str(e) if str(e) else "Invalid Python syntax."
                                QMessageBox.warning(self, "Validation Error", f"'{attr_display_name}' parsing failed: {err_msg}\n\nExample: [['A', 'B'], [1, 2]]")
                                conn.close()
                                return 
                        else:
                            val = raw_text

                if constraints['required'] and is_empty:
                    QMessageBox.warning(self, "Validation Error", f"'{attr_display_name}' is a required field.")
                    conn.close()
                    return

                if constraints['unique'] and not is_empty:
                    if self.obj_id:
                        cur.execute(f"SELECT id FROM {self.table_name} WHERE [{col_name}] = ? AND id != ?", (val, self.obj_id))
                    else:
                        cur.execute(f"SELECT id FROM {self.table_name} WHERE [{col_name}] = ?", (val,))
                        
                    if cur.fetchone():
                        QMessageBox.warning(self, "Validation Error", f"'{attr_display_name}' must be unique. The value '{val}' already exists.")
                        conn.close()
                        return

                columns.append(col_name)
                placeholders.append("?")
                values.append(val)

            active_obj_id = self.obj_id

            if columns:
                if self.obj_id:
                    set_clause = ", ".join([f"[{c}] = ?" for c in columns])
                    query = f"UPDATE {self.table_name} SET {set_clause} WHERE id = ?"
                    cur.execute(query, tuple(values) + (self.obj_id,))
                else:
                    escaped_columns = [f"[{c}]" for c in columns]
                    query = f"INSERT INTO {self.table_name} ({', '.join(escaped_columns)}) VALUES ({', '.join(placeholders)})"
                    cur.execute(query, tuple(values))
                    active_obj_id = cur.lastrowid
            else:
                if not self.obj_id:
                    query = f"INSERT INTO {self.table_name} DEFAULT VALUES"
                    cur.execute(query)
                    active_obj_id = cur.lastrowid
                
            for junc_table, data in self.rel_widgets.items():
                if self.obj_id:
                    cur.execute(f"DELETE FROM {junc_table} WHERE source_id = ?", (active_obj_id,))
                selected_ids = [int(item.data(Qt.UserRole)) for item in data["widget"].selectedItems()]
                for target_id in selected_ids:
                    cur.execute(f"INSERT INTO {junc_table} (source_id, target_id) VALUES (?, ?)", (active_obj_id, target_id))
            
            conn.commit()
            conn.close()
            self.accept()
            
        except Exception as e:
            QMessageBox.critical(self, "Database Error", str(e))
            if 'conn' in locals(): conn.close()


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Database Settings")
        self.setWindowIcon(get_app_icon())
        self.resize(500, 150)
        self.settings = QSettings("MyCompany", "DatabaseManagerApp")
        
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel("Database Path:"))

        path_layout = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setText(self.settings.value("db_path", ""))
        
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self.browse_file)
        
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(btn_browse)
        layout.addLayout(path_layout)

        layout.addStretch()

        btn_save = QPushButton("Save && Connect")
        btn_save.clicked.connect(self.save_and_check_db)
        
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(btn_save)
        
        layout.addLayout(btn_layout)

    def browse_file(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "Select SQLite Database", "", "SQLite DB (*.db *.sqlite)")
        if file_name:
            self.path_input.setText(file_name)

    def save_and_check_db(self):
        path = self.path_input.text().strip()
        if not path: return

        if not os.path.exists(path):
            init_db(path)

        self.settings.setValue("db_path", path)
        QMessageBox.information(self, "Success", "Database connected and set as default.")
        self.accept() 


class DataBrowserPage(QWidget):
    def __init__(self, main_window): 
        super().__init__()
        self.main_window = main_window
        self.current_class_id = None
        self.current_class_name = None
        
        self.current_view_name = None
        self.current_table_name = None
        self.db_path = ""
        
        self.page_size = 100
        self.current_page = 0
        self.total_pages = 0
        self.total_records = 0
        self.current_sort_col = "ID"
        self.current_sort_order = "ASC"
        
        layout = QVBoxLayout(self)

        self.header_label = QLabel("<h2></h2>")
        layout.addWidget(self.header_label)

        search_layout = QHBoxLayout()
        
        self.col_combo = QComboBox()
        self.col_combo.addItem("All Columns", -1)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter text...")
        self.search_input.returnPressed.connect(self.trigger_search)
        
        self.btn_search = QPushButton("\U0001F50D Search") 
        self.btn_search.clicked.connect(self.trigger_search)
        
        self.btn_import = QPushButton("\U0001F4E4 Import")
        self.btn_import.clicked.connect(self.import_data)
        
        self.btn_export = QPushButton("\U0001F4BE Export")
        self.btn_export.clicked.connect(self.export_data)
        
        self.btn_add = QPushButton("+ Add Object")
        self.btn_edit = QPushButton("Edit")
        self.btn_delete = QPushButton("Delete")
        
        self.btn_import.setEnabled(False)
        self.btn_add.setEnabled(False) 
        self.btn_edit.setEnabled(False) 
        self.btn_delete.setEnabled(False) 
        
        self.btn_add.clicked.connect(self.open_add_dialog)
        self.btn_edit.clicked.connect(self.open_edit_dialog)
        self.btn_delete.clicked.connect(self.delete_selected)
        
        search_layout.addWidget(QLabel("Search In:"))
        search_layout.addWidget(self.col_combo)
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.btn_search)
        search_layout.addWidget(self.btn_import)
        search_layout.addWidget(self.btn_export)
        search_layout.addWidget(self.btn_add)
        search_layout.addWidget(self.btn_edit)
        search_layout.addWidget(self.btn_delete)
        layout.addLayout(search_layout)

        self.table_view = QTableView()
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setSelectionBehavior(QTableView.SelectRows)
        self.table_view.setSelectionMode(QTableView.SingleSelection)
        self.table_view.setEditTriggers(QTableView.NoEditTriggers)
        self.table_view.doubleClicked.connect(self.open_edit_dialog)
        
        self.table_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self.show_context_menu)
        
        self.table_view.setSortingEnabled(True)
        header = self.table_view.horizontalHeader()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        header.sortIndicatorChanged.connect(self.on_sort_changed)
        layout.addWidget(self.table_view)

        self.query_model = QSqlQueryModel()
        self.table_view.setModel(self.query_model)
        self.table_view.selectionModel().selectionChanged.connect(self.on_selection_changed)

        page_layout = QHBoxLayout()
        
        self.btn_prev = QPushButton("\u25C0 Prev") 
        self.btn_prev.clicked.connect(self.prev_page)
        
        self.page_label = QLabel("Page 0 of 0 (Total: 0)")
        self.page_label.setAlignment(Qt.AlignCenter)
        
        self.btn_next = QPushButton("Next \u25B6") 
        self.btn_next.clicked.connect(self.next_page)
        
        self.page_size_combo = QComboBox()
        self.page_size_combo.addItems(["50", "100", "500", "1000"])
        self.page_size_combo.setCurrentText("100")
        self.page_size_combo.currentTextChanged.connect(self.change_page_size)
        
        page_layout.addStretch()
        page_layout.addWidget(self.btn_prev)
        page_layout.addWidget(self.page_label)
        page_layout.addWidget(self.btn_next)
        page_layout.addWidget(QLabel("  Rows per page:"))
        page_layout.addWidget(self.page_size_combo)
        layout.addLayout(page_layout)

    def load_table_data(self, class_id, class_name):
        self.current_class_id = class_id
        self.current_class_name = class_name
        self.header_label.setText(f"<h2>{class_name}</h2>")
        
        self.btn_import.setEnabled(True)
        self.btn_add.setEnabled(True)
        self.btn_edit.setEnabled(False)
        self.btn_delete.setEnabled(False)
        
        self.db_path = QSettings("MyCompany", "DatabaseManagerApp").value("db_path", "")
        self.current_view_name, self.current_table_name = sync_physical_table(self.db_path, class_id, class_name, parent_widget=self)
        
        if not self.current_view_name:
            self.header_label.setText("<h2>Sync Aborted</h2>")
            self.btn_import.setEnabled(False)
            self.btn_add.setEnabled(False)
            return
            
        self.search_input.clear()
        self.current_sort_col = "ID"
        self.current_sort_order = "ASC"
        self.current_page = 0
        self.table_view.horizontalHeader().setSortIndicator(0, Qt.AscendingOrder)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({self.current_view_name})")
        columns = [row[1] for row in cur.fetchall()]
        conn.close()
        
        self.col_combo.blockSignals(True)
        self.col_combo.clear()
        self.col_combo.addItem("All Columns", -1)
        for i, col_name in enumerate(columns):
            self.col_combo.addItem(col_name, i)
        self.col_combo.blockSignals(False)
        
        self.build_and_exec_query()

    def build_and_exec_query(self):
        if not self.current_view_name: return
        
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        
        base_query = f"FROM {self.current_view_name}"
        where_clauses = []
        params = []
        
        search_text = self.search_input.text().strip()
        if search_text:
            col_idx = self.col_combo.currentData()
            if col_idx == -1: 
                cur.execute(f"PRAGMA table_info({self.current_view_name})")
                col_names = [row[1] for row in cur.fetchall()]
                or_clauses = [f"[{c}] LIKE ?" for c in col_names]
                where_clauses.append("(" + " OR ".join(or_clauses) + ")")
                params.extend([f"%{search_text}%"] * len(col_names))
            else: 
                col_name = self.col_combo.itemText(self.col_combo.currentIndex())
                where_clauses.append(f"[{col_name}] LIKE ?")
                params.append(f"%{search_text}%")
        
        where_sql = ""
        if where_clauses:
            where_sql = " WHERE " + " AND ".join(where_clauses)
            
        count_query = f"SELECT COUNT(*) {base_query} {where_sql}"
        cur.execute(count_query, params)
        self.total_records = cur.fetchone()[0]
        self.total_pages = max(1, (self.total_records + self.page_size - 1) // self.page_size)
        
        if self.current_page >= self.total_pages:
            self.current_page = max(0, self.total_pages - 1)
            
        offset = self.current_page * self.page_size
        order_sql = f"ORDER BY [{self.current_sort_col}] {self.current_sort_order}"
        final_query = f"SELECT * {base_query} {where_sql} {order_sql} LIMIT {self.page_size} OFFSET {offset}"
        
        db = QSqlDatabase.database()
        query = QSqlQuery(db)
        query.prepare(final_query)
        for p in params: query.addBindValue(p)
        query.exec()
        
        self.query_model.setQuery(query)
        
        self.page_label.setText(f"Page {self.current_page + 1} of {self.total_pages} (Total: {self.total_records})")
        self.btn_prev.setEnabled(self.current_page > 0)
        self.btn_next.setEnabled(self.current_page < self.total_pages - 1)
        
        conn.close()

    def on_sort_changed(self, logical_index, order):
        col_name = self.query_model.headerData(logical_index, Qt.Horizontal)
        self.current_sort_col = col_name
        self.current_sort_order = "ASC" if order == Qt.AscendingOrder else "DESC"
        self.build_and_exec_query()

    def trigger_search(self):
        self.current_page = 0
        self.build_and_exec_query()

    def prev_page(self):
        if self.current_page > 0:
            self.current_page -= 1
            self.build_and_exec_query()
            
    def next_page(self):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.build_and_exec_query()
            
    def change_page_size(self, new_size):
        self.page_size = int(new_size)
        self.current_page = 0
        self.build_and_exec_query()

    def on_selection_changed(self):
        has_sel = bool(self.table_view.selectionModel().selectedRows())
        self.btn_edit.setEnabled(has_sel)
        self.btn_delete.setEnabled(has_sel)

    def get_selected_id(self):
        rows = self.table_view.selectionModel().selectedRows()
        if not rows: return None
        idx = rows[0]
        id_index = self.query_model.index(idx.row(), 0)
        return self.query_model.data(id_index)

    def show_context_menu(self, position):
        if not self.table_view.selectionModel().selectedRows():
            return
        from PySide6.QtWidgets import QMenu
        menu = QMenu()
        act_edit = menu.addAction("Edit Object")
        act_del = menu.addAction("Delete Object")
        
        action = menu.exec(self.table_view.viewport().mapToGlobal(position))
        if action == act_edit:
            self.open_edit_dialog()
        elif action == act_del:
            self.delete_selected()

    def open_add_dialog(self):
        dialog = ObjectEditorDialog(self.db_path, self.current_class_id, self.current_class_name, self.current_table_name, None, self)
        if dialog.exec():
            self.build_and_exec_query() 
            
    def open_edit_dialog(self):
        obj_id = self.get_selected_id()
        if not obj_id: return
        dialog = ObjectEditorDialog(self.db_path, self.current_class_id, self.current_class_name, self.current_table_name, obj_id, self)
        if dialog.exec():
            self.build_and_exec_query() 
            
    def delete_selected(self):
        obj_id = self.get_selected_id()
        if not obj_id: return
        reply = QMessageBox.question(self, "Confirm Delete", f"Are you sure you want to delete this {self.current_class_name}?\n\nIts relationships will be automatically safely removed.", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA foreign_keys = 1") 
            cur = conn.cursor()
            try:
                cur.execute(f"DELETE FROM {self.current_table_name} WHERE id = ?", (obj_id,))
                conn.commit()
                self.build_and_exec_query()
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))
            finally:
                conn.close()

    def export_data(self):
        if not self.query_model: return
        file_path, selected_filter = QFileDialog.getSaveFileName(self, "Export Data", "", "CSV Files (*.csv);;Excel Files (*.xlsx)")
        if not file_path: return
        
        if "csv" in selected_filter.lower() and not file_path.lower().endswith(".csv"):
            file_path += ".csv"
        elif "excel" in selected_filter.lower() and not file_path.lower().endswith(".xlsx"):
            file_path += ".xlsx"
            
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({self.current_view_name})")
        headers = [row[1] for row in cur.fetchall()]
        
        cur.execute(f"SELECT * FROM {self.current_view_name} ORDER BY [{self.current_sort_col}] {self.current_sort_order}")
        raw_data = cur.fetchall()
        conn.close()
        
        data = [["" if v is None else str(v) for v in row] for row in raw_data]

        if file_path.endswith(".csv"):
            try:
                with open(file_path, mode='w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
                    writer.writerows(data)
                QMessageBox.information(self, "Success", "Exported to CSV successfully!")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export: {e}")
                
        elif file_path.endswith(".xlsx"):
            try:
                import openpyxl
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.append(headers)
                for row in data:
                    ws.append(row)
                wb.save(file_path)
                QMessageBox.information(self, "Success", "Exported to Excel successfully!")
            except ImportError:
                QMessageBox.warning(self, "Missing Library", "The 'openpyxl' library is required to export to Excel.\nPlease install it via terminal: pip install openpyxl\n\nFalling back to CSV export...")
                fallback_path = file_path.replace(".xlsx", ".csv")
                with open(fallback_path, mode='w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
                    writer.writerows(data)
                QMessageBox.information(self, "Fallback", f"Exported to CSV instead at:\n{fallback_path}")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to export: {e}")

    def import_data(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import Data", "", "Excel Files (*.xlsx)")
        if not file_path: return
        
        try:
            import openpyxl
        except ImportError:
            QMessageBox.critical(self, "Missing Library", "The 'openpyxl' library is required to import from Excel.\n\nPlease install it via terminal: pip install openpyxl")
            return
            
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            
            cur.execute("SELECT id, name, data_type, is_unique, is_required FROM attributes WHERE class_id = ?", (self.current_class_id,))
            attributes_meta = {}
            for attr_id, name, d_type, is_uniq, is_req in cur.fetchall():
                attributes_meta[name.lower()] = {
                    "safe_col": name.replace(' ', '_').lower(),
                    "type": d_type,
                    "unique": bool(is_uniq),
                    "required": bool(is_req)
                }
                
            matrix_counts = {}
            cur.execute("""
                SELECT a.name, COUNT(m.id) 
                FROM attributes a JOIN matrix_columns m ON a.id = m.attribute_id 
                WHERE a.class_id = ? GROUP BY a.id
            """, (self.current_class_id,))
            for attr_name, col_count in cur.fetchall():
                matrix_counts[attr_name.lower()] = col_count
                
            cur.execute("""
                SELECT c.name, r.rel_type, c.id 
                FROM relationships r JOIN classes c ON r.target_class = c.id 
                WHERE r.source_class = ?
            """, (self.current_class_id,))
            relationships_meta = {}
            for t_name, r_type, t_class_id in cur.fetchall():
                safe_target_name = t_name.replace(' ', '_').lower()
                relationships_meta[t_name.lower()] = {
                    "target_table": f"objects_{safe_target_name}",
                    "junc_table": f"rel_{self.current_table_name}_to_objects_{safe_target_name}"
                }

            existing_unique_values = {}
            for name_lower, meta in attributes_meta.items():
                if meta["unique"]:
                    cur.execute(f"SELECT [{meta['safe_col']}] FROM {self.current_table_name} WHERE [{meta['safe_col']}] IS NOT NULL")
                    existing_unique_values[meta["safe_col"]] = set(row[0] for row in cur.fetchall())

            wb = openpyxl.load_workbook(file_path, data_only=True)
            ws = wb.active
            
            all_rows = list(ws.iter_rows(values_only=True))
            if len(all_rows) < 2:
                raise ValueError("Excel file must contain at least a header row and one data row.")
                
            headers = [str(h).lower().strip() if h else "" for h in all_rows[0]]
            
            for name_lower, meta in attributes_meta.items():
                if meta["required"] and name_lower not in headers:
                    raise ValueError(f"CRITICAL: The required attribute '{meta['safe_col']}' is completely missing from the Excel headers!")

            in_file_unique_tracker = {meta["safe_col"]: set() for meta in attributes_meta.values() if meta["unique"]}
            validated_inserts = []
            validated_relationships = []

            for row_number, row_data in enumerate(all_rows[1:], start=2): 
                if all(cell is None or str(cell).strip() == "" for cell in row_data):
                    continue 

                insert_cols = []
                insert_vals = []
                pending_rels = {} 
                
                for col_idx, cell_value in enumerate(row_data):
                    if col_idx >= len(headers): break
                    header = headers[col_idx]
                    if not header: continue
                    
                    if isinstance(cell_value, datetime.datetime):
                        val = cell_value.strftime("%Y-%m-%d %H:%M:%S")
                    else:
                        val = None if (cell_value is None or str(cell_value).strip() == "") else str(cell_value).strip()
                    
                    is_empty = (val is None)

                    if header in attributes_meta:
                        meta = attributes_meta[header]
                        safe_col = meta["safe_col"]
                        
                        if is_empty:
                            if meta["required"]:
                                raise ValueError(f"Row {row_number}: '{header}' is Required but cell is empty.")
                            val_final = None
                        else:
                            if meta["type"] == "int":
                                try: val_final = int(float(val))
                                except: raise ValueError(f"Row {row_number}: '{header}' must be an Integer.")
                            elif meta["type"] == "float":
                                try: val_final = float(val)
                                except: raise ValueError(f"Row {row_number}: '{header}' must be a Float/Number.")
                            elif meta["type"] == "boolean":
                                v_low = val.lower()
                                if v_low in ('true', '1', 'yes', 'y', 't'): val_final = 1
                                elif v_low in ('false', '0', 'no', 'n', 'f'): val_final = 0
                                else: raise ValueError(f"Row {row_number}: '{header}' must be True/False or 1/0.")
                            elif meta["type"] in ("list", "matrix"):
                                try:
                                    parsed = ast.literal_eval(val)
                                    if not isinstance(parsed, list):
                                        raise ValueError(f"Row {row_number}: '{header}' must be an enclosed list [].")
                                    if meta["type"] == "matrix":
                                        expected_cols = matrix_counts.get(header, 0)
                                        if len(parsed) != expected_cols:
                                            raise ValueError(f"Row {row_number}: Matrix '{header}' expects exactly {expected_cols} inner list(s).")
                                    val_final = str(parsed)
                                except Exception as e:
                                    raise ValueError(f"Row {row_number}: Invalid python syntax in '{header}'. {e}")
                            else:
                                val_final = val 
                                
                        if meta["unique"] and val_final is not None:
                            if val_final in existing_unique_values[safe_col]:
                                raise ValueError(f"Row {row_number}: '{header}' is Unique. The value '{val_final}' already exists in the database.")
                            if val_final in in_file_unique_tracker[safe_col]:
                                raise ValueError(f"Row {row_number}: '{header}' is Unique. The value '{val_final}' is duplicated inside the Excel file.")
                            in_file_unique_tracker[safe_col].add(val_final)

                        insert_cols.append(f"[{safe_col}]")
                        insert_vals.append(val_final)

                    elif header in relationships_meta:
                        if not is_empty:
                            rel_meta = relationships_meta[header]
                            try:
                                target_ids = [int(x.strip()) for x in val.split(",")]
                            except ValueError:
                                raise ValueError(f"Row {row_number}: Relationship '{header}' must be comma-separated numeric IDs.")
                                
                            for tid in target_ids:
                                cur.execute(f"SELECT id FROM {rel_meta['target_table']} WHERE id = ?", (tid,))
                                if not cur.fetchone():
                                    raise ValueError(f"Row {row_number}: Invalid Target ID '{tid}' for relationship '{header}'. Object doesn't exist.")
                            
                            pending_rels[rel_meta["junc_table"]] = target_ids

                validated_inserts.append((insert_cols, insert_vals))
                validated_relationships.append(pending_rels)

            records_imported = 0
            for i, (cols, vals) in enumerate(validated_inserts):
                if not cols:
                    cur.execute(f"INSERT INTO {self.current_table_name} DEFAULT VALUES")
                else:
                    placeholders = ", ".join(["?"] * len(cols))
                    query = f"INSERT INTO {self.current_table_name} ({', '.join(cols)}) VALUES ({placeholders})"
                    cur.execute(query, vals)
                    
                new_obj_id = cur.lastrowid
                records_imported += 1
                
                rels_to_insert = validated_relationships[i]
                for junc_table, target_ids in rels_to_insert.items():
                    for tid in target_ids:
                        cur.execute(f"INSERT INTO {junc_table} (source_id, target_id) VALUES (?, ?)", (new_obj_id, tid))
            
            conn.commit()
            self.build_and_exec_query() 
            QMessageBox.information(self, "Import Successful", f"Successfully imported {records_imported} object(s)!")
            
        except Exception as e:
            if 'conn' in locals(): conn.rollback()
            QMessageBox.critical(self, "Import Failed", f"Import aborted. No changes were made to the database.\n\nReason:\n{str(e)}")
        finally:
            if 'conn' in locals(): conn.close()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nexus")
        self.setWindowIcon(get_app_icon())
        self.resize(1200, 768)

        self.setup_menu()

        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        self.sidebar = QTreeWidget()
        self.sidebar.setHeaderHidden(True)
        self.sidebar.itemClicked.connect(self.change_class)
        splitter.addWidget(self.sidebar)

        self.data_browser = DataBrowserPage(self)
        splitter.addWidget(self.data_browser)
        
        splitter.setSizes([200, 824])
        self.refresh_sidebar()

    def setup_menu(self):
        menubar = self.menuBar()
        db_menu = menubar.addMenu("Database")

        action_settings = QAction("\u2699\uFE0F Settings", self)
        action_settings.triggered.connect(self.open_settings)
        db_menu.addAction(action_settings)

        action_builder = QAction("\U0001F6E0\uFE0F Class Builder", self)
        action_builder.triggered.connect(self.open_builder)
        db_menu.addAction(action_builder)

    def open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec(): 
            self.refresh_sidebar()

    def open_builder(self):
        dialog = ClassBuilderDialog(self)
        dialog.exec()
        self.refresh_sidebar() 

    def refresh_sidebar(self):
        self.sidebar.clear()
        settings = QSettings("MyCompany", "DatabaseManagerApp")
        db_path = settings.value("db_path", "")
        
        if not db_path or not os.path.exists(db_path):
            item = QTreeWidgetItem(["No database connected."])
            item.setFlags(Qt.NoItemFlags)
            self.sidebar.addTopLevelItem(item)
            return

        if QSqlDatabase.contains():
            db = QSqlDatabase.database()
        else:
            db = QSqlDatabase.addDatabase("QSQLITE")
        db.setDatabaseName(db_path)
        if not db.open(): return

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        try:
            cur.execute("SELECT id, name, path FROM classes ORDER BY path ASC, name ASC")
            
            root_items = {}
            parent_item = self.sidebar.invisibleRootItem()
            
            for c_id, c_name, c_path in cur.fetchall():
                c_path = (c_path or "").strip().strip('/')
                current_parent = parent_item
                
                if c_path:
                    current_path = ""
                    for part in c_path.split('/'):
                        part = part.strip()
                        if not part: continue
                        current_path = f"{current_path}/{part}" if current_path else part
                        
                        if current_path not in root_items:
                            folder_item = QTreeWidgetItem([f"\U0001F4C1 {part}"])
                            current_parent.addChild(folder_item)
                            root_items[current_path] = folder_item
                        
                        current_parent = root_items[current_path]
                
                class_item = QTreeWidgetItem([f"\U0001F4C4 {c_name}"])
                class_item.setData(0, Qt.UserRole, c_id)
                current_parent.addChild(class_item)
                
            self.sidebar.expandAll()
            
        except sqlite3.OperationalError:
            pass 
        finally:
            conn.close()

    def change_class(self, item, column):
        class_id = item.data(0, Qt.UserRole)
        if class_id:
            class_name = item.text(0).replace("📄 ", "") 
            self.data_browser.load_table_data(class_id, class_name)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion") 
    app.setWindowIcon(get_app_icon()) 
    
    app_font = app.font()
    app_font.setPointSize(10)
    app.setFont(app_font)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())