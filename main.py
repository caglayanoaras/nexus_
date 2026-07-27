"""Application shell: database settings, the module (script) builder, the main
window, and the entry point. Feature UIs live in class_builder_dialog /
object_editor / data_browser; shared logic in core."""
import os
import sys
import sqlite3
import datetime
import traceback
import qtawesome as qta
from contextlib import redirect_stdout, redirect_stderr

try:
    import pandas as pd
except ImportError:
    pd = None

from PySide6.QtWidgets import (
    QApplication, QWidget, QDialog, QVBoxLayout, QHBoxLayout, QSplitter,
    QLabel, QLineEdit, QPushButton, QPlainTextEdit, QFileDialog, QMessageBox,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QMenuBar, QMenu,
)
from PySide6.QtCore import Qt
from PySide6.QtSql import QSqlDatabase
from PySide6.QtGui import QAction, QFont

from core import (
    get_db_path, init_db, sync_physical_table, sanitize_name, qid, get_app_icon,
    files_dir_for, trash_stored_file, set_db_path,
    get_module_output_path, set_module_output_path, backup_database,
    db_session, is_network_path, journal_mode, BUSY_TIMEOUT_MS,
)
from class_builder_dialog import ClassBuilderDialog, DiscreteTypeBuilderDialog
from data_browser import DataBrowserPage


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Database Settings")
        self.setWindowIcon(get_app_icon())
        self.resize(700, 250)

        main_layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        
        db_tab = QWidget()
        db_layout = QVBoxLayout(db_tab)
        db_layout.addWidget(QLabel("Database Path:"))

        path_layout = QHBoxLayout()
        self.path_input = QLineEdit()
        # Show the resolved absolute path — the config may hold just "nexus.db".
        self.path_input.setText(get_db_path())

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
        # Same single source of truth the module runners use, so what this shows is
        # exactly where a run will write.
        self.output_input.setText(get_module_output_path())


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
        set_db_path(path)
        QMessageBox.information(self, "Success", "Database path saved as default.")

    def save_module_path(self):
        set_module_output_path(self.output_input.text().strip())
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
        with db_session(self.db_path) as conn:
            cur = conn.cursor()
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
        
        with db_session(self.db_path) as conn:
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
            with db_session(self.db_path) as conn:
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
            with db_session(self.db_path) as conn:
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
        with db_session(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM classes WHERE name = ?", (class_name,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Class '{class_name}' not found in the database.")
            cls_id = row[0]
            
        view_name, safe_table_name = sync_physical_table(self.db_path, cls_id, class_name, parent_widget=None)
        if not view_name: raise RuntimeError(f"Failed to sync schema for class '{class_name}'.")
            
        full_view_name = f"full_view_{safe_table_name}"
        with db_session(self.db_path) as conn:
            df = pd.read_sql_query(f"SELECT * FROM {qid(full_view_name)}", conn)
        return df

    def run_script(self):
        code = self.editor.toPlainText()
        out_path = get_module_output_path()
        context = {'get_objects': self.get_objects, 'pd': pd}

        try:
            # Append (don't truncate) so each run keeps the previous output as history.
            with open(out_path, 'a', encoding='utf-8') as f:
                f.write(f"\n--- Script Executed at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n\n")
                with redirect_stdout(f), redirect_stderr(f): exec(code, context)
            QMessageBox.information(self, "Success", f"Script executed successfully.\nOutput appended to:\n{out_path}")
        except Exception as e:
            with open(out_path, 'a', encoding='utf-8') as f:
                f.write("\n\n--- RUNTIME ERROR ---\n")
                traceback.print_exc(file=f)
            QMessageBox.warning(self, "Script Error", f"An error occurred during execution.\nCheck the output file for details:\n{out_path}")


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

        action_discrete = QAction(qta.icon('fa5s.list-ul'), " Discrete Type Builder", self)
        action_discrete.triggered.connect(self.open_discrete_builder)
        db_menu.addAction(action_discrete)

        action_module = QAction(qta.icon('fa5s.file-code'), " Module Builder", self)
        action_module.triggered.connect(self.open_module_builder)
        db_menu.addAction(action_module)

        db_menu.addSeparator()
        action_backup = QAction(qta.icon('fa5s.shield-alt'), " Backup Now...", self)
        action_backup.triggered.connect(self.backup_now)
        db_menu.addAction(action_backup)

        action_cleanup = QAction(qta.icon('fa5s.broom'), " Clean Up Unused Files", self)
        action_cleanup.triggered.connect(self.clean_unused_files)
        db_menu.addAction(action_cleanup)

        doc_menu = menubar.addMenu("Documentation")
        action_api = QAction(qta.icon('fa5s.book'), " Module API", self)
        action_api.triggered.connect(self.open_module_api)
        doc_menu.addAction(action_api)

    def open_settings(self):
        old_db = get_db_path()
        dialog = SettingsDialog(self)
        dialog.exec()
        new_db = get_db_path()
        # Only re-run the (heavy, potentially dialog-popping) schema sync when the
        # database actually changed — not when just the module output path was edited.
        self.refresh_sidebar(do_sync=(old_db != new_db))

    def open_builder(self):
        dialog = ClassBuilderDialog(self)
        dialog.exec()
        self.refresh_sidebar()

    def open_discrete_builder(self):
        dialog = DiscreteTypeBuilderDialog(self)
        dialog.exec()

    def open_module_builder(self):
        dialog = ModuleBuilderDialog(get_db_path(), self)
        dialog.exec()
        self.refresh_module_sidebar()
        
    def open_module_api(self):
        QMessageBox.information(self, "Module API", "Documentation coming soon...\n\nCurrently available built-in functions:\n\nget_objects(class_name)\n-> Returns a complete Pandas DataFrame of the specified class, including automatically resolved multi-title relationships.")

    def warn_if_wal_on_network(self, db_path):
        """Flag the one combination SQLite does not support: WAL on a network share.

        init_db switches network-hosted databases to the rollback journal, but that
        only takes effect when no other connection holds the file. If a colleague
        still has the app open, the switch silently does nothing and everyone stays
        in the unsupported mode — so say so rather than leave it invisible.
        """
        if getattr(self, "_warned_wal_on_network", False):
            return
        if not is_network_path(db_path) or journal_mode(db_path) != "wal":
            return
        self._warned_wal_on_network = True
        QMessageBox.warning(self, "Network Database Warning",
            "This database is on a network location and is still using WAL mode, which "
            "SQLite does not support over a network.\n\n"
            "While it stays in this mode, other PCs may be locked out and there is a risk "
            "to the data.\n\n"
            "It could not be switched automatically because another connection is holding "
            "the file. Ask everyone to close Nexus, then reopen it — the switch happens on "
            "startup and only needs to succeed once.")

    def backup_now(self):
        """Write a verified snapshot of the database, safe to run while it is in use."""
        db_path = get_db_path()
        if not db_path or not os.path.exists(db_path):
            QMessageBox.warning(self, "Backup", "There is no database to back up yet.")
            return

        stem = os.path.splitext(os.path.basename(db_path))[0]
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        suggested = os.path.join(os.path.dirname(db_path), f"{stem}_backup_{stamp}.db")

        dest, _ = QFileDialog.getSaveFileName(self, "Save Database Backup", suggested, "SQLite DB (*.db)")
        if not dest:
            return
        if not dest.lower().endswith(".db"):
            dest += ".db"

        ok, result = backup_database(db_path, dest)
        if not ok:
            QMessageBox.critical(self, "Backup Failed",
                f"No backup was written, and any previous backup at that name is untouched.\n\n{result}")
            return

        # A database backup alone is incomplete when 'file' attributes are in use —
        # the attachments live outside it, in the per-database files folder.
        files_dir = files_dir_for(db_path, create=False)
        note = ""
        if files_dir and os.path.isdir(files_dir):
            note = ("\n\nAttachments are stored separately in:\n"
                    f"{files_dir}\n"
                    "Copy that folder as well for a complete backup.")

        QMessageBox.information(self, "Backup Complete",
            f"Verified snapshot written to:\n{result}\n\nIntegrity check passed.{note}")

    def clean_unused_files(self):
        """Move any file in the storage folder that no record references into _trash."""
        db_path = get_db_path()
        files_dir = files_dir_for(db_path, create=False)
        if not files_dir or not os.path.isdir(files_dir):
            QMessageBox.information(self, "Clean Up", "There is no files folder yet — nothing to clean.")
            return

        referenced = set()
        try:
            with db_session(db_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT c.name, a.name FROM attributes a
                    JOIN classes c ON a.class_id = c.id
                    WHERE a.data_type = 'file'
                """)
                file_attrs = cur.fetchall()
                for class_name, attr_name in file_attrs:
                    table = f"objects_{sanitize_name(class_name)}"
                    col = sanitize_name(attr_name)
                    try:
                        cur.execute(f"SELECT {qid(col)} FROM {qid(table)} WHERE {qid(col)} IS NOT NULL")
                        referenced.update(r[0] for r in cur.fetchall() if r[0])
                    except sqlite3.OperationalError:
                        continue
        except sqlite3.OperationalError:
            pass

        moved = 0
        for entry in os.listdir(files_dir):
            full = os.path.join(files_dir, entry)
            if entry == "_trash" or not os.path.isfile(full):
                continue
            if entry not in referenced:
                trash_stored_file(db_path, entry)
                moved += 1

        QMessageBox.information(self, "Clean Up Complete",
            f"Moved {moved} unused file(s) to the _trash folder inside:\n{files_dir}")

    def sync_all_classes(self, db_path):
        with db_session(db_path) as conn:
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

    def refresh_sidebar(self, do_sync=True):
        self.sidebar.clear()
        db_path = get_db_path()

        if not os.path.exists(db_path):
            init_db(db_path)
        else:
            # Bring an existing database up to date without touching its data:
            # init_db is idempotent (CREATE IF NOT EXISTS + WAL + additive column
            # migrations), so this also backfills columns like relationships.is_required.
            try:
                init_db(db_path)
            except sqlite3.Error:
                pass

        if QSqlDatabase.contains(): db = QSqlDatabase.database()
        else: db = QSqlDatabase.addDatabase("QSQLITE")

        # The Qt connection needs the same lock patience as the sqlite3 ones, or the
        # table view fails instantly on a lock the writer clears a moment later.
        db.setConnectOptions(f"QSQLITE_BUSY_TIMEOUT={BUSY_TIMEOUT_MS}")
        db.setDatabaseName(db_path)
        if not db.open(): return

        self.warn_if_wal_on_network(db_path)

        if do_sync:
            # Drop any read lock the data view holds before sync_all_classes runs DDL,
            # and blank the stale class heading along with the grid.
            self.data_browser.clear_view()
            self.sync_all_classes(db_path)

        try:
            with db_session(db_path) as conn:
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
        db_path = get_db_path()
        if not db_path or not os.path.exists(db_path): return
        
        try:
            with db_session(db_path) as conn:
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
        db_path = get_db_path()

        with db_session(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, code FROM modules WHERE id = ?", (mid,))
            row = cur.fetchone()
        
        if not row or not row[1]:
            QMessageBox.warning(self, "Warning", "Module script is empty or missing.")
            return
            
        mname, code = row
        out_path = get_module_output_path()
        
        def local_get_objects(class_name):
            if pd is None: raise ImportError("Pandas library is not installed.")
            with db_session(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT id FROM classes WHERE name = ?", (class_name,))
                r = cur.fetchone()
                if not r:
                    raise ValueError(f"Class '{class_name}' not found.")
                cls_id = r[0]
                
            # parent_widget=None: a report run must never pop a modal schema/data-loss
            # dialog. If a destructive conversion is pending, sync raises and the error
            # is written to the output file instead of blocking the script.
            view_name, safe_table_name = sync_physical_table(db_path, cls_id, class_name, parent_widget=None)
            if not view_name: raise RuntimeError(f"Failed to sync schema for '{class_name}'.")
                
            full_view_name = f"full_view_{safe_table_name}"
            with db_session(db_path) as conn:
                df = pd.read_sql_query(f"SELECT * FROM {qid(full_view_name)}", conn)
            return df
            
        context = {'get_objects': local_get_objects, 'pd': pd}
        
        try:
            # Append (don't truncate) so each run keeps the previous output as history.
            with open(out_path, 'a', encoding='utf-8') as f:
                f.write(f"\n--- Running Module: {mname} at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n\n")
                with redirect_stdout(f), redirect_stderr(f): exec(code, context)
            QMessageBox.information(self, "Success", f"Script '{mname}' executed successfully.\nOutput appended to:\n{out_path}")
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
