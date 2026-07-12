"""Foundation layer for Nexus.

Pure helpers, the database bootstrap, and the physical-schema synchroniser.
No dialog classes live here — every GUI module (class_builder_dialog,
object_editor, data_browser, main) is built on top of this one.
"""
import sys
import os
import re
import ast
import uuid
import shutil
import sqlite3
import datetime

from PySide6.QtGui import QIcon
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QMessageBox


# ==========================================
# APP / DATABASE LOCATION
# ==========================================
def app_base_dir():
    """Folder the app runs from: next to the frozen executable when packaged
    (PyInstaller etc.), otherwise next to this source file. The default database
    is created here so it sits alongside the app instead of the user's home folder."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def default_db_path():
    """Default database location: nexus.db next to the executable/script."""
    return os.path.join(app_base_dir(), "nexus.db")


def get_db_path():
    """The single active database path shared by every entry point.

    Returns the user's saved choice (Settings dialog), or the default next to the
    app on first run — persisting it. Centralising this stops the main window and
    the builder dialogs from silently diverging onto different database files.
    """
    settings = QSettings("MyCompany", "DatabaseManagerApp")
    path = (settings.value("db_path", "") or "").strip()
    if not path:
        path = default_db_path()
        settings.setValue("db_path", path)
    return path


def get_app_icon():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return QIcon(os.path.join(base_dir, "resources", "app_image.ico"))


# ==========================================
# IDENTIFIERS
# ==========================================
def sanitize_name(name):
    if not name: return "unnamed"
    safe = re.sub(r'[^\w]', '_', str(name)).lower()
    safe = re.sub(r'_+', '_', safe).strip('_')
    if safe and safe[0].isdigit(): safe = "n_" + safe
    return safe if safe else "unnamed"


def qid(name):
    """Safely quote identifiers for SQLite."""
    if name is None: return ""
    return f"[{str(name).replace(']', ']]')}]"


# ==========================================
# VALUE TYPING (shared helpers)
# ==========================================
_TRUE_STRINGS = {"1", "true", "t", "yes", "y", "on"}
_FALSE_STRINGS = {"0", "false", "f", "no", "n", "off"}


def parse_boolean(val):
    """Interpret common truthy/falsy inputs as 1/0. Raise ValueError otherwise.

    Accepts 1/0, true/false, yes/no, y/n, t/f, on/off (case-insensitive) and the
    numeric literals 0 and 1. Used by both import parsing and type conversion so a
    human-filled template can say 'Yes' instead of only '1'.
    """
    if val is None:
        raise ValueError("empty")
    s = str(val).strip().lower()
    if s in _TRUE_STRINGS:
        return 1
    if s in _FALSE_STRINGS:
        return 0
    f = float(s)  # raises ValueError for non-numeric text
    i = int(f)
    if f == i and i in (0, 1):
        return i
    raise ValueError(f"not a boolean: {val}")


def storage_class_for(app_type):
    """The SQLite storage class an app type maps to (mirrors sync_physical_table)."""
    if app_type in ("int", "boolean"):
        return "INTEGER"
    if app_type == "float":
        return "REAL"
    return "TEXT"


def coerce_value_for_type(val, app_type, options=None, matrix_count=None):
    """Validate/convert a single stored value to an app type.

    Returns (ok, coerced_value). Blank/None is always (True, None). When ok is
    False the value is incompatible and the caller should clear it. Used when an
    attribute's type changes but its storage class does not (e.g. string->date,
    list->matrix, int->boolean), where sync_physical_table would not otherwise
    revalidate the existing data.
    """
    if val is None or str(val).strip() == "":
        return True, None
    s = str(val).strip()
    try:
        if app_type == "boolean":
            return True, parse_boolean(s)
        if app_type == "int":
            return True, int(float(s))
        if app_type == "float":
            return True, float(s)
        if app_type == "date":
            datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            return True, s
        if app_type in ("list", "matrix"):
            parsed = ast.literal_eval(s)
            if not isinstance(parsed, list):
                return False, None
            if app_type == "matrix":
                if matrix_count is not None and len(parsed) != matrix_count:
                    return False, None
                if any(not isinstance(inner, list) for inner in parsed):
                    return False, None
            return True, str(parsed)
        if app_type == "discrete":
            if options is not None and s not in options:
                return False, None
            return True, s
        # string / long string / look-through and anything else: keep as text
        return True, s
    except (ValueError, SyntaxError, TypeError):
        return False, None


# ==========================================
# FILE ATTACHMENTS (shared helpers)
# ==========================================
# A "file" attribute stores a reference string of the form  <uuid32hex>__<original name>
# which is ALSO the file's name on disk inside the per-database files folder.
# The generated views expose only the display part (substr from char 35), so the table,
# look-throughs and relationship titles all show the clean filename automatically.
FILES_SUBDIR_SUFFIX = "_files"
FILE_PREFIX_LEN = 34  # uuid4().hex (32) + "__"


def sanitize_filename(name):
    name = os.path.basename(str(name)).strip()
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name)
    return name or "file"


def make_stored_filename(original):
    """Build a collision-proof on-disk/reference name that keeps the original for display."""
    return f"{uuid.uuid4().hex}__{sanitize_filename(original)}"


def display_file_name(stored_value):
    if not stored_value:
        return ""
    return str(stored_value)[FILE_PREFIX_LEN:]


def files_dir_for(db_path, create=False):
    if not db_path:
        return None
    base = os.path.dirname(os.path.abspath(db_path))
    stem = os.path.splitext(os.path.basename(db_path))[0]
    d = os.path.join(base, stem + FILES_SUBDIR_SUFFIX)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def resolve_file_path(db_path, stored_value):
    d = files_dir_for(db_path, create=False)
    if not d or not stored_value:
        return None
    return os.path.join(d, str(stored_value))


def trash_stored_file(db_path, stored_value):
    """Move a stored file into the _trash folder (best-effort, recoverable)."""
    if not stored_value:
        return
    src = resolve_file_path(db_path, stored_value)
    if not src or not os.path.exists(src):
        return
    try:
        trash = os.path.join(files_dir_for(db_path, create=True), "_trash")
        os.makedirs(trash, exist_ok=True)
        dest = os.path.join(trash, str(stored_value))
        if os.path.exists(dest):
            dest = os.path.join(trash, f"{uuid.uuid4().hex[:8]}_{stored_value}")
        shutil.move(src, dest)
    except OSError:
        pass


# ==========================================
# DATABASE BOOTSTRAP
# ==========================================
def _ensure_column(conn, table, column, decl):
    """Add a column if the table lacks it — idempotent, non-destructive migration.

    CREATE TABLE IF NOT EXISTS never alters an existing table, so databases made
    before a field was introduced gain it here without losing any data.
    """
    existing = [r[1] for r in conn.execute(f"PRAGMA table_info({qid(table)})").fetchall()]
    if existing and column not in existing:
        conn.execute(f"ALTER TABLE {qid(table)} ADD COLUMN {qid(column)} {decl}")


def init_db(db_path=None):
    if db_path is None:
        db_path = default_db_path()
    with sqlite3.connect(db_path) as conn:
        # WAL lets readers (the Qt table view) and the writer (sqlite3 DDL/DML)
        # work on the same file concurrently without "database is locked" errors.
        # This is a persistent property of the file, so setting it once is enough.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys = 1")
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
                lookup_query TEXT DEFAULT '',
                FOREIGN KEY(class_id) REFERENCES classes(id) ON DELETE CASCADE
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
                show_in_base INTEGER DEFAULT 1,
                show_in_target INTEGER DEFAULT 1,
                is_required INTEGER DEFAULT 0,
                FOREIGN KEY(source_class) REFERENCES classes(id) ON DELETE CASCADE,
                FOREIGN KEY(target_class) REFERENCES classes(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS modules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                path TEXT DEFAULT '',
                code TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS discrete_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS discrete_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type_id INTEGER,
                value TEXT NOT NULL,
                row_order INTEGER DEFAULT 0,
                FOREIGN KEY(type_id) REFERENCES discrete_types(id) ON DELETE CASCADE
            );
        """)
        # Additive migrations for databases created before a column existed.
        _ensure_column(conn, "relationships", "is_required", "INTEGER DEFAULT 0")
        conn.commit()


