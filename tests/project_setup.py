"""Provision both project modes through the installed plugin's MCP entry point."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from integration import ENTRY, NAOME, cli, until


def main():
    with tempfile.TemporaryDirectory(prefix="naome-project-setup-") as scratch:
        temp = Path(scratch).resolve()
        workspaces, nodes = temp / "workspaces", temp / "nodes"
        config = temp / "plugin-config.json"
        existing = {"workspace": str(temp / "existing-workspace"), "lab": str(temp / "existing-lab")}
        settings = {"naome": str(NAOME), "projects": {"existing": existing},
                    "provisioning": {"workspace_root": str(workspaces), "node_root": str(nodes)}}
        config.write_text(json.dumps(settings))
        env = dict(os.environ, NAOME_CODEX_CONFIG=str(config), NAOME_CODEX_STATE=str(temp / "journal.sqlite"))
        running = []

        def entry(mode, request):
            data = subprocess.check_output([sys.executable, ENTRY, mode], input=json.dumps(request) + "\n",
                                           text=True, env=env, timeout=80)
            return json.loads(data)

        def tool(name, error=False, **arguments):
            result = entry("mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": name, "arguments": arguments,
            }})["result"]
            assert result["isError"] == error, result
            text = result["content"][0]["text"]
            return text if error else json.loads(text)

        def record(project, title):
            result = entry("hook", {"hook_event_name": "UserPromptSubmit", "cwd": project["workspace"],
                                   "session_id": project["project"], "turn_id": "one", "prompt": "Record evidence."})
            context = result["hookSpecificOutput"]["additionalContext"]
            token = re.search(r"binding_token ([A-Za-z0-9_-]+)", context)[1]
            return tool("record_research", binding_token=token, category="hypothesis", title=title,
                        summary="The project can retain this evidence in its own archive.", sources=[])

        try:
            assert not workspaces.exists() and not nodes.exists()
            assert "project name" in tool("create_project", project="../outside", error=True)
            assert not workspaces.exists() and not nodes.exists()
            local = tool("create_project", project="offline-study")
            assert local["created"] and local["mode"] == "local" and "orchestrator" not in local
            assert not local["lab"]["running"]
            lab = Path(local["lab"]["directory"])
            assert lab == nodes / "offline-study" / "lab"
            assert Path(local["workspace"]) == workspaces / "offline-study"
            assert Path(local["workspace"]).is_dir() and (lab / "archive.sqlite").is_file()
            assert json.loads((lab / "node.json").read_text())["peers"] == []
            local_result = record(local, "Offline evidence")
            assert local_result["job"] is None and local_result["pending_local"] == 0
            identifier = local_result["local_record"]
            head = cli(lab, "archive", "snapshot", "Local readback", "--root", identifier)
            assert cli(lab, "archive", "show", head, identifier)["subject"] == "Offline evidence"
            assert not cli(lab, "status")["running"]
            assert tool("create_project", project="offline-study") == {**local, "created": False}

            connected = tool("create_project", project="network-study", mode="network")
            assert connected["created"] and connected["connected"]
            lab = Path(connected["lab"]["directory"])
            orchestrator = Path(connected["orchestrator"]["directory"])
            running.extend([lab, orchestrator])
            assert lab.parent == orchestrator.parent == nodes / "network-study"
            assert connected["lab"]["peer_id"] != connected["orchestrator"]["peer_id"]
            assert connected["orchestrator"]["peer_id"] in cli(lab, "status")["connected_peers"]
            assert connected["lab"]["peer_id"] in cli(orchestrator, "status")["connected_peers"]
            assert json.loads((lab / "node.json").read_text())["peers"][0]["address"].startswith("/ip4/127.0.0.1/")
            job = cli(orchestrator, "research", "assign", connected["lab"]["peer_id"], "network-study", "Verify setup")
            until(lambda: cli(lab, "research", "jobs"),
                  lambda jobs: any(item["id"] == job and item["input_ready"] for item in jobs))
            result = record(connected, "Replicated evidence")
            assert result["job"] == job and result["pending_reports"] == 0, result
            report, identifier = result["last_head"], result["local_record"]
            until(lambda: cli(orchestrator, "archive", "heads"), lambda heads: report in heads)
            assert cli(orchestrator, "archive", "show", report, identifier)["subject"] == "Replicated evidence"
            for index, node in enumerate(running):
                cli(node, "archive", "resolve", report, identifier, temp / f"evidence-{index}")
            assert (temp / "evidence-0").read_bytes() == (temp / "evidence-1").read_bytes()

            assert not cli(lab, "stop")["running"]
            assert not cli(orchestrator, "stop")["running"]
            current = json.loads(config.read_text())
            wrapper = temp / "fail-lab-start"
            wrapper.write_text(
                f"#!{sys.executable}\n"
                "import os, sys\n"
                "args = sys.argv[1:]\n"
                f"if args[1] == {str(lab)!r} and args[3:] == ['start']:\n"
                "    sys.exit(72)\n"
                f"os.execv({str(NAOME)!r}, [{str(NAOME)!r}, *args])\n"
            )
            wrapper.chmod(0o700)
            config.write_text(json.dumps({**current, "naome": str(wrapper)}))
            failed = tool("create_project", project="network-study", mode="network", error=True)
            assert "network is not ready" in failed and "Retry the same setup" in failed
            assert cli(orchestrator, "status")["running"] and not cli(lab, "status")["running"]
            assert json.loads(config.read_text())["projects"] == current["projects"]
            config.write_text(json.dumps(current))
            assert tool("create_project", project="network-study", mode="network") == {**connected, "created": False}
            assert "different setup" in tool("create_project", project="network-study", error=True)
            assert "different setup" in tool("create_project", project="existing", error=True)
            occupied = workspaces / "occupied"
            occupied.mkdir()
            original = occupied / "keep.txt"
            original.write_text("existing work")
            assert "existing workspace" in tool("create_project", project="occupied", error=True)
            assert original.read_text() == "existing work" and not (nodes / "occupied").exists()

            assert current["projects"]["existing"] == existing
            config.write_text(json.dumps({**current, "naome": str(temp / "missing-executable")}))
            assert "could not initialize" in tool("create_project", project="failed-init", error=True)
            assert not (workspaces / "failed-init").exists() and not (nodes / "failed-init").exists()
            assert "failed-init" not in json.loads(config.read_text())["projects"]
            config.write_text(json.dumps(current))
            assert tool("create_project", project="failed-init")["created"]
            print("PASS: blank project to offline evidence; blank project to private network and exact replicated bytes; safe retry and rollback")
        finally:
            for node in running:
                cli(node, "stop")


if __name__ == "__main__":
    main()
