import sys
import sqlite3
import os
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QListWidget, QPushButton, QLineEdit, QLabel, QTableView, 
    QHeaderView, QMessageBox, QFileDialog, QListWidgetItem, QDialog,
    QSplitter, QScrollArea, QComboBox, QInputDialog, QCheckBox,
    QTreeWidget, QTreeWidgetItem
)
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt, QSettings

DB_NAME = "class_manager.db"

def get_app_icon():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return QIcon(os.path.join(base_dir, "resources", "app_image.ico"))

# ==========================================
# DATABASE INITIALIZATION
# ==========================================
def init_db(db_path=DB_NAME):
    """
    Creates the foundational database tables if they do not exist.
    This schema powers the entire meta-architecture of the Nexus app.
    (Clean install mapping - No Legacy ALTER table fallbacks required)
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = 1") # Enforces cascading deletes
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            path TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS attributes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER,
            name TEXT NOT NULL,
            data_type TEXT NOT NULL,
            row_order INTEGER DEFAULT 0,
            show_in_table INTEGER DEFAULT 1,
            is_title INTEGER DEFAULT 0,
            is_unique INTEGER DEFAULT 0,
            is_required INTEGER DEFAULT 0,
            lookup_query TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS matrix_columns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attribute_id INTEGER,
            column_name TEXT NOT NULL,
            column_index INTEGER NOT NULL,
            FOREIGN KEY(attribute_id) REFERENCES attributes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_class INTEGER,
            target_class INTEGER,
            rel_type TEXT NOT NULL,
            row_order INTEGER DEFAULT 0,
            show_in_table INTEGER DEFAULT 1,
            FOREIGN KEY(source_class) REFERENCES classes(id) ON DELETE CASCADE,
            FOREIGN KEY(target_class) REFERENCES classes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS modules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            path TEXT DEFAULT '',
            code TEXT DEFAULT ''
        );
    """)
    conn.commit()
    return conn


class ReorderableRow(QWidget):
    def __init__(self, parent_layout):
        super().__init__()
        self.parent_layout = parent_layout
        
    def move_up(self):
        idx = self.parent_layout.indexOf(self)
        if idx > 0:
            self.parent_layout.takeAt(idx)
            self.parent_layout.insertWidget(idx - 1, self)
            
    def move_down(self):
        idx = self.parent_layout.indexOf(self)
        if idx < self.parent_layout.count() - 1:
            self.parent_layout.takeAt(idx)
            self.parent_layout.insertWidget(idx + 1, self)

class AttributeRow(ReorderableRow):
    def __init__(self, parent_layout, valid_lookups=[], attr_data=None):
        super().__init__(parent_layout)
        self.matrix_cols = []
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.btn_up = QPushButton("\u25B2") 
        self.btn_up.setFixedWidth(25)
        self.btn_up.clicked.connect(self.move_up)
        
        self.btn_down = QPushButton("\u25BC") 
        self.btn_down.setFixedWidth(25)
        self.btn_down.clicked.connect(self.move_down)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Attribute Name")
        
        self.type_combo = QComboBox()
        self.type_combo.addItems([
            "int", "float", "string", "long string", "date", "boolean", "list", "matrix", "look-through"
        ])
        self.type_combo.currentTextChanged.connect(self.on_type_changed)
        
        self.lookup_input = QComboBox()
        self.lookup_input.setEditable(True) 
        self.lookup_input.addItems(valid_lookups)
        self.lookup_input.setCurrentIndex(-1)
        self.lookup_input.setPlaceholderText("TargetClass.Attribute")
        self.lookup_input.setVisible(False)
        self.lookup_input.setToolTip("Select from existing attributes or type manually (TargetClass.Attribute)")
        
        self.show_cb = QCheckBox("Show")
        self.show_cb.setChecked(True)
        
        self.title_cb = QCheckBox("Title")
        self.unique_cb = QCheckBox("Unique")
        self.req_cb = QCheckBox("Req.")

        self.matrix_btn = QPushButton("Set Cols")
        self.matrix_btn.setVisible(False)
        self.matrix_btn.clicked.connect(self.set_matrix_columns)

        self.delete_btn = QPushButton("X")
        self.delete_btn.setFixedWidth(30)
        self.delete_btn.clicked.connect(self.deleteLater)

        layout.addWidget(self.btn_up)
        layout.addWidget(self.btn_down)
        layout.addWidget(self.name_input)
        layout.addWidget(self.type_combo)
        layout.addWidget(self.lookup_input)
        layout.addWidget(self.show_cb)
        layout.addWidget(self.title_cb)
        layout.addWidget(self.unique_cb)
        layout.addWidget(self.req_cb)
        layout.addWidget(self.matrix_btn)
        layout.addWidget(self.delete_btn)

        if attr_data:
            self.name_input.setText(attr_data['name'])
            self.type_combo.setCurrentText(attr_data['type'])
            self.show_cb.setChecked(bool(attr_data.get('show_in_table', 1)))
            self.title_cb.setChecked(bool(attr_data.get('is_title', 0)))
            self.unique_cb.setChecked(bool(attr_data.get('is_unique', 0)))
            self.req_cb.setChecked(bool(attr_data.get('is_required', 0)))
            self.matrix_cols = attr_data.get('matrix_cols', [])
            self.lookup_input.setCurrentText(attr_data.get('lookup_query', ''))
            self.on_type_changed(attr_data['type'])

    def on_type_changed(self, text):
        self.matrix_btn.setVisible(text == "matrix")
        self.lookup_input.setVisible(text == "look-through")

    def set_matrix_columns(self):
        current_cols = ",".join(self.matrix_cols)
        text, ok = QInputDialog.getText(
            self, "Matrix Columns", 
            "Enter column names separated by comma:", 
            QLineEdit.Normal, current_cols
        )
        if ok and text:
            self.matrix_cols = [c.strip() for c in text.split(",") if c.strip()]