# ==========================================
# PHYSICAL SCHEMA SYNCHRONIZATION
# ==========================================
def safe_convert(val, app_type):
    """Safely cast user values to target database data types, strictly failing if it's destructive."""
    if val is None or str(val).strip() == "": return True, None
    try:
        if app_type == "boolean":
            # Accept human-friendly truthy/falsy text (Yes/No/True/False/…), not just 0/1,
            # so hand-filled import templates aren't rejected.
            return True, parse_boolean(val)
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
    except (ValueError, SyntaxError, TypeError):
        return False, None


def sync_physical_table(db_path, class_id, class_name, parent_widget=None):
    safe_table_name = f"objects_{sanitize_name(class_name)}"

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()

        cur.execute("SELECT name, data_type, show_in_table, is_title, lookup_query, is_unique, is_required FROM attributes WHERE class_id = ? ORDER BY row_order", (class_id,))
        attributes = cur.fetchall()

        required_cols = []
        attr_app_types = {}
        unique_flags = {}    # safe_col_name -> bool (enforced via UNIQUE index)
        required_flags = {}  # safe_col_name -> bool (enforced via NOT NULL when feasible)

        for attr_name, attr_type, show_in_table, is_title, lookup_query, is_unique, is_required in attributes:
            safe_col_name = sanitize_name(attr_name)
            if attr_type == "look-through":
                attr_app_types[safe_col_name] = attr_type
                if lookup_query:
                    # Split on the FIRST dot only: the attribute part may itself contain dots.
                    parts = lookup_query.split('.', 1)
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
            unique_flags[safe_col_name] = bool(is_unique)
            required_flags[safe_col_name] = bool(is_required)

        def make_cols_def(notnull_map):
            return ", ".join([f"{qid(c)} {t}" + (" NOT NULL" if notnull_map.get(c) else "") for c, t in required_cols])

        cur.execute("SELECT count(name) FROM sqlite_master WHERE type='table' AND name=?", (safe_table_name,))
        if cur.fetchone()[0] == 0:
            # Fresh, empty table: every required column can safely carry NOT NULL.
            desired_notnull = dict(required_flags)
            cols_def = make_cols_def(desired_notnull)
            cur.execute(f"CREATE TABLE {qid(safe_table_name)} (id INTEGER PRIMARY KEY AUTOINCREMENT{', ' + cols_def if cols_def else ''})")
        else:
            cur.execute(f"PRAGMA table_info({qid(safe_table_name)})")
            info_rows = cur.fetchall()
            existing_cols = {row[1]: row[2] for row in info_rows}
            existing_notnull = {row[1]: row[3] for row in info_rows}

            cur.execute(f"SELECT COUNT(*) FROM {qid(safe_table_name)}")
            table_has_rows = cur.fetchone()[0] > 0

            type_mismatches = []
            for col_name, sql_type in required_cols:
                if col_name in existing_cols and existing_cols[col_name] != sql_type:
                    type_mismatches.append(col_name)

            cols_to_remove = set(existing_cols.keys()) - {c for c, t in required_cols} - {'id'}

            # Decide the NOT NULL constraint we actually want per column, degrading to
            # nullable wherever enforcing it could break existing data (so a rebuild can
            # never fail on a NOT NULL violation).
            desired_notnull = {}
            notnull_changes = []
            for col_name, sql_type in required_cols:
                want = required_flags.get(col_name, False)
                if want:
                    if col_name not in existing_cols:
                        # New column: only enforceable while the table is still empty.
                        if table_has_rows: want = False
                    elif col_name in type_mismatches:
                        # Conversion may null out incompatible values.
                        want = False
                    elif table_has_rows:
                        cur.execute(f"SELECT 1 FROM {qid(safe_table_name)} WHERE {qid(col_name)} IS NULL LIMIT 1")
                        if cur.fetchone(): want = False
                desired_notnull[col_name] = want

                if col_name in existing_cols:
                    if bool(existing_notnull.get(col_name, 0)) != want:
                        notnull_changes.append(col_name)
                elif want:
                    # New required column on an empty table -> rebuild to create it NOT NULL
                    # (ALTER TABLE ADD COLUMN cannot add a NOT NULL column without a default).
                    notnull_changes.append(col_name)

            if type_mismatches or cols_to_remove or notnull_changes:
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

                    cols_def = make_cols_def(desired_notnull)
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

        # --- Schema-level UNIQUE enforcement via standalone indexes ---
        # (ALTER TABLE cannot add a UNIQUE column, so unique constraints live in indexes
        # named ux_<table>_<col>. SQLite unique indexes treat multiple NULLs as distinct,
        # which matches the app's "uniqueness ignores empty values" behaviour.)
        ux_prefix = f"ux_{safe_table_name}_"
        cur.execute(f"PRAGMA index_list({qid(safe_table_name)})")
        existing_index_names = [r[1] for r in cur.fetchall()]
        desired_unique_cols = [c for c, _t in required_cols if unique_flags.get(c)]

        for idx_name in existing_index_names:
            if idx_name.startswith(ux_prefix) and idx_name[len(ux_prefix):] not in desired_unique_cols:
                cur.execute(f"DROP INDEX IF EXISTS {qid(idx_name)}")

        for c in desired_unique_cols:
            idx_name = ux_prefix + c
            if idx_name not in existing_index_names:
                try:
                    cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {qid(idx_name)} ON {qid(safe_table_name)} ({qid(c)})")
                except (sqlite3.IntegrityError, sqlite3.OperationalError):
                    # Existing duplicate values (app-level checks normally prevent this).
                    # Skip schema-level enforcement rather than abort the whole sync.
                    pass

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
        for attr_name, attr_type, show_in_table, is_title, lookup_query, is_unique, is_required in attributes:
            safe_col_name = sanitize_name(attr_name)
            if attr_type == "look-through":
                if lookup_query:
                    # Split on the FIRST dot only: the attribute part may itself contain dots.
                    parts = lookup_query.split('.', 1)
                    if len(parts) == 2:
                        tgt_class, tgt_attr = parts
                        safe_tgt_class = sanitize_name(tgt_class.strip())
                        junc_table = f"rel_{safe_table_name}_to_objects_{safe_tgt_class}"
                        # A look-through resolves through the relationship's junction table.
                        # If no such relationship exists (junction missing), fall back to NULL
                        # so existing data still loads instead of breaking the whole view.
                        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (junc_table,))
                        if cur.fetchone():
                            subquery = f"(SELECT GROUP_CONCAT(tgt_v.{qid(tgt_attr.strip())}) FROM {qid(junc_table)} j JOIN {qid('base_view_objects_' + safe_tgt_class)} tgt_v ON j.target_id = tgt_v.[ID] WHERE j.source_id = m.id)"
                            base_selects.append(f"{subquery} AS {qid(attr_name)}")
                        else:
                            base_selects.append(f"NULL AS {qid(attr_name)}")
                    else:
                        raise ValueError(f"Invalid lookup format '{lookup_query}'. Expected 'TargetClass.Attribute'.")
                else:
                    base_selects.append(f"NULL AS {qid(attr_name)}")
            elif attr_type == "file":
                # The raw column holds "<uuid>__name"; expose only the display name so the
                # table, look-throughs and relationship titles show the clean filename.
                base_selects.append(f"substr(m.{qid(safe_col_name)}, {FILE_PREFIX_LEN + 1}) AS {qid(attr_name)}")
            else:
                base_selects.append(f"m.{qid(safe_col_name)} AS {qid(attr_name)}")

        base_view_name = f"base_view_{safe_table_name}"
        cur.execute(f"DROP VIEW IF EXISTS {qid(base_view_name)}")
        cur.execute(f"CREATE VIEW {qid(base_view_name)} AS SELECT {', '.join(base_selects)} FROM {qid(safe_table_name)} m")

        ui_selects = ["v.[ID]"]
        full_selects = ["v.[ID]"]

        for attr_name, attr_type, show_in_table, is_title, lookup_query, is_unique, is_required in attributes:
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
