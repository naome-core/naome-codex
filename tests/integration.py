"""Exercise hooks and stdio tools against two nodes from the pinned NAOME Network release."""

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
    release = json.loads(Path(__file__).with_name("naome-release.json").read_text(encoding="utf-8"))
    expected_version = f"naome {release['network_version']}"
    installed_version = subprocess.check_output([NAOME, "--version"], text=True).strip()
    assert installed_version == expected_version, f"Expected {expected_version}, found {installed_version}"
    with tempfile.TemporaryDirectory(prefix="naome-codex-integration-") as scratch:
        temp = Path(scratch)
        hub, lab = temp / "hub", temp / "lab"
        hub_id, lab_id = initialize(hub), initialize(lab)
        expected_protocol = release["protocol_version"]
        installed_protocol = cli(hub, "archive", "capabilities")["version"]
        assert installed_protocol == expected_protocol, f"Expected protocol {expected_protocol}, found {installed_protocol}"
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

        def call_tool(name, token, **kwargs):
            return mcp([{"id": 5, "method": "tools/call", "params": {
                "name": name, "arguments": {"binding_token": token, **kwargs},
            }}])[0]["result"]

        def tool(name, token, **kwargs):
            row = call_tool(name, token, **kwargs)
            return row["isError"], row["content"][0]["text"]

        def token_for(workspace, session, turn="turn-one"):
            result = hook("UserPromptSubmit", workspace, session, prompt="Continue.", turn_id=turn)
            context = result["hookSpecificOutput"]["additionalContext"]
            return re.search(r"binding_token ([A-Za-z0-9_-]+)", context).group(1)

        assert hook("UserPromptSubmit", others, "unrelated", prompt="Continue.") == {}
        assert not (temp / "queue.sqlite").exists()
        for session, prompt in (
            ("performance-research", "Test the hypothesis that the new implementation uses less memory."),
            ("expression-research", "Investigate why two analyses of public gene expression data disagree."),
            ("clinical-research", "Compare the primary endpoints of two published clinical trials."),
            ("expression-edit", "Fix the misspelled expression unit in a figure caption."),
            ("clinical-edit", "Alphabetize the clinical trial citations without changing their content."),
        ):
            context = hook("UserPromptSubmit", work, session, prompt=prompt)["hookSpecificOutput"]["additionalContext"]
            assert "For substantive R&D in this enrolled workspace, apply the naome-lab-research skill." in context
            assert "use record_research for separate question and method entries" in context
            assert "Follow the user and workspace instructions for permission to record and send evidence." in context
            assert "Use delivery=local unless assignment delivery is authorized." in context
            assert "If recording is restricted or an approval is denied, continue the allowed work" in context
            assert "For routine edits, maintenance and status checks, rely on automatic hook capture" in context
            assert "Use list_recent_research for the latest research records" in context
            assert "use the displayed research card as the answer" in context
            assert re.search(r"binding_token [A-Za-z0-9_-]+", context)

        uri = "ui://naome/research-card-v2.html"
        legacy_uri = "ui://naome/research-card-v1.html"
        responses = mcp([{"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                         {"method": "notifications/initialized"}, {"id": 2, "method": "tools/list"},
                         {"id": 3, "method": "resources/list"},
                         {"id": 4, "method": "resources/read", "params": {"uri": uri}},
                         {"id": 5, "method": "resources/read", "params": {"uri": "ui://naome/unknown.html"}},
                         {"id": 6, "method": "resources/templates/list"},
                         {"id": 7, "method": "resources/read", "params": {"uri": legacy_uri}}])
        assert responses[0]["result"]["protocolVersion"] == "2025-11-25"
        assert "resources" in responses[0]["result"]["capabilities"]
        descriptors = {item["name"]: item for item in responses[1]["result"]["tools"]}
        assert descriptors.keys() == {
            "create_project", "research_status", "list_recent_research", "search_research",
            "record_research", "retry_research_delivery",
        }
        assert descriptors["search_research"]["annotations"]["readOnlyHint"] is True
        assert descriptors["list_recent_research"]["annotations"]["readOnlyHint"] is True
        assert descriptors["record_research"]["annotations"]["readOnlyHint"] is False
        assert descriptors["record_research"]["inputSchema"]["properties"]["delivery"]["default"] == "assigned"
        card_tools = {name: item["_meta"] for name, item in descriptors.items() if "_meta" in item}
        assert card_tools.keys() == {"record_research", "list_recent_research", "search_research"}
        assert all(meta == {"ui": {"resourceUri": uri}, "openai/ui": {"preferredModelDisplayMode": "inline"}}
                   for meta in card_tools.values())
        mime = "text/html;profile=mcp-app"
        assert responses[2]["result"]["resources"] == [{"uri": uri, "name": "Naome research block", "mimeType": mime}]
        resource, = responses[3]["result"]["contents"]
        assert resource["uri"] == uri and resource["mimeType"] == mime
        assert resource["_meta"]["openai/widgetShowCodexWidgetInline"] is True
        assert resource["_meta"]["openai/widgetMinFrameHeight"] == 0
        assert resource["_meta"]["openai/widgetHeightHint"] == 104
        assert "data:image/png;base64," in resource["text"] and "__NAOME_CARD_" not in resource["text"]
        assert "ui/initialize" in resource["text"]
        assert responses[4]["error"]["code"] == -32002
        assert responses[5]["result"] == {"resourceTemplates": []}
        assert responses[6]["result"]["contents"] == [{**resource, "uri": legacy_uri}]

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
        empty = call_tool("list_recent_research", token)
        assert json.loads(empty["content"][0]["text"])["records"] == []
        assert "structuredContent" not in empty
        assert tool("search_research", token, query="calibration")[0]
        hypothesis = call_tool("record_research", token, category="hypothesis",
                               title="Offline hypothesis", summary="The archive is available locally.", sources=[])
        status = json.loads(hypothesis["content"][0]["text"])
        assert not hypothesis["isError"] and status["job"] is None
        assert status["pending_local"] == status["pending_reports"] == 0
        origin = status["local_record"]
        offline = show_local(lab, origin)
        assert offline["subject"] == "Offline hypothesis" and "naome_job" not in offline["fields"]
        assert hypothesis["structuredContent"]["record_id"] == origin

        summary = "The recovered archive contained the record and its references. " * 4
        sources = ["Synthetic fixture: <sample label>", "Synthetic fixture: archive readback"]
        recorded = call_tool("record_research", token, category="result", title="  Offline readback  ",
                             summary=summary, sources=sources, linked_records=[origin, origin])
        assert not recorded["isError"]
        record = json.loads(recorded["content"][0]["text"])["local_record"]
        card = recorded["structuredContent"]
        assert card["record_id"] == record
        assert (card["title"], card["category"], card["lab"]) == ("Offline readback", "result", "default")
        assert len(card["summary"]) <= 160 and card["summary"].endswith("…")
        assert recorded["_meta"] == {"naome/research-block": {
            "record_id": record, "summary": summary.strip(), "sources": sources, "linked_records": [origin],
        }}
        assert token not in json.dumps(recorded) and sources[0] not in json.dumps(card)
        archived = show_local(lab, record)
        assert archived["subject"] == card["title"]
        assert archived["fields"]["summary"] == summary.strip() and archived["fields"]["sources"] == sources
        assert [(entry["role"], entry["target"]) for entry in archived["inputs"]] == [("references", origin)]

        # A newer receipt in a different project must stay separate even on a shared lab.
        other_token = token_for(desk, "other-project-receipts")
        other = call_tool("record_research", other_token, category="hypothesis",
                          title="Other project's receipt", summary="This lab is shared by two projects.", sources=[])
        assert not other["isError"] and other["structuredContent"]["record_id"] != record
        assert hook("Stop", work, "repository-session", turn_id="offline-capture") == {}
        reader = token_for(work, "reader-session")

        config.write_text(json.dumps({"naome": str(temp / "unavailable"), "projects": projects}))
        queued = call_tool("record_research", token, category="method",
                           title="Deferred locally", summary="Repeat the readback.", sources=[])
        status = json.loads(queued["content"][0]["text"])
        assert not queued["isError"] and status["pending_local"] == 1 and status["local_record"] is None
        assert "structuredContent" not in queued and "_meta" not in queued
        recent_result = call_tool("list_recent_research", reader, limit=10)
        assert not recent_result["isError"], recent_result
        recent = json.loads(recent_result["content"][0]["text"])
        assert recent_result["structuredContent"] == recent and "_meta" not in recent_result
        assert (recent["project"], recent["lab"], recent["source"]) == (
            "repo-research", "default", "codex_archive_receipts")
        assert [item["record_id"] for item in recent["records"]] == [record, origin]
        assert recent["records"][0] == {
            "record_id": record, "title": "Offline readback", "category": "result",
            "captured_at": archived["fields"]["captured_at"],
        }
        assert json.loads(tool("list_recent_research", reader)[1])["records"] == recent["records"][:1]
        assert tool("list_recent_research", reader, limit=True)[0]
        assert tool("list_recent_research", reader, lab="other")[0]
        config.write_text(json.dumps({"naome": str(NAOME), "projects": projects}))
        expired = token
        token = token_for(work, "repository-session", "offline-retry")
        rejected = call_tool("record_research", expired, category="method",
                             title="Expired", summary="A previous turn cannot write.", sources=[])
        assert rejected["isError"] and "structuredContent" not in rejected and "_meta" not in rejected
        assert tool("list_recent_research", expired)[0]
        recovered = json.loads(tool("research_status", token)[1])
        assert recovered["pending_local"] == recovered["pending_reports"] == 0
        assert show_local(lab, recovered["local_record"])["subject"] == "Deferred locally"
        assert json.loads(tool("list_recent_research", reader)[1])["records"][0]["record_id"] == (
            recovered["local_record"])
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
            operation = {"category": "result", "title": "Operation completed",
                         "summary": "The observed command returned zero.", "sources": ["tool:turn-one:call-one"]}
            failed, result = tool("record_research", token, **operation)
            assigned = json.loads(result)
            assert not failed and assigned["pending_reports"] == 0 and assigned["job"] == repository_job
            failed, result = tool("record_research", token, delivery="local", **operation)
            local_only = json.loads(result)
            assert not failed and local_only["pending_reports"] == 0 and local_only["job"] is None
            assert local_only["local_record"] and local_only["local_record"] != assigned["local_record"]
            assert "naome_job" not in show_local(lab, local_only["local_record"])["fields"]
            assert json.loads(tool("record_research", token, delivery="local", **operation)[1]) == local_only
            assert tool("record_research", token, delivery="unrecognized", **operation)[0]
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
                assert local_only["local_record"] not in discovered["ids"]
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
