import sys
import sqlite3
import os
import csv
import ast
import datetime
import traceback
import qtawesome as qta
from contextlib import redirect_stdout, redirect_stderr

try:
    import pandas as pd
except ImportError:
    pd = None

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, 
    QTreeWidget, QTreeWidgetItem, QPushButton, QLineEdit, QLabel, QTableView, 
    QHeaderView, QMessageBox, QFileDialog, QListWidget, QListWidgetItem, QDialog,
    QSplitter, QScrollArea, QComboBox, QInputDialog, QFormLayout, QSpinBox, 
    QDoubleSpinBox, QTextEdit, QDateTimeEdit, QCheckBox, QPlainTextEdit, QTabWidget,
    QMenuBar, QMenu
)
from PySide6.QtCore import Qt, QSettings, QDateTime
from PySide6.QtSql import QSqlDatabase, QSqlQueryModel, QSqlQuery
from PySide6.QtGui import QAction, QFontDatabase, QFont

from class_builder_dialog import ClassBuilderDialog, init_db, get_app_icon, sanitize_name, qid

# ==========================================
# PHYSICAL SCHEMA SYNCHRONIZATION
# ==========================================
def safe_convert(val, app_type):
    """Safely cast user values to target database data types, strictly failing if it's destructive."""
    if val is None or str(val).strip() == "": return True, None
    try:
        if app_type == "boolean":
            v = int(float(val))
            if v in (0, 1): return True, v
            return False, None
        elif app_type == "int":
            return True, int(float(val))
        elif app_type == "float":
            return True, float(val)
        elif app_type in ("list", "matrix"):
            parsed = ast.literal_eval(str(val))
            if isinstance(parsed, list):
                return True, str(parsed)
            return False, None
        else:
            return True, str(val)
    except:
        return False, None

