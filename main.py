import sys
import sqlite3
import os
import csv
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QTreeWidget, QTreeWidgetItem, QPushButton, QLineEdit, QLabel, QTableView, 
    QHeaderView, QMessageBox, QFileDialog, QListWidget, QListWidgetItem, QDialog,
    QSplitter, QScrollArea, QComboBox, QInputDialog, QFormLayout, QSpinBox, QDoubleSpinBox
)
from PySide6.QtCore import Qt, QSettings, QSortFilterProxyModel
from PySide6.QtSql import QSqlDatabase, QSqlTableModel
from PySide6.QtGui import QAction

from class_builder_dialog import ClassBuilderDialog, init_db, get_app_icon


def sync_physical_table(db_path, class_id, class_name, parent_widget=None):
    """Generates tables, junction tables, and a view to show bidirectional relationships."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Safety migration: ensure database has show_in_table for relationships if opening directly
    try: cur.execute("ALTER TABLE relationships ADD COLUMN show_in_table INTEGER DEFAULT 1")
    except sqlite3.OperationalError: pass
    conn.commit()
    
    safe_table_name = f"objects_{class_name.replace(' ', '_').lower()}"
    
    # 1. PRIMARY TABLE
    cur.execute("SELECT name, data_type, show_in_table, is_title FROM attributes WHERE class_id = ? ORDER BY row_order", (class_id,))
    attributes = cur.fetchall()
    
    required_cols = []
    attr_app_types = {}
    
    # Setup exact columns for the View
    select_clause = ["m.id AS [ID]"]
    
    for attr_name, attr_type, show_in_table, is_title in attributes:
        safe_col_name = attr_name.replace(' ', '_').lower()
        sql_type = "INTEGER" if attr_type == "int" else "REAL" if attr_type == "float" else "TEXT"
        required_cols.append((safe_col_name, sql_type))
        attr_app_types[safe_col_name] = attr_type
        
        if show_in_table:
            select_clause.append(f"m.{safe_col_name} AS [{attr_name}]")

    # Check if table exists
    cur.execute(f"SELECT count(name) FROM sqlite_master WHERE type='table' AND name='{safe_table_name}'")
    if cur.fetchone()[0] == 0:
        cols_def = ", ".join([f"{c} {t}" for c, t in required_cols])
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
            cur.execute(f"SELECT id, {', '.join(type_mismatches)} FROM {safe_table_name}")
            rows = cur.fetchall()
            conversion_failures = 0
            
            for row in rows:
                for idx, col in enumerate(type_mismatches):
                    val = row[idx+1]
                    if val is not None and str(val).strip() != "":
                        app_type = attr_app_types[col]
                        success = True
                        if app_type == "int":
                            try: int(float(val))
                            except: success = False
                        elif app_type == "float":
                            try: float(val)
                            except: success = False
                        elif app_type in ("list", "matrix"):
                            s = str(val).strip()
                            if not (s.startswith('[') and s.endswith(']')):
                                success = False
                                
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
                        
            # STANDARD SQLITE SCHEMA REBUILD PROCEDURE
            conn.commit()
            conn.execute("PRAGMA foreign_keys = OFF")
            
            # 1. Drop the view first to remove ghost dependencies before table alteration
            view_name = f"view_{safe_table_name}"
            cur.execute(f"DROP VIEW IF EXISTS {view_name}")
            
            # 2. Create the new table
            new_table = f"new_{safe_table_name}"
            cur.execute(f"DROP TABLE IF EXISTS {new_table}")
            
            cols_def = ", ".join([f"{c} {t}" for c, t in required_cols])
            if cols_def:
                cur.execute(f"CREATE TABLE {new_table} (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols_def})")
            else:
                cur.execute(f"CREATE TABLE {new_table} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            
            common_cols = list(set(existing_cols.keys()).intersection([c for c, t in required_cols]))
            if common_cols:
                # 3. Copy data over from old table
                cur.execute(f"SELECT id, {', '.join(common_cols)} FROM {safe_table_name}")
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
                                if app_type == "int": new_val = int(float(val))
                                elif app_type == "float": new_val = float(val)
                                elif app_type in ("list", "matrix"):
                                    s = str(val).strip()
                                    if s.startswith('[') and s.endswith(']'): new_val = s
                                    else: new_val = None
                                else: new_val = str(val)
                            except:
                                new_val = None
                        
                        new_values.append(new_val)
                        insert_cols.append(col)
                        placeholders.append("?")
                        
                    cur.execute(f"INSERT INTO {new_table} ({', '.join(insert_cols)}) VALUES ({', '.join(placeholders)})", new_values)
                    
            # 4. Drop old table and rename the new one back to the original name
            cur.execute(f"DROP TABLE {safe_table_name}")
            cur.execute(f"ALTER TABLE {new_table} RENAME TO {safe_table_name}")
            
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")
            
        else:
            for col_name, col_type in required_cols:
                if col_name not in existing_cols:
                    cur.execute(f"ALTER TABLE {safe_table_name} ADD COLUMN {col_name} {col_type}")

    # 2. JUNCTION TABLES (OUTGOING AND INCOMING)
    
    # Outgoing Relationships
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
            # Look up Target Title Column
            cur.execute("SELECT name FROM attributes WHERE class_id = ? AND is_title = 1 LIMIT 1", (target_class_id,))
            title_row = cur.fetchone()
            if title_row:
                target_title_col = title_row[0].replace(' ', '_').lower()
                display_label = f"[{target_name} ({title_row[0]})]"
            else:
                target_title_col = "id"
                display_label = f"[{target_name} (IDs)]"
                
            select_clause.append(f"(SELECT GROUP_CONCAT(t.{target_title_col}) FROM {junc_table} j JOIN objects_{safe_target_name} t ON j.target_id = t.id WHERE j.source_id = m.id) AS {display_label}")

    # Incoming Relationships
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
            # Look up Source Title Column
            cur.execute("SELECT name FROM attributes WHERE class_id = ? AND is_title = 1 LIMIT 1", (source_class_id,))
            title_row = cur.fetchone()
            if title_row:
                source_title_col = title_row[0].replace(' ', '_').lower()
                display_label = f"[From {source_name} ({title_row[0]})]"
            else:
                source_title_col = "id"
                display_label = f"[From {source_name} (IDs)]"
                
            select_clause.append(f"(SELECT GROUP_CONCAT(t.{source_title_col}) FROM {junc_table} j JOIN objects_{safe_source_name} t ON j.source_id = t.id WHERE j.target_id = m.id) AS {display_label}")
            
    # 3. CREATE DYNAMIC VIEW TO SHOW ALL RELATIONSHIPS AND SHOWN COLUMNS
    view_name = f"view_{safe_table_name}"
    cur.execute(f"DROP VIEW IF EXISTS {view_name}")
    
    view_sql = f"CREATE VIEW {view_name} AS SELECT {', '.join(select_clause)} FROM {safe_table_name} m"
    cur.execute(view_sql)

    conn.commit()
    conn.close()
    
    return view_name, safe_table_name

class AddObjectDialog(QDialog):
    def __init__(self, db_path, class_id, class_name, table_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Add New {class_name}")
        self.setWindowIcon(get_app_icon())
        self.resize(500, 550) 
        
        self.db_path = db_path
        self.class_id = class_id
        self.table_name = table_name
        self.input_widgets = {} 
        self.rel_widgets = {}   
        self.attr_app_types = {} 
        
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
        
        # --- Add Attributes ---
        cur.execute("SELECT name, data_type FROM attributes WHERE class_id = ? ORDER BY row_order", (self.class_id,))
        attributes = cur.fetchall()

        for attr_name, attr_type in attributes:
            safe_col_name = attr_name.replace(' ', '_').lower()
            self.attr_app_types[safe_col_name] = attr_type
            
            if attr_type == "int":
                widget = QSpinBox()
                widget.setRange(-2147483648, 2147483647) 
            elif attr_type == "float":
                widget = QDoubleSpinBox()
                widget.setRange(-1e9, 1e9)
                widget.setDecimals(4)
            elif attr_type in ("list", "matrix"):
                widget = QLineEdit()
                widget.setText("[]") 
                widget.setToolTip("Must be enclosed in brackets []")
            else:
                widget = QLineEdit()
                
            self.input_widgets[safe_col_name] = widget
            form_layout.addRow(attr_name + ":", widget)

        # --- Add Relationships ---
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
                list_widget.setToolTip("Select a single target")
            else:
                list_widget.setSelectionMode(QListWidget.MultiSelection)
                list_widget.setToolTip("Hold Ctrl/Cmd to select multiple targets")
                
            list_widget.setMinimumHeight(120) 
            target_table = f"objects_{safe_target_name}"
            
            cur.execute("SELECT name FROM attributes WHERE class_id = ? AND is_title = 1 LIMIT 1", (target_class_id,))
            title_row = cur.fetchone()
            
            try:
                if title_row:
                    title_col = title_row[0].replace(' ', '_').lower()
                    cur.execute(f"SELECT id, {title_col} FROM {target_table}")
                    for row in cur.fetchall():
                        obj_id = row[0]
                        display_val = row[1]
                        
                        # Show ID in UI, but store only the name for search filtering
                        if display_val is None or str(display_val).strip() == "":
                            display_text = f"Unnamed {target_name} (ID: {obj_id})"
                            search_text_data = f"unnamed {target_name}".lower()
                        else:
                            display_text = f"{str(display_val)} (ID: {obj_id})"
                            search_text_data = str(display_val).lower()
                            
                        item = QListWidgetItem(display_text)
                        item.setData(Qt.UserRole, obj_id)
                        item.setData(Qt.UserRole + 1, search_text_data) # Store hidden search tag
                        list_widget.addItem(item)
                else:
                    cur.execute(f"SELECT id FROM {target_table}")
                    for row in cur.fetchall():
                        obj_id = row[0]
                        item = QListWidgetItem(f"{target_name} #{obj_id}")
                        item.setData(Qt.UserRole, obj_id)
                        item.setData(Qt.UserRole + 1, target_name.lower()) # Store hidden search tag
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
            # Retrieve the specific searchable text (Name only) stored in UserRole + 1
            search_target = item.data(Qt.UserRole + 1)
            if search_target is None:
                search_target = item.text().lower() # Fallback if not set
            item.setHidden(search_text not in search_target)

    # Stop Enter key from closing the dialog entirely
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            return # Simply ignore the event
        super().keyPressEvent(event)

    def save_record(self):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA foreign_keys = 1")
            cur = conn.cursor()

            columns = []
            values = []
            placeholders = []
            for col_name, widget in self.input_widgets.items():
                columns.append(col_name)
                placeholders.append("?")
                app_type = self.attr_app_types.get(col_name)
                
                if isinstance(widget, QSpinBox) or isinstance(widget, QDoubleSpinBox):
                    values.append(widget.value())
                else:
                    val = widget.text().strip()
                    if app_type in ("list", "matrix"):
                        if not (val.startswith('[') and val.endswith(']')):
                            QMessageBox.warning(self, "Invalid Input", f"The attribute '{col_name}' must be enclosed in brackets [].")
                            if 'conn' in locals(): conn.close()
                            return 
                    values.append(val)

            if columns:
                query = f"INSERT INTO {self.table_name} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
                cur.execute(query, tuple(values))
            else:
                query = f"INSERT INTO {self.table_name} DEFAULT VALUES"
                cur.execute(query)
                
            new_obj_id = cur.lastrowid
            
            for junc_table, data in self.rel_widgets.items():
                selected_ids = [int(item.data(Qt.UserRole)) for item in data["widget"].selectedItems()]
                for target_id in selected_ids:
                    cur.execute(f"INSERT INTO {junc_table} (source_id, target_id) VALUES (?, ?)", (new_obj_id, target_id))
            
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

        btn_save = QPushButton("Save & Connect")
        btn_save.clicked.connect(self.save_and_check_db)
        layout.addWidget(btn_save)

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
        
        layout = QVBoxLayout(self)

        self.header_label = QLabel("<h2></h2>")
        layout.addWidget(self.header_label)

        search_layout = QHBoxLayout()
        
        self.col_combo = QComboBox()
        self.col_combo.addItem("All Columns", -1)
        self.col_combo.currentIndexChanged.connect(self.apply_filter)
        
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter text...")
        self.search_input.textChanged.connect(self.apply_filter)
        
        self.btn_export = QPushButton("💾 Export")
        self.btn_export.clicked.connect(self.export_data)
        
        self.btn_add = QPushButton("+ Add Object")
        self.btn_add.setEnabled(False) 
        self.btn_add.clicked.connect(self.open_add_dialog)
        
        search_layout.addWidget(QLabel("Search In:"))
        search_layout.addWidget(self.col_combo)
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.btn_export)
        search_layout.addWidget(self.btn_add)
        layout.addLayout(search_layout)

        self.table_view = QTableView()
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setSortingEnabled(True)
        header = self.table_view.horizontalHeader()
        header.setSectionsMovable(True)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        layout.addWidget(self.table_view)

        self.proxy_model = QSortFilterProxyModel()
        self.proxy_model.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy_model.setFilterKeyColumn(-1) 
        self.sql_model = None

    def load_table_data(self, class_id, class_name):
        self.current_class_id = class_id
        self.current_class_name = class_name
        self.header_label.setText(f"<h2>{class_name}</h2>")
        self.btn_add.setEnabled(True)
        
        settings = QSettings("MyCompany", "DatabaseManagerApp")
        db_path = settings.value("db_path", "")
        
        self.current_view_name, self.current_table_name = sync_physical_table(db_path, class_id, class_name, parent_widget=self)
        
        if not self.current_view_name:
            self.header_label.setText("<h2>Sync Aborted</h2>")
            self.btn_add.setEnabled(False)
            return

        self.sql_model = QSqlTableModel()
        self.sql_model.setTable(self.current_view_name)
        self.sql_model.select() 
        
        self.proxy_model.setSourceModel(self.sql_model)
        self.table_view.setModel(self.proxy_model)
        
        self.col_combo.blockSignals(True)
        self.col_combo.clear()
        self.col_combo.addItem("All Columns", -1)
        for i in range(self.sql_model.columnCount()):
            col_name = self.sql_model.headerData(i, Qt.Horizontal)
            self.col_combo.addItem(col_name, i)
        self.col_combo.blockSignals(False)
        
        self.apply_filter()

    def apply_filter(self):
        text = self.search_input.text()
        col_idx = self.col_combo.currentData()
        if col_idx is None:
            col_idx = -1
            
        self.proxy_model.setFilterKeyColumn(col_idx)
        self.proxy_model.setFilterFixedString(text)

    def open_add_dialog(self):
        settings = QSettings("MyCompany", "DatabaseManagerApp")
        db_path = settings.value("db_path", "")
        
        dialog = AddObjectDialog(db_path, self.current_class_id, self.current_class_name, self.current_table_name, self)
        if dialog.exec():
            self.sql_model.select() 

    def export_data(self):
        if not self.sql_model: return
        
        file_path, selected_filter = QFileDialog.getSaveFileName(
            self, "Export Data", "", "CSV Files (*.csv);;Excel Files (*.xlsx)"
        )
        if not file_path: return
        
        if "csv" in selected_filter.lower() and not file_path.lower().endswith(".csv"):
            file_path += ".csv"
        elif "excel" in selected_filter.lower() and not file_path.lower().endswith(".xlsx"):
            file_path += ".xlsx"
            
        rows = self.proxy_model.rowCount()
        cols = self.proxy_model.columnCount()
        headers = [self.proxy_model.headerData(i, Qt.Horizontal) for i in range(cols)]
        
        data = []
        for r in range(rows):
            row_data = []
            for c in range(cols):
                idx = self.proxy_model.index(r, c)
                val = self.proxy_model.data(idx)
                row_data.append("" if val is None else str(val))
            data.append(row_data)

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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nexus")
        self.setWindowIcon(get_app_icon())
        self.resize(1024, 768)

        self.setup_menu()

        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)

        # Replaced QListWidget with QTreeWidget
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

        action_settings = QAction("⚙️ Settings", self)
        action_settings.triggered.connect(self.open_settings)
        db_menu.addAction(action_settings)

        action_builder = QAction("🛠️ Class Builder", self)
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
            # Safely check if path column exists yet via automatic migration
            try:
                cur.execute("ALTER TABLE classes ADD COLUMN path TEXT DEFAULT ''")
                conn.commit()
            except sqlite3.OperationalError:
                pass # Already exists
                
            cur.execute("SELECT id, name, path FROM classes ORDER BY path ASC, name ASC")
            
            root_items = {}
            parent_item = self.sidebar.invisibleRootItem()
            
            for c_id, c_name, c_path in cur.fetchall():
                c_path = (c_path or "").strip().strip('/')
                current_parent = parent_item
                
                # Navigate and generate Tree folders if path has parts
                if c_path:
                    current_path = ""
                    for part in c_path.split('/'):
                        part = part.strip()
                        if not part: continue
                        current_path = f"{current_path}/{part}" if current_path else part
                        
                        if current_path not in root_items:
                            folder_item = QTreeWidgetItem([f"📁 {part}"])
                            current_parent.addChild(folder_item)
                            root_items[current_path] = folder_item
                        
                        current_parent = root_items[current_path]
                
                # Append actual class document under the specified parent
                class_item = QTreeWidgetItem([f"📄 {c_name}"])
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
            # Only trigger for files (leaves), not folders
            class_name = item.text(0).replace("📄 ", "") 
            self.data_browser.load_table_data(class_id, class_name)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion") 
    app.setWindowIcon(get_app_icon()) 
    window = MainWindow()
    window.show()
    sys.exit(app.exec())