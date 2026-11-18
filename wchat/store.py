import fcntl
import os
from pathlib import Path
import re
import sqlite3


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = open(self.directory / "client.lock", "a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("С этим хранилищем уже работает другой wchat") from None
        self.db = sqlite3.connect(self.directory / "history.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                name TEXT PRIMARY KEY, cwd TEXT NOT NULL, thread_id TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(name),
                prompt TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
                response TEXT NOT NULL DEFAULT '', turn_id TEXT, error TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
        """)
        # A crash may have left effects in the workspace. Never replay implicitly.
        self.db.execute("UPDATE jobs SET status='unknown' WHERE status IN ('starting','running')")
        self.db.execute("UPDATE jobs SET status='needs_review' WHERE status='queued'")
        self.db.commit()
        os.chmod(self.directory / "history.sqlite", 0o600)

    def close(self):
        self.db.close()
        self.lock.close()

    def session(self, name):
        row = self.db.execute("SELECT * FROM sessions WHERE name=?", (name,)).fetchone()
        if row is None:
            raise ValueError(f"Нет диалога {name!r}; используйте /new")
        return dict(row)

    def create(self, name, cwd):
        if not re.fullmatch(r"[\w-]{1,64}", name):
            raise ValueError("Имя диалога: 1-64 буквы, цифры, дефис или подчёркивание")
        path = Path(cwd).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"Нет рабочей папки: {path}")
        try:
            self.db.execute("INSERT INTO sessions(name,cwd) VALUES (?,?)", (name, str(path)))
        except sqlite3.IntegrityError:
            raise ValueError(f"Диалог {name!r} уже существует") from None
        self.db.commit()
        self.select(name)

    @property
    def active(self):
        row = self.db.execute("SELECT value FROM settings WHERE key='active'").fetchone()
        return row[0] if row else None

    def select(self, name):
        self.session(name)
        self.db.execute("INSERT OR REPLACE INTO settings VALUES ('active',?)", (name,))
        self.db.commit()

    def sessions(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM sessions ORDER BY name")]

    def setting(self, key, default=""):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @property
    def default_cwd(self):
        return self.setting("default_cwd", str(Path.home() / "Documents/jack"))

    def save_context(self, context, cwd):
        if not isinstance(context, str) or len(context) > 64000:
            raise ValueError("Общий контекст: не более 64000 символов")
        path = Path(cwd).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"Нет рабочей папки: {path}")
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                                [("common_context", context), ("default_cwd", str(path))])

    def thread(self, name, thread_id):
        self.db.execute("UPDATE sessions SET thread_id=? WHERE name=?", (thread_id, name))
        self.db.commit()

    def enqueue(self, session, prompt, source):
        self.session(session)
        if not prompt.strip() or len(prompt) > 32000:
            raise ValueError("Поручение должно содержать от 1 до 32000 символов")
        cur = self.db.execute("INSERT INTO jobs(session,prompt,source,status) VALUES (?,?,?,'queued')",
                              (session, prompt.strip(), source))
        self.db.commit()
        return cur.lastrowid

    def update(self, job_id, **fields):
        allowed = {"status", "response", "turn_id", "error"}
        if not fields.keys() <= allowed:
            raise ValueError("Unknown job fields")
        columns = ",".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*fields.values(), job_id))
        self.db.commit()

    def job(self, job_id):
        return dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def jobs(self, session=None, limit=20):
        where, args = ("WHERE session=?", [session]) if session else ("", [])
        return [dict(r) for r in self.db.execute(
            f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT ?", (*args, limit))]
