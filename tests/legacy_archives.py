"""Upgrade synthetic archives written by Codex commit 38191b3 with Network v0.4.0."""

import gzip
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from integration import NAO, call, cli, prompt, value

FIXTURES = Path(__file__).with_name("fixtures")


def main():
    fixtures = json.loads((FIXTURES / "legacy-archives.json").read_text())
    with tempfile.TemporaryDirectory(prefix="naome-archive-upgrade-") as scratch:
        root = Path(scratch).resolve()
        workspace, config, journal = (root / name for name in ("workspace", "config.json", "journal.sqlite"))
        workspace.mkdir()
        env = dict(os.environ, NAOME_CODEX_CONFIG=str(config), NAOME_CODEX_STATE=str(journal))
        settings = {"nao": str(NAO), "naome": str(root / "retired-executable"), "projects": {"fixture": {
            "workspace": str(workspace), "default_lab": "alpha", "labs": {},
        }}}
        labs = settings["projects"]["fixture"]["labs"]
        with sqlite3.connect(journal) as db:
            db.executescript("""
                CREATE TABLE binding (
                    session TEXT PRIMARY KEY, project TEXT NOT NULL, job TEXT,
                    lab TEXT NOT NULL, token TEXT NOT NULL UNIQUE, UNIQUE(lab, job)
                );
                CREATE TABLE entry (
                    session TEXT NOT NULL, offset INTEGER NOT NULL, event TEXT NOT NULL,
                    document TEXT NOT NULL, job TEXT, stream_offset INTEGER, record TEXT, head TEXT,
                    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(session, offset), UNIQUE(session, event)
                );
            """)
            for name, data in fixtures.items():
                lab = root / name
                lab.mkdir()
                (lab / "archive.sqlite").write_bytes(gzip.decompress((FIXTURES / f"legacy-{name}.sqlite.gz").read_bytes()))
                labs[name] = {"directory": str(lab), "search_head": data["snapshot"]}
                session = "legacy-" + name
                db.execute("INSERT INTO binding VALUES (?,?,?,?,?)", (session, "fixture", data["job"], str(lab), session))
                # Beta was written to the archive before its SQLite receipt was committed.
                db.execute("INSERT INTO entry VALUES (?,0,?,?,?,?,?,NULL,?)", (
                    session, data["event"], json.dumps(data["document"]), data["job"],
                    0 if data["job"] else None, None if name == "beta" else data["record"], data["captured_at"],
                ))
        config.write_text(json.dumps(settings))

        for name, data in fixtures.items():
            token, _ = prompt(env, workspace, "legacy-" + name, "upgraded")
            assert value(env, "research_status", binding_token=token)["chat_pending_local"] == 0
            recent = call(env, "list_recent_research", binding_token=token, lab=name)
            assert recent["structuredContent"]["records"][0]["record_id"] == data["record"]
            details = recent["_meta"]["naome/research-details"][data["record"]]
            assert details["summary"] == data["document"]["fields"]["summary"]
            old = call(env, "search_research", binding_token=token, lab=name, query=data["record"])
            search = old["structuredContent"]
            assert search["head"] == data["snapshot"] and search["hits"][0]["record_id"] == data["record"]
            lab = root / name
            document = cli(lab, "show", data["snapshot"], data["record"])
            assert document["subject"] == data["title"] and document["fields"].get("naome_job") == data["job"]

            value(env, "select_lab", binding_token=token, lab=name)
            title = f"Upgraded {name} evidence"
            created = call(env, "record_research", binding_token=token, category="result", title=title,
                           summary=f"Readback after upgrade for {name}.", sources=[], linked_records=[data["record"]])
            card = created["structuredContent"]
            assert card["title"] == title and card["lab"] == name
            identifier = card["record_id"]
            new_head = cli(lab, "snapshot", "New evidence", "--root", identifier)
            labs[name]["search_head"] = new_head
            config.write_text(json.dumps(settings))

            found = call(env, "search_research", binding_token=token, lab=name, query="upgraded")
            hit, = found["structuredContent"]["hits"]
            assert hit["record_id"] == identifier and hit["links"] == [{"role": "references", "target": data["record"]}]
            assert cli(lab, "show", new_head, identifier)["subject"] == title
            assert not (lab / "node.json").exists() and not (lab / "identity.key").exists()

        with sqlite3.connect(journal) as db:
            recovered = db.execute("SELECT record,job,head FROM entry WHERE session=? AND offset=0", ("legacy-beta",))
            beta = fixtures["beta"]
            assert recovered.fetchone() == (beta["record"], beta["job"], None)
            assert db.execute("SELECT count(*) FROM entry WHERE record IS NULL").fetchone()[0] == 0
        print("PASS: two pre-upgrade SQLite archives and their journal retain record IDs, snapshots, and local writes.")


if __name__ == "__main__":
    main()
