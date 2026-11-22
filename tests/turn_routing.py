"""Exercise separate archives and delayed local receipt retries in one Codex chat."""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from integration import NAO, cli, initialize, invoke, prompt, value


def main():
    with tempfile.TemporaryDirectory(prefix="naome-turn-routing-") as scratch:
        root = Path(scratch).resolve()
        workspace, lab_a, lab_b = (root / name for name in ("work", "a", "b"))
        workspace.mkdir()
        for lab in (lab_a, lab_b):
            initialize(lab)
        config, journal, hold = (root / name for name in ("config.json", "journal.sqlite", "hold"))
        wrapper = root / "protocol-wrapper"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            f"if args[1:3] == [{str(lab_b / 'archive.sqlite')!r}, 'put'] and Path({str(hold)!r}).exists():\n"
            "    sys.exit(72)\n"
            f"os.execv({str(NAO)!r}, [{str(NAO)!r}, *args])\n"
        )
        wrapper.chmod(0o700)
        settings = {"nao": str(wrapper), "projects": {"fixture": {
            "workspace": str(workspace), "default_lab": "A",
            "labs": {"A": {"directory": str(lab_a)}, "B": {"directory": str(lab_b)}},
        }}}
        project = settings["projects"]["fixture"]
        config.write_text(json.dumps(settings))
        env = dict(os.environ, NAOME_CODEX_CONFIG=str(config), NAOME_CODEX_STATE=str(journal))
        session = "one-codex-chat"

        def archive(token, title):
            return value(env, "record_research", binding_token=token, category="hypothesis",
                         title=title, summary="A distinct local destination.", sources=[])

        def tool_event(turn, identifier):
            return invoke(env, "hook", {"hook_event_name": "PostToolUse", "cwd": str(workspace),
                                        "session_id": session, "turn_id": turn, "tool_name": "Bash",
                                        "tool_use_id": identifier, "tool_input": {}, "tool_response": {"exit_code": 0}})

        a, _ = prompt(env, workspace, session, "one")
        first = archive(a, "Evidence for A")
        assert first["lab"] == "A" and first["local_record"]

        hold.touch()
        b, _ = prompt(env, workspace, session, "two")
        assert value(env, "select_lab", binding_token=b, lab="B")["lab"] == "B"
        pending = archive(b, "Evidence for B")
        assert pending["lab"] == "B" and pending["local_record"] is None and pending["pending_local"] == 1
        assert "pending local queue" in json.dumps(tool_event("two", "deferred"))
        assert "before recording" in value(env, "select_lab", error=True, binding_token=b, lab="A", remember=True)

        a2, context = prompt(env, workspace, session, "three")
        assert "2 pending local writes" in context
        status = value(env, "research_status", binding_token=a2)
        assert status["lab"] == "A" and status["pending_local"] == 0 and status["chat_pending_local"] == 2
        assert "no valid" in value(env, "research_status", error=True, binding_token=b)
        assert "no longer" in json.dumps(invoke(env, "hook", {
            "hook_event_name": "UserPromptSubmit", "cwd": str(workspace), "session_id": session, "turn_id": "two",
        }))

        removed = project["labs"].pop("B")
        config.write_text(json.dumps(settings))
        hold.unlink()
        assert tool_event("two", "unconfigured") == {}
        assert value(env, "retry_research_delivery", binding_token=a2)["chat_pending_local"] == 2
        project["labs"]["B"] = removed
        config.write_text(json.dumps(settings))
        late = tool_event("two", "late")
        assert "in the local archive as" in json.dumps(late) and tool_event("two", "late") == late
        assert value(env, "retry_research_delivery", binding_token=a2)["chat_pending_local"] == 0

        selected = value(env, "list_recent_research", binding_token=a2, lab="B")["records"][0]
        assert selected["title"] == "Evidence for B"
        head = cli(lab_b, "snapshot", "Archive B", "--root", selected["record_id"])
        assert cli(lab_b, "show", head, selected["record_id"])["subject"] == "Evidence for B"
        assert value(env, "list_recent_research", binding_token=a2)["records"][0]["record_id"] == first["local_record"]
        with sqlite3.connect(journal) as db:
            assert db.execute("SELECT count(*) FROM entry WHERE record IS NULL").fetchone()[0] == 0

        del project["default_lab"]
        config.write_text(json.dumps(settings))
        unselected, context = prompt(env, workspace, session, "four")
        assert "No lab is selected" in context
        assert "Use select_lab" in value(env, "record_research", error=True, binding_token=unselected,
                                        category="hypothesis", title="Needs a lab", summary="No default.", sources=[])
        value(env, "select_lab", binding_token=unselected, lab="B", remember=True)
        _, remembered = prompt(env, workspace, session, "five")
        assert "selected lab B" in remembered
        print("PASS: lab routing, late events, destination retention, and local retry.")


if __name__ == "__main__":
    main()
