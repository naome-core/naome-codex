"""Exercise one Codex session against two independent NAOME lab networks."""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from integration import ENTRY, NAOME, cli, initialize, listener, until


def archive_records(node):
    head = cli(node, "archive", "snapshot", "Turn routing test readback")
    ids = cli(node, "archive", "discover", head)["ids"]
    return {item: cli(node, "archive", "show", head, item) for item in ids}


def main():
    with tempfile.TemporaryDirectory(prefix="naome-turn-routing-") as scratch:
        temp = Path(scratch).resolve()
        hubs, labs = [[temp / f"{role}-{name}" for name in "AB"] for role in ("hub", "lab")]
        jobs, held, running = [], [], []
        try:
            for hub, lab in zip(hubs, labs):
                hub_id, lab_id = initialize(hub), initialize(lab)
                hub_port, hub_address = listener()
                lab_port, lab_address = listener()
                held.extend([hub_port, lab_port])
                for node, role, own, pid, peer, port in (
                    (hub, "orchestrator", hub_address, lab_id, lab_address, hub_port),
                    (lab, "lab", lab_address, hub_id, hub_address, lab_port),
                ):
                    (node / "node.json").write_text(json.dumps({
                        "role": role, "listen": own, "peers": [{"id": pid, "address": peer}],
                    }))
                    port.close()
                    assert cli(node, "start")["running"]
                    running.append(node)
                job = cli(hub, "research", "assign", lab_id, "research", f"Verify {lab.name}")
                jobs.append(job)
                until(lambda: cli(lab, "research", "jobs"),
                      lambda rows: any(row["id"] == job and row["input_ready"] for row in rows))

            workspace = temp / "work"
            workspace.mkdir()
            config = temp / "config.json"
            journal = temp / "journal.sqlite"
            wrapper = temp / "naome-wrapper"
            halt_local, halt_report = temp / "halt-local", temp / "halt-report"
            wrapper.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                f"if args[1] == {str(labs[1])!r}:\n"
                f"    if (args[3:5] == ['archive', 'put'] and Path({str(halt_local)!r}).exists()\n"
                f"        or args[3:5] == ['research', 'stream'] and Path({str(halt_report)!r}).exists()):\n"
                "        sys.exit(72)\n"
                f"os.execv({str(NAOME)!r}, [{str(NAOME)!r}, *args])\n"
            )
            wrapper.chmod(0o700)
            config.write_text(json.dumps({
                "naome": str(wrapper), "projects": {"research": {
                    "workspace": str(workspace), "default_lab": "A",
                    "labs": {name: {"directory": str(lab)} for name, lab in zip("AB", labs)},
                }},
            }))
            env = dict(os.environ, NAOME_CODEX_CONFIG=str(config), NAOME_CODEX_STATE=str(journal))
            session = "one-codex-chat"

            # A pending entry from before turn routing must retain its original lab and bytes.
            old_doc = {"kind": "claim", "subject": "Pending legacy evidence", "scope": "research",
                       "fields": {"codex_session": session, "category": "hypothesis"}, "inputs": []}
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
                db.execute("INSERT INTO binding VALUES (?,?,NULL,?,?)", (session, "research", str(labs[0]), "legacy"))
                db.execute("INSERT INTO entry(session,offset,event,document) VALUES (?,0,?,?)",
                           (session, "legacy-event", json.dumps(old_doc)))

            def entry(mode, payload):
                raw = subprocess.check_output([sys.executable, ENTRY, mode], input=json.dumps(payload) + "\n",
                                              text=True, env=env)
                return json.loads(raw)

            def hook(event, turn, **fields):
                return entry("hook", {"hook_event_name": event, "turn_id": turn,
                                      "session_id": session, "cwd": str(workspace), **fields})

            def choose(turn, prompt, lab=None):
                result = hook("UserPromptSubmit", turn, prompt=prompt)
                context = result["hookSpecificOutput"]["additionalContext"]
                if lab is None:
                    assert "No Naome writes are enabled" in context and "binding_token " not in context, context
                    return None
                assert f"selected lab {lab} for this turn" in context, context
                return re.search(r"binding_token ([A-Za-z0-9_-]+)", context)[1]

            def tool(token, name, error=False, **fields):
                response = entry("mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": name, "arguments": {"binding_token": token, **fields},
                }})["result"]
                assert response["isError"] == error, response
                return None if error else json.loads(response["content"][0]["text"])

            def record(token, title, error=False):
                return tool(token, "record_research", error, title=title, category="hypothesis",
                            summary="The fixture is confined to its selected lab.", sources=[])

            def tool_event(turn, call):
                return hook("PostToolUse", turn, tool_name="Bash", tool_use_id=call,
                            tool_input={"command": "fixture"}, tool_response={"exit_code": 0})

            tool("legacy", "research_status", error=True)
            a = choose("one", "Record the first result.", "A")
            first = record(a, "Result in lab A")
            assert (first["lab"], first["job"]) == ("A", jobs[0])
            halt_local.touch()
            halt_report.touch()
            b = choose("two", "Für diesen Schritt bitte Labor B verwenden.\nRecord a second result.", "B")
            pending = record(b, "Deferred result in lab B")
            assert (pending["lab"], pending["job"]) == ("B", jobs[1])
            assert pending["local_record"] is None and pending["pending_local"] == 1, pending
            assert "pending local queue" in json.dumps(tool_event("two", "deferred-tool"))
            a2 = choose("three", "Continue with the chat default.", "A")
            assert len({a, a2, b}) == 3
            record(b, "A stale token must never write", error=True)
            assert first["local_record"] not in archive_records(labs[1])
            choose("two", "/lab default A")
            tool(b, "research_status", error=True)

            halt_local.unlink()
            # The old turn's hook arrives after the current turn already selected A.
            delayed = tool_event("two", "late-tool")
            assert "in the local archive" in json.dumps(delayed)
            assert tool_event("two", "late-tool") == delayed
            tool(a2, "retry_research_delivery")
            a_docs, b_docs = [archive_records(lab) for lab in labs]
            assert first["local_record"] in a_docs and first["local_record"] not in b_docs
            assert sum(d["subject"] == old_doc["subject"] for d in a_docs.values()) == 1
            assert all(d["subject"] != old_doc["subject"] for d in b_docs.values())
            b_records = {identifier: doc for identifier, doc in b_docs.items()
                         if doc["fields"].get("codex_session") == session}
            assert not (b_records.keys() & a_docs.keys())
            assert sorted(doc["fields"].get("tool_call_id", "research") for doc in b_records.values()) == [
                "deferred-tool", "late-tool", "research",
            ]
            assert all(doc["fields"]["codex_turn"] == "two" for doc in b_records.values())
            assert all(doc["fields"]["naome_job"] == jobs[1] for doc in b_records.values())
            halt_report.unlink()
            tool(a2, "retry_research_delivery")

            for hub, expected in zip(hubs, ({first["local_record"]}, set(b_records))):
                def published_ids():
                    found = set()
                    for head in cli(hub, "archive", "heads"):
                        found.update(cli(hub, "archive", "discover", head)["ids"])
                    return found

                observed = until(published_ids, expected.issubset)
                all_recorded = {first["local_record"], *b_records}
                assert observed & all_recorded == expected

            choose("four", "Vergleiche die Ergebnisse von Labor A und B und speichere den Befund.")
            assert tool_event("four", "ambiguous-tool") == {}
            tool(a2, "research_status", error=True)
            choose("five", "/lab unknown")
            choose("six", "Ab jetzt Labor B.", "B")
            choose("seven", "Continue here.", "B")
            choose("eight", "/lab A", "A")
            b2 = choose("nine", "Continue here.", "B")
            assert hook("UserPromptSubmit", "outside", prompt="Continue.", cwd=str(temp)) == {}
            tool(b2, "research_status", error=True)
            choose("ten", "/lab A\n/lab B")
            with sqlite3.connect(journal) as db:
                assert db.execute("SELECT count(*) FROM entry WHERE record IS NULL OR (job IS NOT NULL AND head IS NULL)").fetchone()[0] == 0
            print("PASS: one chat, two labs and orchestrators, fixed turn routing, delayed local and network delivery")
        finally:
            for port in held:
                port.close()
            for node in reversed(running):
                cli(node, "stop")


if __name__ == "__main__":
    main()
