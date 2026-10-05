import os, sqlite3
from contextlib import contextmanager
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    c = sqlite3.connect(db_path(), timeout=5)
    c.row_factory = sqlite3.Row
    return c

@contextmanager
def immediate_tx(c):
    """Serialize writers (preview confirm vs expire-sweep vs other consumes).

    Manage transactions explicitly: python's sqlite3 would otherwise open an
    implicit transaction at an arbitrary statement.
    """
    c.isolation_level = None
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.isolation_level = ""
