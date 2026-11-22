"""Exercise hooks and stdio tools against two actual NAOME Network v0.2.0 nodes."""

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "plugin" / "entry.py"
NAOME = Path(os.environ.get("NAOME_TEST_BINARY", str(Path.home() / ".cargo/bin/naome")))


def cli(node, *args):
    raw = subprocess.check_output([NAOME, "--directory", node, "--json", *args], text=True)
    return json.loads(raw) if raw.strip() else None


def until(get, condition):
    deadline = time.monotonic() + 25
    while True:
        result = get()
        if condition(result):
            return result
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out: {result}")
        time.sleep(0.1)


def initialize(node):
    return cli(node, "archive", "init")["peer_id"]


def listener():
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    return held, f"/ip4/127.0.0.1/tcp/{held.getsockname()[1]}"


def main():
    assert subprocess.check_output([NAOME, "--version"], text=True).strip() == "naome 0.2.0"
    with tempfile.TemporaryDirectory(prefix="naome-codex-integration-") as scratch:
        temp = Path(scratch)
        hub, lab = temp / "hub", temp / "lab"
        hub_id, lab_id = initialize(hub), initialize(lab)
        assert cli(hub, "archive", "capabilities")["version"] == "0.1.2"
        held1, addr1 = listener()
        held2, addr2 = listener()
        for node, role, own, pid, peer in (
            (hub, "orchestrator", addr1, lab_id, addr2), (lab, "lab", addr2, hub_id, addr1),
        ):
            (node / "node.json").write_text(json.dumps({
                "role": role, "listen": own, "peers": [{"id": pid, "address": peer}],
            }))
        work = temp / "work"
        work.mkdir()
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        desk = temp / "desk"
        desk.mkdir()
        others = temp / "unrelated"
        others.mkdir()
        projects = {
            "repo-research": {"workspace": str(work), "lab": str(lab)},
            "desk-research": {"workspace": str(desk), "lab": str(lab)},
        }
        config = temp / "config.json"
        config.write_text(json.dumps({"naome": str(NAOME), "projects": projects}))
        env = dict(os.environ, NAOME_CODEX_CONFIG=str(config), NAOME_CODEX_STATE=str(temp / "queue.sqlite"))

        def hook(event, workspace, session, **extra):
            payload = {"hook_event_name": event, "cwd": str(workspace),
                       "session_id": session, "turn_id": "turn-one", **extra}
            return json.loads(subprocess.check_output(
                [sys.executable, ENTRY, "hook"], input=json.dumps(payload), text=True, env=env,
            ))

        def mcp(messages):
            wire = "".join(json.dumps({"jsonrpc": "2.0", **item}) + "\n" for item in messages)
            output = subprocess.check_output([sys.executable, ENTRY, "mcp"], input=wire, text=True, env=env)
            return [json.loads(line) for line in output.splitlines()]

        def tool(name, token, **kwargs):
            row = mcp([{"id": 5, "method": "tools/call", "params": {
                "name": name, "arguments": {"binding_token": token, **kwargs},
            }}])[0]["result"]
            return row["isError"], row["content"][0]["text"]

        def token_for(workspace, session, turn="turn-one"):
            result = hook("UserPromptSubmit", workspace, session, prompt="Continue.", turn_id=turn)
            context = result["hookSpecificOutput"]["additionalContext"]
            return re.search(r"binding_token ([A-Za-z0-9_-]+)", context).group(1)

        assert hook("UserPromptSubmit", others, "unrelated", prompt="Continue.") == {}
        assert not (temp / "queue.sqlite").exists()
        responses = mcp([{"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                         {"method": "notifications/initialized"}, {"id": 2, "method": "tools/list"}])
        assert responses[0]["result"]["protocolVersion"] == "2025-11-25"
        assert {item["name"] for item in responses[1]["result"]["tools"]} == {
            "research_status", "record_research", "retry_research_delivery",
        }

        def show_local(node, identifier):
            snapshot = cli(node, "archive", "snapshot", "Integration local readback",
                           "--root", identifier)
            return cli(node, "archive", "show", snapshot, identifier)

        # No node process or configured orchestrator is needed to capture evidence.
        lab_config_path = lab / "node.json"
        lab_config = json.loads(lab_config_path.read_text())
        lab_config_path.write_text(json.dumps({**lab_config, "peers": []}))
        assert not cli(hub, "status")["running"] and not cli(lab, "status")["running"]
        token = token_for(work, "repository-session", "offline-capture")
        assert json.loads(tool("research_status", token)[1])["job"] is None
        error, response = tool("record_research", token, category="hypothesis",
                               title="Offline hypothesis", summary="The archive is available locally.",
                               sources=[])
        status = json.loads(response)
        assert not error and status["job"] is None
        assert status["pending_local"] == status["pending_reports"] == 0
        offline = show_local(lab, status["local_record"])
        assert offline["subject"] == "Offline hypothesis" and "naome_job" not in offline["fields"]
        config.write_text(json.dumps({"naome": str(temp / "unavailable"), "projects": projects}))
        error, response = tool("record_research", token, category="method",
                               title="Deferred locally", summary="Repeat the readback.", sources=[])
        status = json.loads(response)
        assert not error and status["pending_local"] == 1 and status["local_record"] is None
        config.write_text(json.dumps({"naome": str(NAOME), "projects": projects}))
        token = token_for(work, "repository-session", "offline-retry")
        recovered = json.loads(tool("research_status", token)[1])
        assert recovered["pending_local"] == recovered["pending_reports"] == 0
        assert show_local(lab, recovered["local_record"])["subject"] == "Deferred locally"
        lab_config_path.write_text(json.dumps(lab_config))

        started = []
        try:
            for node, held in ((hub, held1), (lab, held2)):
                held.close()
                started.append(node)
                assert cli(node, "start")["running"]
            repository_job = cli(hub, "research", "assign", lab_id, "repo-research", "Check an operation")
            desk_job = cli(hub, "research", "assign", lab_id, "desk-research", "Compare two public sources")
            until(lambda: cli(lab, "research", "jobs"),
                  lambda jobs: {repository_job, desk_job} <= {job["id"] for job in jobs if job["input_ready"]})
            token = token_for(work, "repository-session")
            desk_token = token_for(desk, "desk-session")
            assert token != desk_token
            extra = token_for(work, "second-repository-session")
            assert json.loads(tool("research_status", extra)[1])["job"] is None
            assert tool("research_status", "invalid")[0]
            assert json.loads(tool("research_status", token)[1])["job"] == repository_job
            assert json.loads(tool("research_status", desk_token)[1])["job"] == desk_job
            assert not cli(hub, "stop")["running"]

            event = {"tool_name": "Bash", "tool_use_id": "call-one", "tool_input": {"command": "private text"},
                     "tool_response": {"exit_code": 0, "stdout": "private output"}}
            assert hook("PostToolUse", others, "repository-session", **event) == {}
            original = hook("PostToolUse", work, "repository-session", **event)
            assert original == hook("PostToolUse", work, "repository-session", **event)
            hook("Interrupt", work, "repository-session")
            captured = json.loads(tool("research_status", token)[1])
            assert captured["pending_local"] == 0 and captured["pending_reports"] == 2
            assert show_local(lab, captured["local_record"])["fields"]["hook_event"] == "Interrupt"

            # Persist the first segment in the real lab while hiding the acknowledgment.
            wrapper = temp / "lost-ack.py"
            wrapper.write_text(
                "#!" + sys.executable + "\n"
                "import subprocess, sys\n"
                f"binary = {str(NAOME)!r}\n"
                "args = sys.argv[1:]\n"
                "if 'stream' in args:\n"
                "    line = sys.stdin.readline()\n"
                "    subprocess.run([binary, *args], input=line, text=True, check=True)\n"
                "    sys.exit(72)\n"
                "sys.exit(subprocess.call([binary, *args]))\n"
            )
            wrapper.chmod(0o700)
            config.write_text(json.dumps({"naome": str(wrapper), "projects": projects}))
            failed = json.loads(tool("retry_research_delivery", token)[1])
            assert failed["pending_local"] == 0 and failed["pending_reports"] == 2 and "report_retry" in failed
            config.write_text(json.dumps({"naome": str(NAOME), "projects": projects}))
            token = token_for(work, "repository-session", "turn-two")
            assert json.loads(tool("research_status", token)[1])["pending_reports"] == 0

            assert tool("record_research", token, category="result", title="Missing source",
                        summary="This result has no evidence.", sources=[])[0]
            failed, result = tool("record_research", token, category="result", title="Operation completed",
                                  summary="The observed command returned zero.", sources=["tool:turn-one:call-one"])
            assert not failed and json.loads(result)["pending_reports"] == 0
            assert hook("Stop", work, "repository-session", turn_id="turn-two") == {}
            assert not tool("record_research", desk_token, category="result", title="Source comparison",
                            summary="The supplied documents describe different populations.",
                            sources=["Case fixture A, section 1", "Case fixture B, section 2"])[0]
            assert hook("Stop", desk, "desk-session") == {}
            assert cli(hub, "start")["running"]

            def reports():
                rows = cli(hub, "research", "events")
                return [row["event"]["report"] for row in rows
                        if row["event"]["kind"] == "report" and row["event"]["report"]["segment"]]

            delivered = until(reports, lambda items: len(items) >= 6)
            assert [row["segment"]["offset"] for row in delivered if row["job"] == repository_job] == [0, 1, 2, 3]
            assert [row["segment"]["offset"] for row in delivered if row["job"] == desk_job] == [0, 1]
            until(lambda: cli(hub, "research", "jobs"),
                  lambda jobs: len(jobs) == 2 and all(j["pending_evidence"] == 0 for j in jobs))
            for row in delivered:
                head = row["head"]
                discovered = cli(hub, "archive", "discover", head, "--budget", "100")
                docs = [cli(hub, "archive", "show", head, item) for item in discovered["ids"]]
                assert len(docs) == 1 and docs[0]["fields"]["naome_job"] == row["job"]
                assert "private text" not in json.dumps(docs) and "private output" not in json.dumps(docs)
                assert token not in json.dumps(docs) and desk_token not in json.dumps(docs)
            print("PASS: local capture without a job or orchestrator, two workspaces, offline delivery, six distinct evidence segments")
        finally:
            held1.close()
            held2.close()
            for node in reversed(started):
                cli(node, "stop")


if __name__ == "__main__":
    main()
