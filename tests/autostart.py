"""Check that the Codex hook wakes a network without waiting to archive evidence."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from integration import ENTRY, NAOME, cli, until


def main():
    with tempfile.TemporaryDirectory(prefix="naome-autostart-test-") as scratch:
        temp = Path(scratch).resolve()
        config = temp / "config.json"
        config.write_text(json.dumps({"naome": str(NAOME), "provisioning": {
            "workspace_root": str(temp / "workspaces"), "node_root": str(temp / "nodes"),
        }}))
        env = dict(os.environ, NAOME_CODEX_CONFIG=str(config), NAOME_CODEX_STATE=str(temp / "journal.sqlite"))
        release = temp / "allow-start"
        attempts = temp / "attempts"
        attempts.mkdir()
        running_nodes = {}

        def entry(mode, request, timeout=5):
            raw = subprocess.check_output([sys.executable, ENTRY, mode], input=json.dumps(request) + "\n",
                                          text=True, timeout=timeout, env=env)
            return json.loads(raw)

        def tool(name, timeout=5, **arguments):
            result = entry("mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": name, "arguments": arguments,
            }}, timeout)["result"]
            assert not result["isError"], result
            return json.loads(result["content"][0]["text"])

        def token(workspace, turn):
            response = entry("hook", {"hook_event_name": "UserPromptSubmit", "cwd": workspace,
                                      "session_id": "research-chat", "turn_id": turn, "prompt": "Record a finding."})
            context = response["hookSpecificOutput"]["additionalContext"]
            return re.search(r"binding_token ([A-Za-z0-9_-]+)", context)[1]

        try:
            project = tool("create_project", timeout=35, project="startup-study", mode="network")
            running_nodes = {role: Path(project[role]["directory"]) for role in ("lab", "orchestrator")}
            for node in running_nodes.values():
                assert not cli(node, "stop")["running"]
            lab = running_nodes["lab"]

            launcher = temp / "hold-node-start"
            launcher.write_text(
                f"#!{sys.executable}\n"
                "import os, sys, time\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "if args[3:] == ['start']:\n"
                f"    marker = Path({str(attempts)!r}) / Path(args[1]).name\n"
                "    with marker.open('a') as file:\n"
                "        file.write('start\\n')\n"
                "    deadline = time.monotonic() + 18\n"
                f"    while not Path({str(release)!r}).exists():\n"
                "        if time.monotonic() >= deadline:\n"
                "            sys.exit(72)\n"
                "        time.sleep(0.05)\n"
                f"os.execv({str(NAOME)!r}, [{str(NAOME)!r}, *args])\n"
            )
            launcher.chmod(0o700)
            settings = json.loads(config.read_text())
            settings["naome"] = str(launcher)
            config.write_text(json.dumps(settings))
            token(project["workspace"], "before-opt-in")
            assert not (temp / "autostart.log").exists()

            settings["projects"]["startup-study"]["auto_start"] = True
            config.write_text(json.dumps(settings))
            active = token(project["workspace"], "startup")
            until(lambda: {item.name for item in attempts.iterdir()}, lambda seen: running_nodes.keys() <= seen)
            assert all(not cli(node, "status")["running"] for node in running_nodes.values())
            assert not release.exists()
            saved = tool("record_research", binding_token=active, category="hypothesis", title="Before either node starts",
                         summary="The local archive remains available while both listeners are stopped.", sources=[])
            assert saved["pending_local"] == 0 and saved["job"] is None
            record = saved["local_record"]
            head = cli(lab, "archive", "snapshot", "Autostart test", "--root", record)
            assert cli(lab, "archive", "show", head, record)["subject"] == "Before either node starts"
            token(project["workspace"], "during-startup")
            assert all(not cli(node, "status")["running"] for node in running_nodes.values())
            assert all((attempts / role).read_text() == "start\n" for role in running_nodes)

            release.touch()
            for role, other in (("lab", "orchestrator"), ("orchestrator", "lab")):
                node, peer = running_nodes[role], project[other]["peer_id"]
                status = until(lambda: cli(node, "status"), lambda state: state["running"] and peer in state["connected_peers"])
                assert status["peer_id"] == project[role]["peer_id"]
            token(project["workspace"], "already-running")
            assert all((attempts / role).read_text() == "start\n" for role in running_nodes)
            print("PASS: both start commands held; committed and read back local evidence while both nodes were stopped; one background startup per node")
        finally:
            release.touch()
            for role, node in running_nodes.items():
                if (attempts / role).exists():
                    until(lambda: cli(node, "status"), lambda status: status["running"])
                cli(node, "stop")


if __name__ == "__main__":
    main()