def sync_physical_table(db_path, class_id, class_name, parent_widget=None):
    safe_table_name = f"objects_{sanitize_name(class_name)}"
    
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        
        cur.execute("SELECT name, data_type, show_in_table, is_title, lookup_query FROM attributes WHERE class_id = ? ORDER BY row_order", (class_id,))
        attributes = cur.fetchall()
        
        required_cols = []
        attr_app_types = {}
        
        for attr_name, attr_type, show_in_table, is_title, lookup_query in attributes:
            safe_col_name = sanitize_name(attr_name)
            if attr_type == "look-through":
                attr_app_types[safe_col_name] = attr_type
                if lookup_query:
                    parts = lookup_query.split('.')
                    if len(parts) == 2:
                        tgt_class = sanitize_name(parts[0].strip())
                        cur.execute(f"CREATE TABLE IF NOT EXISTS {qid('objects_' + tgt_class)} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
                        cur.execute(f"CREATE VIEW IF NOT EXISTS {qid('base_view_objects_' + tgt_class)} AS SELECT id AS [ID] FROM {qid('objects_' + tgt_class)}")
                continue

            if attr_type in ("int", "boolean"): sql_type = "INTEGER"
            elif attr_type == "float": sql_type = "REAL"
            else: sql_type = "TEXT"
                
            required_cols.append((safe_col_name, sql_type))
            attr_app_types[safe_col_name] = attr_type

        cur.execute("SELECT count(name) FROM sqlite_master WHERE type='table' AND name=?", (safe_table_name,))
        if cur.fetchone()[0] == 0:
            cols_def = ", ".join([f"{qid(c)} {t}" for c, t in required_cols])
            cur.execute(f"CREATE TABLE {qid(safe_table_name)} (id INTEGER PRIMARY KEY AUTOINCREMENT{', ' + cols_def if cols_def else ''})")
        else:
            cur.execute(f"PRAGMA table_info({qid(safe_table_name)})")
            existing_cols = {row[1]: row[2] for row in cur.fetchall()}
            
            type_mismatches = []
            for col_name, sql_type in required_cols:
                if col_name in existing_cols and existing_cols[col_name] != sql_type:
                    type_mismatches.append(col_name)
                    
            cols_to_remove = set(existing_cols.keys()) - {c for c, t in required_cols} - {'id'}
                    
            if type_mismatches or cols_to_remove:
                conversion_failures = 0
                if type_mismatches:
                    type_mismatches_escaped = ", ".join([qid(c) for c in type_mismatches])
                    cur.execute(f"SELECT id, {type_mismatches_escaped} FROM {qid(safe_table_name)}")
                    rows = cur.fetchall()
                    
                    for row in rows:
                        for idx, col in enumerate(type_mismatches):
                            val = row[idx+1]
                            success, _ = safe_convert(val, attr_app_types[col])
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
                        if reply == QMessageBox.No: return None, None
                    else:
                        raise RuntimeError(f"Sync aborted: Data type conversion would cause {conversion_failures} object values to be cleared to None. Manual resolution required.")
                            
                cur.execute("PRAGMA foreign_keys")
                fk_state = cur.fetchone()[0]
                
                cur.execute("PRAGMA legacy_alter_table")
                legacy_row = cur.fetchone()
                legacy_state = legacy_row[0] if legacy_row else 0
                
                conn.commit()
                conn.execute("PRAGMA foreign_keys = OFF")
                
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    
                    # Prevent modern SQLite from verifying/modifying views dynamically during ALTER TABLE 
                    conn.execute("PRAGMA legacy_alter_table = ON")
                    
                    # Clear views mapped to the old schema explicitly before renaming/dropping the underlying table
                    cur.execute(f"DROP VIEW IF EXISTS {qid('view_' + safe_table_name)}")
                    cur.execute(f"DROP VIEW IF EXISTS {qid('full_view_' + safe_table_name)}")
                    cur.execute(f"DROP VIEW IF EXISTS {qid('base_view_' + safe_table_name)}")
                    
                    new_table = f"new_{safe_table_name}"
                    cur.execute(f"DROP TABLE IF EXISTS {qid(new_table)}")
                    
                    cols_def = ", ".join([f"{qid(c)} {t}" for c, t in required_cols])
                    cur.execute(f"CREATE TABLE {qid(new_table)} (id INTEGER PRIMARY KEY AUTOINCREMENT{', ' + cols_def if cols_def else ''})")
                    
                    common_cols = list(set(existing_cols.keys()).intersection([c for c, t in required_cols]))
                    if common_cols:
                        escaped_common_cols = ", ".join([qid(c) for c in common_cols])
                        cur.execute(f"SELECT id, {escaped_common_cols} FROM {qid(safe_table_name)}")
                        old_data = cur.fetchall()
                        
                        for row in old_data:
                            row_id = row[0]
                            new_values = [row_id]
                            insert_cols = ["id"]
                            placeholders = ["?"]
                            
                            for idx, col in enumerate(common_cols):
                                val = row[idx+1]
                                new_val = val
                                if col in type_mismatches:
                                    success, conv_val = safe_convert(val, attr_app_types[col])
                                    new_val = conv_val if success else None
                                
                                new_values.append(new_val)
                                insert_cols.append(qid(col))
                                placeholders.append("?")
                                
                            cur.execute(f"INSERT INTO {qid(new_table)} ({', '.join(insert_cols)}) VALUES ({', '.join(placeholders)})", new_values)
                            
                    cur.execute(f"DROP TABLE {qid(safe_table_name)}")
                    cur.execute(f"ALTER TABLE {qid(new_table)} RENAME TO {qid(safe_table_name)}")
                    conn.commit()
                except Exception as e:
                    conn.rollback()
                    raise e
                finally:
                    conn.execute(f"PRAGMA legacy_alter_table = {legacy_state}")
                    conn.execute(f"PRAGMA foreign_keys = {fk_state}")
                
            else:
                for col_name, col_type in required_cols:
                    if col_name not in existing_cols:
                        cur.execute(f"ALTER TABLE {qid(safe_table_name)} ADD COLUMN {qid(col_name)} {col_type}")

        # Outgoing relationships uses show_in_base
        cur.execute("SELECT c.name, r.rel_type, c.id, r.show_in_base FROM relationships r JOIN classes c ON r.target_class = c.id WHERE r.source_class = ? ORDER BY r.row_order", (class_id,))
        outgoing_rels = cur.fetchall()
        
        for target_name, rel_type, target_class_id, show_in_base in outgoing_rels:
            safe_target_name = sanitize_name(target_name)
            cur.execute(f"CREATE TABLE IF NOT EXISTS {qid('objects_' + safe_target_name)} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            cur.execute(f"CREATE VIEW IF NOT EXISTS {qid('base_view_objects_' + safe_target_name)} AS SELECT id AS [ID] FROM {qid('objects_' + safe_target_name)}")
            
            junc_table = f"rel_{safe_table_name}_to_objects_{safe_target_name}"
            cur.execute(f"CREATE TABLE IF NOT EXISTS {qid(junc_table)} (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, target_id INTEGER, FOREIGN KEY(source_id) REFERENCES {qid(safe_table_name)}(id) ON DELETE CASCADE, FOREIGN KEY(target_id) REFERENCES {qid('objects_' + safe_target_name)}(id) ON DELETE CASCADE)")

        # Incoming relationships uses show_in_target
        cur.execute("SELECT c.name, r.rel_type, c.id, r.show_in_target FROM relationships r JOIN classes c ON r.source_class = c.id WHERE r.target_class = ? ORDER BY r.row_order", (class_id,))
        incoming_rels = cur.fetchall()

        for source_name, rel_type, source_class_id, show_in_target in incoming_rels:
            safe_source_name = sanitize_name(source_name)
            cur.execute(f"CREATE TABLE IF NOT EXISTS {qid('objects_' + safe_source_name)} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            cur.execute(f"CREATE VIEW IF NOT EXISTS {qid('base_view_objects_' + safe_source_name)} AS SELECT id AS [ID] FROM {qid('objects_' + safe_source_name)}")
            
            junc_table = f"rel_objects_{safe_source_name}_to_{safe_table_name}"
            cur.execute(f"CREATE TABLE IF NOT EXISTS {qid(junc_table)} (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, target_id INTEGER, FOREIGN KEY(source_id) REFERENCES {qid('objects_' + safe_source_name)}(id) ON DELETE CASCADE, FOREIGN KEY(target_id) REFERENCES {qid(safe_table_name)}(id) ON DELETE CASCADE)")

        base_selects = ["m.id AS [ID]"]
        for attr_name, attr_type, show_in_table, is_title, lookup_query in attributes:
            safe_col_name = sanitize_name(attr_name)
            if attr_type == "look-through":
                if lookup_query:
                    parts = lookup_query.split('.')
                    if len(parts) == 2:
                        tgt_class, tgt_attr = parts
                        safe_tgt_class = sanitize_name(tgt_class.strip())
                        junc_table = f"rel_{safe_table_name}_to_objects_{safe_tgt_class}"
                        subquery = f"(SELECT GROUP_CONCAT(tgt_v.{qid(tgt_attr.strip())}) FROM {qid(junc_table)} j JOIN {qid('base_view_objects_' + safe_tgt_class)} tgt_v ON j.target_id = tgt_v.[ID] WHERE j.source_id = m.id)"
                        base_selects.append(f"{subquery} AS {qid(attr_name)}")
                    else:
                        raise ValueError(f"Invalid lookup format '{lookup_query}'. Expected 'TargetClass.Attribute'.")
                else:
                    base_selects.append(f"NULL AS {qid(attr_name)}")
            else:
                base_selects.append(f"m.{qid(safe_col_name)} AS {qid(attr_name)}")

        base_view_name = f"base_view_{safe_table_name}"
        cur.execute(f"DROP VIEW IF EXISTS {qid(base_view_name)}")
        cur.execute(f"CREATE VIEW {qid(base_view_name)} AS SELECT {', '.join(base_selects)} FROM {qid(safe_table_name)} m")

        ui_selects = ["v.[ID]"]
        full_selects = ["v.[ID]"]

        for attr_name, attr_type, show_in_table, is_title, lookup_query in attributes:
            col_sql = f"v.{qid(attr_name)}"
            if show_in_table: ui_selects.append(col_sql)
            full_selects.append(col_sql)

        for target_name, rel_type, target_class_id, show_in_base in outgoing_rels:
            safe_target_name = sanitize_name(target_name)
            junc_table = f"rel_{safe_table_name}_to_objects_{safe_target_name}"
            
            # ALWAYS process explicitly requested ID columns
            id_display_label = qid(f"{target_name} (IDs)")
            id_query_str = f"(SELECT GROUP_CONCAT(j.target_id) FROM {qid(junc_table)} j WHERE j.source_id = v.[ID]) AS {id_display_label}"
            if show_in_base: ui_selects.append(id_query_str)
            full_selects.append(id_query_str)
            
            # Process Title Columns in addition to the IDs
            cur.execute("SELECT name FROM attributes WHERE class_id = ? AND is_title = 1 ORDER BY row_order", (target_class_id,))
            title_rows = cur.fetchall()
            for (t_name,) in title_rows:
                display_label = qid(f"{target_name} ({t_name})")
                query_str = f"(SELECT GROUP_CONCAT(tgt_v.{qid(t_name)}) FROM {qid(junc_table)} j JOIN {qid('base_view_objects_' + safe_target_name)} tgt_v ON j.target_id = tgt_v.[ID] WHERE j.source_id = v.[ID]) AS {display_label}"
                if show_in_base: ui_selects.append(query_str)
                full_selects.append(query_str)

        for source_name, rel_type, source_class_id, show_in_target in incoming_rels:
            safe_source_name = sanitize_name(source_name)
            junc_table = f"rel_objects_{safe_source_name}_to_{safe_table_name}"
            
            # ALWAYS process explicitly requested ID columns
            id_display_label = qid(f"From {source_name} (IDs)")
            id_query_str = f"(SELECT GROUP_CONCAT(j.source_id) FROM {qid(junc_table)} j WHERE j.target_id = v.[ID]) AS {id_display_label}"
            if show_in_target: ui_selects.append(id_query_str)
            full_selects.append(id_query_str)
            
            # Process Title Columns in addition to the IDs
            cur.execute("SELECT name FROM attributes WHERE class_id = ? AND is_title = 1 ORDER BY row_order", (source_class_id,))
            title_rows = cur.fetchall()
            for (t_name,) in title_rows:
                display_label = qid(f"From {source_name} ({t_name})")
                query_str = f"(SELECT GROUP_CONCAT(src_v.{qid(t_name)}) FROM {qid(junc_table)} j JOIN {qid('base_view_objects_' + safe_source_name)} src_v ON j.source_id = src_v.[ID] WHERE j.target_id = v.[ID]) AS {display_label}"
                if show_in_target: ui_selects.append(query_str)
                full_selects.append(query_str)

        view_name = f"view_{safe_table_name}"
        cur.execute(f"DROP VIEW IF EXISTS {qid(view_name)}")
        cur.execute(f"CREATE VIEW {qid(view_name)} AS SELECT {', '.join(ui_selects)} FROM {qid(base_view_name)} v")

        full_view_name = f"full_view_{safe_table_name}"
        cur.execute(f"DROP VIEW IF EXISTS {qid(full_view_name)}")
        cur.execute(f"CREATE VIEW {qid(full_view_name)} AS SELECT {', '.join(full_selects)} FROM {qid(base_view_name)} v")
    
    return view_name, safe_table_name


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
        
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            
            cur.execute("SELECT id, name, data_type, is_unique, is_required FROM attributes WHERE class_id = ? ORDER BY row_order", (self.class_id,))
            attributes = cur.fetchall()

            for attr_id, attr_name, attr_type, is_unique, is_required in attributes:
                safe_col_name = sanitize_name(attr_name)
                
                if attr_type == "look-through": continue
                    
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
                    widget = QCheckBox("Yes / True")
                elif attr_type == "date":
                    widget = QDateTimeEdit()
                    widget.setCalendarPopup(True) 
                    widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
                elif attr_type == "long string":
                    widget = QTextEdit()
                    widget.setMaximumHeight(100) 
                elif attr_type in ("list", "matrix"):
                    widget = QLineEdit("[]") 
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
                form_layout.addRow(QLabel(label_text), widget)

            cur.execute("""
                SELECT c.id, c.name, r.rel_type 
                FROM relationships r JOIN classes c ON r.target_class = c.id 
                WHERE r.source_class = ? ORDER BY r.row_order
            """, (self.class_id,))
            relationships = cur.fetchall()

            for target_class_id, target_name, rel_type in relationships:
                safe_target_name = sanitize_name(target_name)
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
                    
                self.rel_widgets[junc_table] = {"widget": list_widget, "rel_type": rel_type, "target_name": target_name}
                form_layout.addRow(f"Rel: {target_name}:", rel_container)

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

    def save_record(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("PRAGMA foreign_keys = 1")
                cur = conn.cursor()
                
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

                try:
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
                    self.accept()
                except Exception as e:
                    conn.rollback()
                    raise e
            
        except Exception as e:
            QMessageBox.critical(self, "Database Error", str(e))


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Database Settings")
        self.setWindowIcon(get_app_icon())
        self.resize(700, 250)
        self.settings = QSettings("MyCompany", "DatabaseManagerApp")
        
        main_layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        
        db_tab = QWidget()
        db_layout = QVBoxLayout(db_tab)
        db_layout.addWidget(QLabel("Database Path:"))

        path_layout = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setText(self.settings.value("db_path", ""))
        
        btn_browse = QPushButton(" Browse...")
        btn_browse.setIcon(qta.icon('fa5s.folder-open'))
        btn_browse.clicked.connect(self.browse_file)
        
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(btn_browse)
        db_layout.addLayout(path_layout)
        
        btn_db_layout = QHBoxLayout()
        btn_db_layout.addStretch()
        btn_db_save = QPushButton(" Save")
        btn_db_save.setIcon(qta.icon('fa5s.save'))
        btn_db_save.clicked.connect(self.save_db_settings)
        btn_db_layout.addWidget(btn_db_save)
        
        db_layout.addStretch()
        db_layout.addLayout(btn_db_layout)
        self.tabs.addTab(db_tab, "Database")

        mod_tab = QWidget()
        mod_layout = QVBoxLayout(mod_tab)
        mod_layout.addWidget(QLabel("Module Output File Path:"))
        
        out_layout = QHBoxLayout()
        self.output_input = QLineEdit()
        
        db_path = self.settings.value("db_path", "")
        if db_path and os.path.exists(os.path.dirname(db_path)):
            default_out = os.path.join(os.path.dirname(db_path), "module_output.txt")
        else:
            default_out = os.path.join(os.path.expanduser("~"), "module_output.txt")
            
        self.output_input.setText(self.settings.value("module_output_path", default_out))
        
        btn_browse_out = QPushButton(" Browse...")
        btn_browse_out.setIcon(qta.icon('fa5s.folder-open'))
        btn_browse_out.clicked.connect(self.browse_output_file)
        
        out_layout.addWidget(self.output_input)
        out_layout.addWidget(btn_browse_out)
        mod_layout.addLayout(out_layout)
        
        btn_mod_layout = QHBoxLayout()
        btn_mod_layout.addStretch()
        btn_mod_save = QPushButton(" Save")
        btn_mod_save.setIcon(qta.icon('fa5s.save'))
        btn_mod_save.clicked.connect(self.save_module_path)
        btn_mod_layout.addWidget(btn_mod_save)
        
        mod_layout.addStretch()
        mod_layout.addLayout(btn_mod_layout)
        self.tabs.addTab(mod_tab, "Modules")

    def browse_file(self):
        file_name, _ = QFileDialog.getOpenFileName(self, "Select SQLite Database", "", "SQLite DB (*.db *.sqlite)")
        if file_name: self.path_input.setText(file_name)

    def browse_output_file(self):
        file_name, _ = QFileDialog.getSaveFileName(self, "Select Output File", "", "Text Files (*.txt)")
        if file_name: self.output_input.setText(file_name)

    def save_db_settings(self):
        path = self.path_input.text().strip()
        if not path: return
        if not os.path.exists(path): init_db(path)
        self.settings.setValue("db_path", path)
        QMessageBox.information(self, "Success", "Database path saved as default.")
        
    def save_module_path(self):
        self.settings.setValue("module_output_path", self.output_input.text().strip())
        QMessageBox.information(self, "Success", "Module output path saved.")


class ModuleBuilderDialog(QDialog):
    def __init__(self, db_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Module Builder")
        self.setWindowIcon(get_app_icon())
        self.resize(1000, 700)
        self.db_path = db_path
        self.current_module_id = None
        
        if pd is None:
            QMessageBox.warning(self, "Missing Library", "The 'pandas' library is required to use the Module Builder and return DataFrames.\n\nPlease install it via terminal: pip install pandas")
        
        main_layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter)
        
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        self.module_list_widget = QTreeWidget()
        self.module_list_widget.setHeaderHidden(True)
        self.module_list_widget.itemClicked.connect(self.load_module)
        
        btn_add = QPushButton(" Create New Module")
        btn_add.setIcon(qta.icon('fa5s.plus-circle'))
        btn_add.clicked.connect(self.create_new_module)
        
        left_layout.addWidget(QLabel("<b>Saved Modules</b>"))
        left_layout.addWidget(self.module_list_widget)
        left_layout.addWidget(btn_add)
        
        self.editor_widget = QWidget()
        self.editor_layout = QVBoxLayout(self.editor_widget)
        self.editor_widget.setEnabled(False)
        
        info_layout = QHBoxLayout()
        self.module_name_input = QLineEdit()
        self.module_name_input.setPlaceholderText("Module Name")
        self.module_path_input = QLineEdit()
        self.module_path_input.setPlaceholderText("e.g. Reports/Inventory")
        info_layout.addWidget(QLabel("Name:"))
        info_layout.addWidget(self.module_name_input)
        info_layout.addWidget(QLabel("Path:"))
        info_layout.addWidget(self.module_path_input)
        self.editor_layout.addLayout(info_layout)
        
        self.editor = QPlainTextEdit()
        font = self.editor.font()
        font.setFamily("Courier New")
        font.setPointSize(11)
        font.setStyleHint(QFont.Monospace)
        self.editor.setFont(font)
        
        self.editor_layout.addWidget(QLabel("Python Script Editor:"))
        self.editor_layout.addWidget(self.editor)
        
        btn_layout = QHBoxLayout()
        
        btn_delete = QPushButton(" Delete Module")
        btn_delete.setIcon(qta.icon('fa5s.trash-alt', color='white'))
        btn_delete.setStyleSheet("background-color: #ff4c4c; color: white;")
        btn_delete.clicked.connect(self.delete_module)
        
        btn_save = QPushButton(" Save Module")
        btn_save.setIcon(qta.icon('fa5s.save'))
        btn_save.clicked.connect(self.save_module)
        
        btn_run = QPushButton(" Run Script")
        btn_run.setIcon(qta.icon('fa5s.play', color='white'))
        btn_run.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 5px 15px;")
        btn_run.clicked.connect(self.run_script)
        
        btn_layout.addWidget(btn_delete)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_save)
        btn_layout.addWidget(btn_run)
        self.editor_layout.addLayout(btn_layout)

        splitter.addWidget(left_widget)
        splitter.addWidget(self.editor_widget)
        splitter.setSizes([250, 750])
        
        self.refresh_module_list()

    def get_all_modules(self):
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS modules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    path TEXT DEFAULT '',
                    code TEXT DEFAULT ''
                )
            """)
            conn.commit()
            cur.execute("SELECT id, name, path FROM modules ORDER BY path ASC, name ASC")
            return cur.fetchall()

    def refresh_module_list(self):
        self.module_list_widget.clear()
        root_items = {}
        parent_item = self.module_list_widget.invisibleRootItem()
        
        for mid, mname, mpath in self.get_all_modules():
            mpath = (mpath or "").strip().strip('/')
            current_parent = parent_item
            
            if mpath:
                current_path = ""
                for part in mpath.split('/'):
                    part = part.strip()
                    if not part: continue
                    current_path = f"{current_path}/{part}" if current_path else part
                    
                    if current_path not in root_items:
                        folder_item = QTreeWidgetItem([part])
                        folder_item.setIcon(0, qta.icon('fa5s.folder', color='#FFC107'))
                        current_parent.addChild(folder_item)
                        root_items[current_path] = folder_item
                    
                    current_parent = root_items[current_path]
            
            item = QTreeWidgetItem([mname])
            item.setIcon(0, qta.icon('fa5s.file-code', color='#2196F3'))
            item.setData(0, Qt.UserRole, mid)
            current_parent.addChild(item)
            
        self.module_list_widget.expandAll()

    def create_new_module(self):
        self.current_module_id = None
        self.module_name_input.clear()
        self.module_path_input.clear()
        placeholder = (
            "# Write your Python script here.\n"
            "# Built-in Helper API:\n"
            "#   df = get_objects('ClassName') -> Returns a pandas DataFrame of the class view.\n\n"
            "print('Hello from Module Builder!')\n"
        )
        self.editor.setPlainText(placeholder)
        self.editor_widget.setEnabled(True)

    def load_module(self, item, column=0):
        mid = item.data(0, Qt.UserRole)
        if not mid: return
        
        self.current_module_id = mid
        self.editor_widget.setEnabled(True)
        
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, path, code FROM modules WHERE id = ?", (mid,))
            row = cur.fetchone()
            
            if row:
                self.module_name_input.setText(row[0])
                self.module_path_input.setText(row[1] if row[1] else "")
                self.editor.setPlainText(row[2] if row[2] else "")

    def save_module(self):
        name = self.module_name_input.text().strip()
        path_val = self.module_path_input.text().strip()
        code = self.editor.toPlainText()
        
        if not name:
            QMessageBox.warning(self, "Error", "Module name cannot be empty.")
            return
            
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                if self.current_module_id is None:
                    cur.execute("INSERT INTO modules (name, path, code) VALUES (?, ?, ?)", (name, path_val, code))
                    self.current_module_id = cur.lastrowid
                else:
                    cur.execute("UPDATE modules SET name = ?, path = ?, code = ? WHERE id = ?", (name, path_val, code, self.current_module_id))
                conn.commit()
            
            self.refresh_module_list()
            QMessageBox.information(self, "Success", "Module saved successfully!")
        except sqlite3.IntegrityError:
            QMessageBox.warning(self, "Error", "A module with this name already exists.")

    def delete_module(self):
        if self.current_module_id is None: return
        reply = QMessageBox.question(self, "Delete", "Are you sure you want to delete this module?", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM modules WHERE id = ?", (self.current_module_id,))
                conn.commit()
                
            self.current_module_id = None
            self.editor_widget.setEnabled(False)
            self.module_name_input.clear()
            self.module_path_input.clear()
            self.editor.clear()
            self.refresh_module_list()

    def get_objects(self, class_name):
        if pd is None: raise ImportError("Pandas library is not installed.")
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM classes WHERE name = ?", (class_name,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Class '{class_name}' not found in the database.")
            cls_id = row[0]
            
        view_name, safe_table_name = sync_physical_table(self.db_path, cls_id, class_name, parent_widget=None)
        if not view_name: raise RuntimeError(f"Failed to sync schema for class '{class_name}'.")
            
        full_view_name = f"full_view_{safe_table_name}"
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql_query(f"SELECT * FROM {qid(full_view_name)}", conn)
        return df

    def run_script(self):
        code = self.editor.toPlainText()
        settings = QSettings("MyCompany", "DatabaseManagerApp")
        out_path = settings.value("module_output_path", os.path.join(os.path.expanduser("~"), "module_output.txt"))
        context = {'get_objects': self.get_objects, 'pd': pd}
        
        try:
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(f"--- Script Executed at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n\n")
                with redirect_stdout(f), redirect_stderr(f): exec(code, context)
            QMessageBox.information(self, "Success", f"Script executed successfully.\nOutput saved to:\n{out_path}")
        except Exception as e:
            with open(out_path, 'a', encoding='utf-8') as f:
                f.write("\n\n--- RUNTIME ERROR ---\n")
                traceback.print_exc(file=f)
            QMessageBox.warning(self, "Script Error", f"An error occurred during execution.\nCheck the output file for details:\n{out_path}")


class DataBrowserPage(QWidget):
    def __init__(self, main_window): 
        super().__init__()
        self.main_window = main_window
        self.current_class_id = None
        self.current_class_name = None
        
        self.current_view_name = None
        self.current_table_name = None
        self.db_path = ""
        self.hidden_columns_memory = {}
        
        self.page_size = 100
        self.current_page = 0
        self.total_pages = 0
        self.total_records = 0
        self.current_sort_col = "ID"
        self.current_sort_order = "ASC"
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 9, 9, 9)

        self.header_label = QLabel("<h2>&nbsp;</h2>")
        layout.addWidget(self.header_label)

        search_layout = QHBoxLayout()
        self.col_combo = QComboBox()
        self.col_combo.addItem("All Columns", -1)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter text...")
        self.search_input.returnPressed.connect(self.trigger_search)
        
        self.btn_search = QPushButton(" Search") 
        self.btn_search.setIcon(qta.icon('fa5s.search'))
        self.btn_search.clicked.connect(self.trigger_search)
        
        self.btn_import = QPushButton(" Import")
        self.btn_import.setIcon(qta.icon('fa5s.file-import'))
        self.btn_import.clicked.connect(self.import_data)
        
        self.btn_export = QPushButton(" Export")
        self.btn_export.setIcon(qta.icon('fa5s.file-export'))
        self.btn_export.clicked.connect(self.export_data)
        
        self.btn_add = QPushButton(" Add Object")
        self.btn_add.setIcon(qta.icon('fa5s.plus'))
        
        self.btn_edit = QPushButton(" Edit")
        self.btn_edit.setIcon(qta.icon('fa5s.edit'))
        
        self.btn_delete = QPushButton(" Delete")
        self.btn_delete.setIcon(qta.icon('fa5s.trash-alt'))
        
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
        header.setContextMenuPolicy(Qt.CustomContextMenu)
        header.customContextMenuRequested.connect(self.show_header_context_menu)
        
        layout.addWidget(self.table_view)

        self.query_model = QSqlQueryModel()
        self.table_view.setModel(self.query_model)
        self.table_view.selectionModel().selectionChanged.connect(self.on_selection_changed)

        page_layout = QHBoxLayout()
        
        self.btn_prev = QPushButton(" Prev") 
        self.btn_prev.setIcon(qta.icon('fa5s.chevron-left'))
        self.btn_prev.clicked.connect(self.prev_page)
        
        self.page_label = QLabel("Page 0 of 0 (Total: 0)")
        self.page_label.setAlignment(Qt.AlignCenter)
        
        self.btn_next = QPushButton("Next ") 
        self.btn_next.setIcon(qta.icon('fa5s.chevron-right'))
        self.btn_next.setLayoutDirection(Qt.RightToLeft)
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
        
        try:
            self.current_view_name, self.current_table_name = sync_physical_table(self.db_path, class_id, class_name, parent_widget=self)
            
            if not self.current_view_name:
                raise RuntimeError("Sync aborted or failed to generate a valid view.")
                
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute(f"PRAGMA table_info({qid(self.current_view_name)})")
                columns = [row[1] for row in cur.fetchall()]
            
            if not columns:
                raise sqlite3.OperationalError(f"View '{self.current_view_name}' is broken and returned no columns.")
                
        except Exception as e:
            QMessageBox.critical(self, "Schema Load Error", f"A database operational error occurred while loading '{class_name}'.\n\nThis is usually caused by broken 'Look-through' attributes referencing deleted or missing columns.\n\nDetails:\n{str(e)}")
            self.header_label.setText("<h2>Sync Aborted</h2>")
            self.btn_import.setEnabled(False)
            self.btn_add.setEnabled(False)
            return
            
        self.search_input.clear()
        self.current_sort_col = "ID"
        self.current_sort_order = "ASC"
        self.current_page = 0
        
        header = self.table_view.horizontalHeader()
        header.blockSignals(True)
        header.setSortIndicator(0, Qt.AscendingOrder)
        header.blockSignals(False)

        self.col_combo.blockSignals(True)
        self.col_combo.clear()
        self.col_combo.addItem("All Columns", -1)
        for i, col_name in enumerate(columns):
            self.col_combo.addItem(col_name, i)
        self.col_combo.blockSignals(False)
        
        self.build_and_exec_query()

    def build_and_exec_query(self):
        if not self.current_view_name: return
        
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            
            base_query = f"FROM {qid(self.current_view_name)}"
            where_clauses = []
            params = []
            
            search_text = self.search_input.text().strip()
            if search_text:
                col_idx = self.col_combo.currentData()
                if col_idx == -1: 
                    cur.execute(f"PRAGMA table_info({qid(self.current_view_name)})")
                    col_names = [row[1] for row in cur.fetchall()]
                    or_clauses = [f"{qid(c)} LIKE ?" for c in col_names]
                    where_clauses.append("(" + " OR ".join(or_clauses) + ")")
                    params.extend([f"%{search_text}%"] * len(col_names))
                else: 
                    col_name = self.col_combo.itemText(self.col_combo.currentIndex())
                    where_clauses.append(f"{qid(col_name)} LIKE ?")
                    params.append(f"%{search_text}%")
            
            where_sql = ""
            if where_clauses: where_sql = " WHERE " + " AND ".join(where_clauses)
                
            count_query = f"SELECT COUNT(*) {base_query} {where_sql}"
            cur.execute(count_query, params)
            self.total_records = cur.fetchone()[0]
            self.total_pages = max(1, (self.total_records + self.page_size - 1) // self.page_size)
            
            if self.current_page >= self.total_pages:
                self.current_page = max(0, self.total_pages - 1)
                
            offset = self.current_page * self.page_size
            order_sql = f"ORDER BY {qid(self.current_sort_col)} {self.current_sort_order}"
            final_query = f"SELECT * {base_query} {where_sql} {order_sql} LIMIT {self.page_size} OFFSET {offset}"
            
            db = QSqlDatabase.database()
            query = QSqlQuery(db)
            query.prepare(final_query)
            for p in params: query.addBindValue(p)
            
            query.exec()
            if query.lastError().isValid():
                QMessageBox.critical(self, "Database Query Error", f"An error occurred while fetching data from the view:\n\n{query.lastError().text()}")
                
            self.query_model.setQuery(query)
            
            hidden_for_class = self.hidden_columns_memory.get(self.current_class_name, set())
            for i in range(self.query_model.columnCount()):
                col_name = self.query_model.headerData(i, Qt.Horizontal)
                if col_name in hidden_for_class:
                    self.table_view.setColumnHidden(i, True)
                else:
                    self.table_view.setColumnHidden(i, False)
            
            self.page_label.setText(f"Page {self.current_page + 1} of {self.total_pages} (Total: {self.total_records})")
            self.btn_prev.setEnabled(self.current_page > 0)
            self.btn_next.setEnabled(self.current_page < self.total_pages - 1)

    def on_sort_changed(self, logical_index, order):
        col_name = self.query_model.headerData(logical_index, Qt.Horizontal)
        if not col_name: return 
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

    def show_header_context_menu(self, position):
        header = self.table_view.horizontalHeader()
        logical_index = header.logicalIndexAt(position)
        menu = QMenu()
        
        act_hide = None
        if logical_index >= 0:
            col_name = self.query_model.headerData(logical_index, Qt.Horizontal)
            if col_name:
                visible_count = sum(1 for i in range(header.count()) if not self.table_view.isColumnHidden(i))
                if visible_count > 1: act_hide = menu.addAction(f"Hide '{col_name}' Temporarily")
        
        hidden_cols = [i for i in range(header.count()) if self.table_view.isColumnHidden(i)]
        act_unhide = None
        if hidden_cols:
            if act_hide: menu.addSeparator()
            act_unhide = menu.addAction("Unhide All Columns")
            
        if not menu.actions(): return
            
        action = menu.exec(header.viewport().mapToGlobal(position))
        
        if act_hide and action == act_hide:
            self.table_view.setColumnHidden(logical_index, True)
            col_name = self.query_model.headerData(logical_index, Qt.Horizontal)
            if col_name: self.hidden_columns_memory.setdefault(self.current_class_name, set()).add(col_name)
        elif act_unhide and action == act_unhide:
            for i in range(header.count()): self.table_view.setColumnHidden(i, False)
            if self.current_class_name in self.hidden_columns_memory:
                self.hidden_columns_memory[self.current_class_name].clear()

    def show_context_menu(self, position):
        if not self.table_view.selectionModel().selectedRows(): return
        from PySide6.QtWidgets import QMenu
        menu = QMenu()
        act_edit = menu.addAction(qta.icon('fa5s.edit'), "Edit Object")
        act_del = menu.addAction(qta.icon('fa5s.trash-alt'), "Delete Object")
        
        action = menu.exec(self.table_view.viewport().mapToGlobal(position))
        if action == act_edit: self.open_edit_dialog()
        elif action == act_del: self.delete_selected()

    def open_add_dialog(self):
        dialog = ObjectEditorDialog(self.db_path, self.current_class_id, self.current_class_name, self.current_table_name, None, self)
        if dialog.exec(): self.build_and_exec_query() 
            
    def open_edit_dialog(self):
        obj_id = self.get_selected_id()
        if not obj_id: return
        dialog = ObjectEditorDialog(self.db_path, self.current_class_id, self.current_class_name, self.current_table_name, obj_id, self)
        if dialog.exec(): self.build_and_exec_query() 
            
    def delete_selected(self):
        obj_id = self.get_selected_id()
        if not obj_id: return
        reply = QMessageBox.question(self, "Confirm Delete", f"Are you sure you want to delete this {self.current_class_name}?\n\nIts relationships will be automatically safely removed.", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("PRAGMA foreign_keys = 1") 
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(f"DELETE FROM {qid(self.current_table_name)} WHERE id = ?", (obj_id,))
                    conn.commit()
                    
                self.build_and_exec_query()
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def export_data(self):
        if not self.query_model: return
        file_path, selected_filter = QFileDialog.getSaveFileName(self, "Export Data", "", "CSV Files (*.csv);;Excel Files (*.xlsx)")
        if not file_path: return
        
        if "csv" in selected_filter.lower() and not file_path.lower().endswith(".csv"): file_path += ".csv"
        elif "excel" in selected_filter.lower() and not file_path.lower().endswith(".xlsx"): file_path += ".xlsx"
            
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(f"PRAGMA table_info({qid(self.current_view_name)})")
            headers = [row[1] for row in cur.fetchall()]
            
            cur.execute(f"SELECT * FROM {qid(self.current_view_name)} ORDER BY {qid(self.current_sort_col)} {self.current_sort_order}")
            raw_data = cur.fetchall()
        
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
                for row in data: ws.append(row)
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
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                
                cur.execute("SELECT id, name, data_type, is_unique, is_required FROM attributes WHERE class_id = ?", (self.current_class_id,))
                attributes_meta = {}
                for attr_id, name, d_type, is_uniq, is_req in cur.fetchall():
                    if d_type == "look-through": continue
                    
                    safe_name = sanitize_name(name)
                    attributes_meta[safe_name] = {
                        "safe_col": safe_name,
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
                    matrix_counts[sanitize_name(attr_name)] = col_count
                    
                cur.execute("""
                    SELECT c.name, r.rel_type, c.id 
                    FROM relationships r JOIN classes c ON r.target_class = c.id 
                    WHERE r.source_class = ?
                """, (self.current_class_id,))
                relationships_meta = {}
                for t_name, r_type, t_class_id in cur.fetchall():
                    safe_target_name = sanitize_name(t_name)
                    relationships_meta[safe_target_name] = {
                        "target_table": f"objects_{safe_target_name}",
                        "junc_table": f"rel_{self.current_table_name}_to_objects_{safe_target_name}"
                    }

                existing_unique_values = {}
                for name_safe, meta in attributes_meta.items():
                    if meta["unique"]:
                        cur.execute(f"SELECT {qid(meta['safe_col'])} FROM {qid(self.current_table_name)} WHERE {qid(meta['safe_col'])} IS NOT NULL")
                        existing_unique_values[meta["safe_col"]] = set(row[0] for row in cur.fetchall())

                wb = openpyxl.load_workbook(file_path, data_only=True)
                ws = wb.active
                
                all_rows = list(ws.iter_rows(values_only=True))
                if len(all_rows) < 2: raise ValueError("Excel file must contain at least a header row and one data row.")
                    
                headers = [sanitize_name(str(h)) if h else "" for h in all_rows[0]]
                
                for name_safe, meta in attributes_meta.items():
                    if meta["required"] and name_safe not in headers:
                        raise ValueError(f"CRITICAL: The required attribute '{meta['safe_col']}' is completely missing from the Excel headers!")

                in_file_unique_tracker = {meta["safe_col"]: set() for meta in attributes_meta.values() if meta["unique"]}
                validated_inserts = []
                validated_relationships = []

                for row_number, row_data in enumerate(all_rows[1:], start=2): 
                    if all(cell is None or str(cell).strip() == "" for cell in row_data): continue 

                    insert_cols = []
                    insert_vals = []
                    pending_rels = {} 
                    
                    for col_idx, cell_value in enumerate(row_data):
                        if col_idx >= len(headers): break
                        header = headers[col_idx]
                        if not header: continue
                        
                        if isinstance(cell_value, datetime.datetime): val = cell_value.strftime("%Y-%m-%d %H:%M:%S")
                        else: val = None if (cell_value is None or str(cell_value).strip() == "") else str(cell_value).strip()
                        
                        is_empty = (val is None)

                        if header in attributes_meta:
                            meta = attributes_meta[header]
                            safe_col = meta["safe_col"]
                            
                            if is_empty:
                                if meta["required"]: raise ValueError(f"Row {row_number}: '{header}' is Required but cell is empty.")
                                val_final = None
                            else:
                                success, val_final = safe_convert(val, meta["type"])
                                if not success: 
                                    raise ValueError(f"Row {row_number}: Invalid data formatting in '{header}'. Cannot convert '{val}'.")
                                    
                            if meta["unique"] and val_final is not None:
                                if val_final in existing_unique_values[safe_col]:
                                    raise ValueError(f"Row {row_number}: '{header}' is Unique. The value '{val_final}' already exists in the database.")
                                if val_final in in_file_unique_tracker[safe_col]:
                                    raise ValueError(f"Row {row_number}: '{header}' is Unique. The value '{val_final}' is duplicated inside the Excel file.")
                                in_file_unique_tracker[safe_col].add(val_final)

                            insert_cols.append(qid(safe_col))
                            insert_vals.append(val_final)

                        elif header in relationships_meta:
                            if not is_empty:
                                rel_meta = relationships_meta[header]
                                try: target_ids = [int(x.strip()) for x in val.split(",")]
                                except ValueError: raise ValueError(f"Row {row_number}: Relationship '{header}' must be comma-separated numeric IDs.")
                                    
                                for tid in target_ids:
                                    cur.execute(f"SELECT id FROM {qid(rel_meta['target_table'])} WHERE id = ?", (tid,))
                                    if not cur.fetchone():
                                        raise ValueError(f"Row {row_number}: Invalid Target ID '{tid}' for relationship '{header}'. Object doesn't exist.")
                                
                                pending_rels[rel_meta["junc_table"]] = target_ids

                    validated_inserts.append((insert_cols, insert_vals))
                    validated_relationships.append(pending_rels)

                # Process verified inserts directly
                conn.execute("BEGIN IMMEDIATE")
                records_imported = 0
                for i, (cols, vals) in enumerate(validated_inserts):
                    if not cols:
                        cur.execute(f"INSERT INTO {qid(self.current_table_name)} DEFAULT VALUES")
                    else:
                        placeholders = ", ".join(["?"] * len(cols))
                        query = f"INSERT INTO {qid(self.current_table_name)} ({', '.join(cols)}) VALUES ({placeholders})"
                        cur.execute(query, vals)
                        
                    new_obj_id = cur.lastrowid
                    records_imported += 1
                    
                    rels_to_insert = validated_relationships[i]
                    for junc_table, target_ids in rels_to_insert.items():
                        for tid in target_ids: cur.execute(f"INSERT INTO {qid(junc_table)} (source_id, target_id) VALUES (?, ?)", (new_obj_id, tid))
                
                conn.commit()
                self.build_and_exec_query() 
                QMessageBox.information(self, "Import Successful", f"Successfully imported {records_imported} object(s)!")
            
        except Exception as e:
            QMessageBox.critical(self, "Import Failed", f"Import aborted. No changes were made to the database.\n\nReason:\n{str(e)}")


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nexus")
        self.setWindowIcon(get_app_icon())
        self.resize(1200, 768)

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)

        self.setup_menu()

        splitter = QSplitter(Qt.Horizontal)
        self.main_layout.addWidget(splitter)

        # ---------------------------------------------
        # LEFT PANEL: Splitting Classes and Modules
        # ---------------------------------------------
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(9, 9, 9, 9)
        
        # Dummy header to perfectly align the top of the sidebars with the search layout
        dummy_header = QLabel("<h2>&nbsp;</h2>")
        left_layout.addWidget(dummy_header)
        
        left_splitter = QSplitter(Qt.Vertical)
        
        # 1. Classes Tree (Headers removed)
        self.sidebar = QTreeWidget()
        self.sidebar.setHeaderHidden(True)
        self.sidebar.itemClicked.connect(self.change_class)
        left_splitter.addWidget(self.sidebar)

        # 2. Modules Tree (Headers removed)
        self.module_sidebar = QTreeWidget()
        self.module_sidebar.setHeaderHidden(True)
        self.module_sidebar.setContextMenuPolicy(Qt.CustomContextMenu)
        self.module_sidebar.customContextMenuRequested.connect(self.show_module_context_menu)
        left_splitter.addWidget(self.module_sidebar)

        # Allocate 75% height to classes, 25% to modules
        left_splitter.setSizes([750, 250])
        left_layout.addWidget(left_splitter)
        
        # Dummy footer to perfectly align the bottom of the sidebars with the table view bottom
        dummy_footer_layout = QHBoxLayout()
        dummy_btn = QPushButton(" ")
        dummy_btn.setFlat(True)
        dummy_btn.setEnabled(False)
        dummy_btn.setStyleSheet("background: transparent; color: transparent; border: none;")
        dummy_footer_layout.addWidget(dummy_btn)
        left_layout.addLayout(dummy_footer_layout)
        
        splitter.addWidget(left_widget)

        # ---------------------------------------------
        # RIGHT PANEL: Data Browser
        # ---------------------------------------------
        self.data_browser = DataBrowserPage(self)
        splitter.addWidget(self.data_browser)
        
        splitter.setSizes([250, 950])
        self.refresh_sidebar()

    def setup_menu(self):
        menubar = QMenuBar(self)
        self.main_layout.setMenuBar(menubar)
        
        db_menu = menubar.addMenu("Database")

        action_settings = QAction(qta.icon('fa5s.cog'), " Settings", self)
        action_settings.triggered.connect(self.open_settings)
        db_menu.addAction(action_settings)

        action_builder = QAction(qta.icon('fa5s.tools'), " Class Builder", self)
        action_builder.triggered.connect(self.open_builder)
        db_menu.addAction(action_builder)
        
        action_module = QAction(qta.icon('fa5s.file-code'), " Module Builder", self)
        action_module.triggered.connect(self.open_module_builder)
        db_menu.addAction(action_module)

        doc_menu = menubar.addMenu("Documentation")
        action_api = QAction(qta.icon('fa5s.book'), " Module API", self)
        action_api.triggered.connect(self.open_module_api)
        doc_menu.addAction(action_api)

    def open_settings(self):
        dialog = SettingsDialog(self)
        dialog.exec()
        self.refresh_sidebar()

    def open_builder(self):
        dialog = ClassBuilderDialog(self)
        dialog.exec()
        self.refresh_sidebar() 

    def open_module_builder(self):
        db_path = QSettings("MyCompany", "DatabaseManagerApp").value("db_path", "")
        dialog = ModuleBuilderDialog(db_path, self)
        dialog.exec()
        self.refresh_module_sidebar()
        
    def open_module_api(self):
        QMessageBox.information(self, "Module API", "Documentation coming soon...\n\nCurrently available built-in functions:\n\nget_objects(class_name)\n-> Returns a complete Pandas DataFrame of the specified class, including automatically resolved multi-title relationships.")

    def sync_all_classes(self, db_path):
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            
            try:
                cur.execute("SELECT id, name FROM classes")
                classes = {row[0]: row[1] for row in cur.fetchall()}
            except sqlite3.OperationalError:
                classes = {}
                
            dependencies = {cid: set() for cid in classes}
            try:
                cur.execute("SELECT class_id, lookup_query FROM attributes WHERE data_type = 'look-through' AND lookup_query != ''")
                for cid, lookup in cur.fetchall():
                    if lookup:
                        tgt_class_name = lookup.split('.')[0].strip().lower()
                        for t_id, t_name in classes.items():
                            if t_name.lower() == tgt_class_name: dependencies[cid].add(t_id)
            except sqlite3.OperationalError:
                pass 
                
        ordered_cids = []
        visited = set()
        temp_mark = set()
        cycle_detected = False
        
        def visit(n):
            if n in temp_mark: return False 
            if n not in visited:
                temp_mark.add(n)
                for m in dependencies.get(n, set()):
                    if m in classes and not visit(m): return False
                temp_mark.remove(n)
                visited.add(n)
                ordered_cids.append(n)
            return True
            
        for cid in classes:
            if cid not in visited:
                if not visit(cid):
                    cycle_detected = True
                    break
                    
        if cycle_detected: ordered_cids = list(classes.keys())
        
        sync_errors = []
        for cid in ordered_cids:
            try:
                sync_physical_table(db_path, cid, classes[cid], parent_widget=self)
            except Exception as e:
                sync_errors.append(f"Class '{classes[cid]}': {str(e)}")
                
        if sync_errors:
            error_msg = "The following configuration errors were detected during sync. Some views might not load perfectly until you fix their schemas:\n\n" + "\n".join(sync_errors)
            QMessageBox.warning(self, "Database Schema Warnings", error_msg)

    def refresh_sidebar(self):
        self.sidebar.clear()
        settings = QSettings("MyCompany", "DatabaseManagerApp")
        db_path = settings.value("db_path", "")
        
        if not db_path:
            db_path = os.path.join(os.path.expanduser("~"), "nexus_default.db")
            settings.setValue("db_path", db_path)
            
        if not os.path.exists(db_path): init_db(db_path)

        if QSqlDatabase.contains(): db = QSqlDatabase.database()
        else: db = QSqlDatabase.addDatabase("QSQLITE")
        
        db.setDatabaseName(db_path)
        if not db.open(): return

        self.sync_all_classes(db_path)

        try:
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
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
                                folder_item = QTreeWidgetItem([part])
                                folder_item.setIcon(0, qta.icon('fa5s.folder', color='#FFC107'))
                                current_parent.addChild(folder_item)
                                root_items[current_path] = folder_item
                            
                            current_parent = root_items[current_path]
                    
                    class_item = QTreeWidgetItem([c_name])
                    class_item.setIcon(0, qta.icon('fa5s.file-alt', color='#4CAF50'))
                    class_item.setData(0, Qt.UserRole, c_id)
                    current_parent.addChild(class_item)
                    
                self.sidebar.expandAll()
            
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e).lower():
                print(f"Sidebar loading error: {e}")
            
        self.refresh_module_sidebar()

    def refresh_module_sidebar(self):
        self.module_sidebar.clear()
        settings = QSettings("MyCompany", "DatabaseManagerApp")
        db_path = settings.value("db_path", "")
        if not db_path or not os.path.exists(db_path): return
        
        try:
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT id, name, path FROM modules ORDER BY path ASC, name ASC")
                root_items = {}
                parent_item = self.module_sidebar.invisibleRootItem()
                
                for mid, mname, mpath in cur.fetchall():
                    mpath = (mpath or "").strip().strip('/')
                    current_parent = parent_item
                    
                    if mpath:
                        current_path = ""
                        for part in mpath.split('/'):
                            part = part.strip()
                            if not part: continue
                            current_path = f"{current_path}/{part}" if current_path else part
                            
                            if current_path not in root_items:
                                folder_item = QTreeWidgetItem([part])
                                folder_item.setIcon(0, qta.icon('fa5s.folder', color='#FFC107'))
                                current_parent.addChild(folder_item)
                                root_items[current_path] = folder_item
                            current_parent = root_items[current_path]
                    
                    item = QTreeWidgetItem([mname])
                    item.setIcon(0, qta.icon('fa5s.file-code', color='#2196F3'))
                    item.setData(0, Qt.UserRole, mid)
                    current_parent.addChild(item)
                    
                self.module_sidebar.expandAll()
        except sqlite3.OperationalError:
            pass

    def change_class(self, item, column):
        class_id = item.data(0, Qt.UserRole)
        if class_id:
            class_name = item.text(0)
            self.data_browser.load_table_data(class_id, class_name)

    def show_module_context_menu(self, position):
        item = self.module_sidebar.itemAt(position)
        if not item: return
        mid = item.data(0, Qt.UserRole)
        if not mid: return 
        
        menu = QMenu()
        act_run = menu.addAction(qta.icon('fa5s.play', color='green'), "Run Script")
        act_edit = menu.addAction(qta.icon('fa5s.edit'), "Edit Script in Builder")
        
        action = menu.exec(self.module_sidebar.viewport().mapToGlobal(position))
        if action == act_run:
            self.run_module_by_id(mid)
        elif action == act_edit:
            self.open_module_builder()

    def run_module_by_id(self, mid):
        settings = QSettings("MyCompany", "DatabaseManagerApp")
        db_path = settings.value("db_path", "")
        
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, code FROM modules WHERE id = ?", (mid,))
            row = cur.fetchone()
        
        if not row or not row[1]:
            QMessageBox.warning(self, "Warning", "Module script is empty or missing.")
            return
            
        mname, code = row
        out_path = settings.value("module_output_path", os.path.join(os.path.expanduser("~"), "module_output.txt"))
        
        def local_get_objects(class_name):
            if pd is None: raise ImportError("Pandas library is not installed.")
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT id FROM classes WHERE name = ?", (class_name,))
                r = cur.fetchone()
                if not r:
                    raise ValueError(f"Class '{class_name}' not found.")
                cls_id = r[0]
                
            view_name, safe_table_name = sync_physical_table(db_path, cls_id, class_name, parent_widget=self)
            if not view_name: raise RuntimeError(f"Failed to sync schema for '{class_name}'.")
                
            full_view_name = f"full_view_{safe_table_name}"
            with sqlite3.connect(db_path) as conn:
                df = pd.read_sql_query(f"SELECT * FROM {qid(full_view_name)}", conn)
            return df
            
        context = {'get_objects': local_get_objects, 'pd': pd}
        
        try:
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(f"--- Running Module: {mname} at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n\n")
                with redirect_stdout(f), redirect_stderr(f): exec(code, context)
            QMessageBox.information(self, "Success", f"Script '{mname}' executed successfully.\nOutput saved to:\n{out_path}")
        except Exception as e:
            with open(out_path, 'a', encoding='utf-8') as f:
                f.write("\n\n--- RUNTIME ERROR ---\n")
                traceback.print_exc(file=f)
            QMessageBox.warning(self, "Script Error", f"An error occurred during execution of '{mname}'.\nCheck the output file for details:\n{out_path}")

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