class RelationshipRow(ReorderableRow):
    def __init__(self, parent_layout, valid_classes, rel_data=None):
        super().__init__(parent_layout)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.btn_up = QPushButton("\u25B2") 
        self.btn_up.setFixedWidth(25)
        self.btn_up.clicked.connect(self.move_up)
        
        self.btn_down = QPushButton("\u25BC") 
        self.btn_down.setFixedWidth(25)
        self.btn_down.clicked.connect(self.move_down)

        self.target_combo = QComboBox()
        for cid, cname in valid_classes: 
            self.target_combo.addItem(cname, cid)
            
        self.type_combo = QComboBox()
        self.type_combo.addItems(["one_to_many", "many_to_many"])
        
        self.show_cb = QCheckBox("Show in Table")
        self.show_cb.setChecked(True)
        
        self.delete_btn = QPushButton("X")
        self.delete_btn.setFixedWidth(30)
        self.delete_btn.clicked.connect(self.deleteLater)

        layout.addWidget(self.btn_up)
        layout.addWidget(self.btn_down)
        layout.addWidget(QLabel("Target Class:"))
        layout.addWidget(self.target_combo)
        layout.addWidget(self.type_combo)
        layout.addWidget(self.show_cb)
        layout.addWidget(self.delete_btn)

        if rel_data:
            idx = self.target_combo.findData(rel_data['target_class'])
            if idx >= 0: 
                self.target_combo.setCurrentIndex(idx)
            self.type_combo.setCurrentText(rel_data['type'])
            self.show_cb.setChecked(bool(rel_data.get('show_in_table', 1)))


class ClassBuilderDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Nexus - Class Builder")
        self.setWindowIcon(get_app_icon())
        self.resize(1000, 600)
        
        settings = QSettings("MyCompany", "DatabaseManagerApp")
        path = settings.value("db_path", "").strip()
        if not path:
            path = DB_NAME 
            
        self.db = init_db(path)
        self.current_class_id = None

        main_layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter)

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        self.class_list_widget = QTreeWidget()
        self.class_list_widget.setHeaderHidden(True)
        self.class_list_widget.itemClicked.connect(self.load_class)
        
        btn_add = QPushButton("Create New Class")
        btn_add.clicked.connect(self.create_new_class)
        left_layout.addWidget(QLabel("<b>Classes</b>"))
        left_layout.addWidget(self.class_list_widget)
        left_layout.addWidget(btn_add)

        self.editor_widget = QWidget()
        self.editor_layout = QVBoxLayout(self.editor_widget)
        self.editor_widget.setEnabled(False)
        
        name_layout = QHBoxLayout()
        self.class_name_input = QLineEdit()
        self.class_name_input.setPlaceholderText("Name")
        self.class_path_input = QLineEdit()
        self.class_path_input.setPlaceholderText("e.g. Settings/Core")
        
        name_layout.addWidget(QLabel("Class Name:"))
        name_layout.addWidget(self.class_name_input)
        name_layout.addWidget(QLabel("Path:"))
        name_layout.addWidget(self.class_path_input)
        self.editor_layout.addLayout(name_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(scroll_content)
        self.scroll_layout.setAlignment(Qt.AlignTop)
        scroll.setWidget(scroll_content)
        self.editor_layout.addWidget(scroll)

        self.attributes_layout = QVBoxLayout()
        self.relationships_layout = QVBoxLayout()
        
        self.scroll_layout.addWidget(QLabel("<b>Attributes</b>"))
        self.scroll_layout.addLayout(self.attributes_layout)
        self.scroll_layout.addWidget(QLabel("<b>Relationships</b>"))
        self.scroll_layout.addLayout(self.relationships_layout)

        btn_layout = QHBoxLayout()
        btn_attr = QPushButton("+ Add Attribute")
        btn_attr.clicked.connect(lambda: self.attributes_layout.addWidget(AttributeRow(self.attributes_layout, self.get_all_lookups())))
        btn_rel = QPushButton("+ Add Relationship")
        btn_rel.clicked.connect(self.add_rel_row)
        btn_layout.addWidget(btn_attr)
        btn_layout.addWidget(btn_rel)
        self.editor_layout.addLayout(btn_layout)

        action_layout = QHBoxLayout()
        btn_save = QPushButton("Save Class")
        btn_save.clicked.connect(self.save_class)
        
        btn_delete = QPushButton("Delete Class")
        btn_delete.setStyleSheet("background-color: #ff4c4c; color: white;")
        btn_delete.clicked.connect(self.delete_class)
        
        action_layout.addWidget(btn_save)
        action_layout.addWidget(btn_delete)
        self.editor_layout.addLayout(action_layout)

        splitter.addWidget(left_widget)
        splitter.addWidget(self.editor_widget)
        splitter.setSizes([250, 750])

        self.refresh_class_list()
        
    def get_all_classes(self):
        cur = self.db.cursor()
        cur.execute("SELECT id, name, path FROM classes ORDER BY path ASC, name ASC")
        return cur.fetchall()

    def get_all_lookups(self):
        cur = self.db.cursor()
        try:
            cur.execute("""
                SELECT c.name, a.name 
                FROM classes c 
                JOIN attributes a ON c.id = a.class_id
                ORDER BY c.name ASC, a.name ASC
            """)
            return [f"{row[0]}.{row[1]}" for row in cur.fetchall()]
        except sqlite3.OperationalError:
            return []

    def refresh_class_list(self):
        self.class_list_widget.clear()
        
        root_items = {}
        parent_item = self.class_list_widget.invisibleRootItem()
        
        for cid, cname, cpath in self.get_all_classes():
            cpath = (cpath or "").strip().strip('/')
            current_parent = parent_item
            
            if cpath:
                current_path = ""
                for part in cpath.split('/'):
                    part = part.strip()
                    if not part: continue
                    current_path = f"{current_path}/{part}" if current_path else part
                    
                    if current_path not in root_items:
                        folder_item = QTreeWidgetItem([f"\U0001F4C1 {part}"])
                        current_parent.addChild(folder_item)
                        root_items[current_path] = folder_item
                    
                    current_parent = root_items[current_path]
            
            item = QTreeWidgetItem([f"\U0001F4C4 {cname}"])
            item.setData(0, Qt.UserRole, cid)
            current_parent.addChild(item)
            
        self.class_list_widget.expandAll()

    def add_rel_row(self):
        valid = [(c[0], c[1]) for c in self.get_all_classes() if c[0] != self.current_class_id]
        self.relationships_layout.addWidget(RelationshipRow(self.relationships_layout, valid))

    def create_new_class(self):
        self.current_class_id = None
        self.class_name_input.clear()
        self.class_path_input.clear()
        self.clear_layout(self.attributes_layout)
        self.clear_layout(self.relationships_layout)
            
        self.editor_widget.setEnabled(True)

    def clear_layout(self, layout):
        while layout.count():
            child = layout.takeAt(0)
            if child.widget(): child.widget().deleteLater()

    def load_class(self, item, column=0):
        cid = item.data(0, Qt.UserRole)
        if not cid: 
            return 
            
        self.current_class_id = cid
        self.editor_widget.setEnabled(True)
        self.clear_layout(self.attributes_layout)
        self.clear_layout(self.relationships_layout)

        cur = self.db.cursor()
        cur.execute("SELECT name, path FROM classes WHERE id = ?", (self.current_class_id,))
        row = cur.fetchone()
        self.class_name_input.setText(row[0])
        self.class_path_input.setText(row[1] if row[1] else "")
        
        valid_lookups = self.get_all_lookups()

        cur.execute("SELECT id, name, data_type, show_in_table, is_title, is_unique, is_required, lookup_query FROM attributes WHERE class_id = ? ORDER BY row_order ASC", (self.current_class_id,))
        attributes = cur.fetchall()
        for attr_id, attr_name, attr_type, show_in_table, is_title, is_unique, is_required, lookup_query in attributes:
            matrix_cols = []
            if attr_type == "matrix":
                cur.execute("SELECT column_name FROM matrix_columns WHERE attribute_id = ? ORDER BY column_index", (attr_id,))
                matrix_cols = [row[0] for row in cur.fetchall()]
            
            attr_data = {
                'name': attr_name, 
                'type': attr_type, 
                'matrix_cols': matrix_cols,
                'show_in_table': show_in_table,
                'is_title': is_title,
                'is_unique': is_unique,
                'is_required': is_required,
                'lookup_query': lookup_query if lookup_query else ''
            }
            self.attributes_layout.addWidget(AttributeRow(self.attributes_layout, valid_lookups, attr_data))

        cur.execute("SELECT target_class, rel_type, show_in_table FROM relationships WHERE source_class = ? ORDER BY row_order ASC", (self.current_class_id,))
        for target, rel_type, show_in_table in cur.fetchall():
            valid_classes = [(c[0], c[1]) for c in self.get_all_classes() if c[0] != self.current_class_id]
            self.relationships_layout.addWidget(RelationshipRow(self.relationships_layout, valid_classes, {'target_class': target, 'type': rel_type, 'show_in_table': show_in_table}))

    def check_for_circular_dependencies(self, new_class_name):
        """Builds a temporary look-through graph to prevent Infinite Recursion Loops."""
        cur = self.db.cursor()
        cur.execute("SELECT id, name FROM classes")
        classes = {row[0]: row[1] for row in cur.fetchall()}
        
        temp_id = self.current_class_id if self.current_class_id else -1
        classes[temp_id] = new_class_name
        
        graph = {cid: set() for cid in classes}
        
        # Load existing configurations
        cur.execute("SELECT class_id, lookup_query FROM attributes WHERE data_type = 'look-through' AND lookup_query != ''")
        for cid, lookup in cur.fetchall():
            if cid == self.current_class_id: continue # We'll substitute unsaved UI values
            tgt_name = lookup.split('.')[0].strip().lower()
            for t_id, t_name in classes.items():
                if t_name.lower() == tgt_name:
                    graph[cid].add(t_id)

        # Inject currently unsaved UI parameters
        for i in range(self.attributes_layout.count()):
            widget = self.attributes_layout.itemAt(i).widget()
            if isinstance(widget, AttributeRow) and widget.type_combo.currentText() == "look-through":
                lookup = widget.lookup_input.currentText().strip()
                if lookup:
                    tgt_name = lookup.split('.')[0].strip().lower()
                    for t_id, t_name in classes.items():
                        if t_name.lower() == tgt_name:
                            graph[temp_id].add(t_id)

        # Graph DFS cycle detection
        visited = set()
        temp_mark = set()
        def visit(n):
            if n in temp_mark: return True
            if n not in visited:
                temp_mark.add(n)
                for m in graph.get(n, set()):
                    if visit(m): return True
                temp_mark.remove(n)
                visited.add(n)
            return False
            
        for node in graph:
            if visit(node): return True
        return False

    def save_class(self):
        name = self.class_name_input.text().strip()
        path_val = self.class_path_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Error", "Class name cannot be empty.")
            return

        attr_names = set()
        for i in range(self.attributes_layout.count()):
            widget = self.attributes_layout.itemAt(i).widget()
            if isinstance(widget, AttributeRow):
                attr_name = widget.name_input.text().strip()
                if not attr_name: continue
                safe_name = attr_name.replace(' ', '_').lower()
                if safe_name in attr_names:
                    QMessageBox.warning(self, "Error", f"Duplicate attribute detected: '{attr_name}'. Attribute names must be unique.")
                    return
                attr_names.add(safe_name)

        if self.check_for_circular_dependencies(name):
            QMessageBox.critical(self, "Circular Dependency Detected", 
                "Cannot save class: This 'Look-Through' configuration creates an infinite circular loop.\n\n"
                "For example: Class A looks through Class B, and Class B looks through Class A. "
                "Please fix the routing architecture.")
            return

        cur = self.db.cursor()
        try:
            if self.current_class_id is None:
                cur.execute("INSERT INTO classes (name, path) VALUES (?, ?)", (name, path_val))
                self.current_class_id = cur.lastrowid
            else:
                cur.execute("UPDATE classes SET name = ?, path = ? WHERE id = ?", (name, path_val, self.current_class_id))
            
            cur.execute("DELETE FROM attributes WHERE class_id = ?", (self.current_class_id,))
            cur.execute("DELETE FROM relationships WHERE source_class = ?", (self.current_class_id,))

            for i in range(self.attributes_layout.count()):
                widget = self.attributes_layout.itemAt(i).widget()
                if isinstance(widget, AttributeRow):
                    attr_name = widget.name_input.text().strip()
                    attr_type = widget.type_combo.currentText()
                    if not attr_name: continue
                    
                    show_in_table = 1 if widget.show_cb.isChecked() else 0
                    is_title = 1 if widget.title_cb.isChecked() else 0
                    is_unique = 1 if widget.unique_cb.isChecked() else 0
                    is_required = 1 if widget.req_cb.isChecked() else 0
                    lookup_query = widget.lookup_input.currentText().strip() if attr_type == "look-through" else ""
                    
                    cur.execute("""
                        INSERT INTO attributes (class_id, name, data_type, row_order, show_in_table, is_title, is_unique, is_required, lookup_query) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (self.current_class_id, attr_name, attr_type, i, show_in_table, is_title, is_unique, is_required, lookup_query))
                    new_attr_id = cur.lastrowid

                    if attr_type == "matrix":
                        for idx, col_name in enumerate(widget.matrix_cols):
                            cur.execute("INSERT INTO matrix_columns (attribute_id, column_name, column_index) VALUES (?, ?, ?)",
                                        (new_attr_id, col_name, idx))

            for i in range(self.relationships_layout.count()):
                widget = self.relationships_layout.itemAt(i).widget()
                if isinstance(widget, RelationshipRow):
                    target_id = widget.target_combo.currentData()
                    rel_type = widget.type_combo.currentText()
                    show_in_table = 1 if widget.show_cb.isChecked() else 0
                    
                    if target_id is not None:
                        cur.execute("INSERT INTO relationships (source_class, target_class, rel_type, row_order, show_in_table) VALUES (?, ?, ?, ?, ?)",
                                    (self.current_class_id, target_id, rel_type, i, show_in_table))

            self.db.commit()
            self.refresh_class_list()
            QMessageBox.information(self, "Success", "Class saved successfully!")
            
        except sqlite3.IntegrityError:
            self.db.rollback()
            QMessageBox.warning(self, "Error", "Class name already exists.")

    def delete_class(self):
        if self.current_class_id is None: return
        reply = QMessageBox.question(self, "Delete", "Are you sure you want to delete this class?", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.db.execute("DELETE FROM classes WHERE id = ?", (self.current_class_id,))
            self.db.commit()
            self.current_class_id = None
            self.editor_widget.setEnabled(False)
            self.clear_layout(self.attributes_layout)
            self.clear_layout(self.relationships_layout)
            self.class_name_input.clear()
            self.class_path_input.clear()
            self.refresh_class_list()