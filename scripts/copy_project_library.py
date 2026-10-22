"""Copy projects/attachments from the old service through a read-only SQLite connection.
Never copy credentials, execution history, browser profiles, or tracing settings.
"""
import argparse
import json
import shutil
import sqlite3
from pathlib import Path

from crashcustomer.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    destination = Path(__file__).resolve().parent.parent / "data"
    if source == destination:
        raise SystemExit("Source and destination must differ")
    store = Store(destination)
    old = sqlite3.connect(f"file:{source / 'crashcustomer.sqlite3'}?mode=ro", uri=True)
    projects = old.execute("SELECT id,payload FROM projects").fetchall()
    attachments = old.execute("SELECT id,project_id,payload FROM attachments").fetchall()
    old.close()
    copied = 0
    with store.connect() as conn:
        for project_id, payload in projects:
            conn.execute("INSERT OR IGNORE INTO projects VALUES(?,?)", (project_id, payload))
        for attachment_id, project_id, payload in attachments:
            if len(attachment_id) != 32 or any(c not in "0123456789abcdef" for c in attachment_id):
                continue
            original = source / "uploads" / attachment_id
            if not original.is_file():
                continue
            if not (store.uploads / attachment_id).exists():
                shutil.copy2(original, store.uploads / attachment_id)
            conn.execute("INSERT OR IGNORE INTO attachments VALUES(?,?,?)", (attachment_id, project_id, payload))
            copied += 1
    print(json.dumps({"projects": len(projects), "attachments": copied, "source_read_only": True}))


if __name__ == "__main__":
    main()
