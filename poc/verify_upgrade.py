"""Verify SQLite schema upgrade on a backup copy, preserving existing table content."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from agent_workbench.server import migrate
from agent_workbench.settings import Settings
from agent_workbench.workspace import write_json


def table_digests(connection, original_columns=None):
    result = {}
    for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        if name == "alembic_version":
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        columns = [row[1] for row in connection.execute("PRAGMA table_info(" + quoted + ")")]
        if original_columns and name in original_columns: columns = original_columns[name]
        selected = ','.join('"' + column.replace('"', '""') + '"' for column in columns)
        values = sorted(connection.execute("SELECT " + selected + " FROM " + quoted).fetchall(), key=repr)
        result[name] = {"columns": columns, "rows": len(values), "sha256": hashlib.sha256(json.dumps(values, ensure_ascii=False, default=str).encode()).hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    source, root = args.database.resolve(strict=True), args.root.resolve()
    if root.exists():
        raise ValueError("Use a new verification directory")
    root.mkdir(parents=True, mode=0o700)
    destination = root / "workbench.db"
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as original, sqlite3.connect(destination) as copied:
        original.backup(copied)
    with sqlite3.connect(destination) as connection:
        before = table_digests(connection)
        previous_revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    # This is a metadata migration check, not a full workspace backup/restore.
    settings = Settings(root, f"sqlite:///{destination}", "fixture-user-token" * 2, "fixture-runner-token" * 2)
    migrate(settings)
    with sqlite3.connect(destination) as connection:
        after = table_digests(connection, {name: value["columns"] for name, value in before.items()})
        complete = table_digests(connection)
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert revision == "0009"
    assert all(after.get(name) == value for name, value in before.items())
    report = {"status": "PASSED_SQLITE_METADATA_UPGRADE", "from": previous_revision, "to": revision,
        "preserved_tables": before, "added_tables": sorted(set(after) - set(before)),
        "added_columns": {name: sorted(set(value["columns"]) - set(before.get(name, {}).get("columns", []))) for name, value in complete.items() if name in before and value["columns"] != before[name]["columns"]},
        "original_database_modified": False, "full_workspace_restore_tested": False}
    write_json(root / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
