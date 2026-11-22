"""Exercise the released native search worker through the normal plugin MCP server."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from integration import ENTRY, NAOME, cli, initialize


def main():
    with tempfile.TemporaryDirectory(prefix="naome-codex-search-") as scratch:
        root = Path(scratch)
        lab_a, lab_b, workspace = (root / name for name in ("lab-a", "lab-b", "work"))
        workspace.mkdir()
        for lab in (lab_a, lab_b):
            initialize(lab)
            (lab / "node.json").write_text(json.dumps({"role": "lab", "listen": "/ip4/127.0.0.1/tcp/0", "peers": []}))

        def put(lab, scope, title, summary, links=()):
            document = root / "record.json"
            document.write_text(json.dumps({"kind": "claim", "scope": scope, "subject": title,
                                            "fields": {"summary": summary}, "inputs": [
                                                {"role": "references", "target": value} for value in links
                                            ]}))
            return cli(lab, "archive", "put", document)

        control = put(lab_a, "calibration", "Daily calibration log", "The equipment was calibrated before collection.")
        foreign = put(lab_a, "another-project", "Foreign private log", "External evidence from another project.")
        approved = put(lab_a, "calibration", "Detector drift over time",
                       "Detector readings slowly drifted. The instrument was recalibrated every morning.",
                       [control, foreign])
        head_a = cli(lab_a, "archive", "snapshot", "Approved project A", "--root", approved)
        newer = put(lab_a, "calibration", "Unapproved newer record", "Later evidence received after approval.")
        newer_head = cli(lab_a, "archive", "snapshot", "Unapproved newer inventory")
        cli(lab_a, "archive", "search-head", newer_head)
        only_b = put(lab_b, "calibration", "Oscillator thermal run", "Temperatures stayed inside the expected range.")
        head_b = cli(lab_b, "archive", "snapshot", "Approved project B", "--root", only_b)
        assert all(not cli(lab, "status")["running"] for lab in (lab_a, lab_b))

        config = root / "config.json"
        launches = root / "launches"
        revoke = root / "revoke"
        wrapper = root / "native.py"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\nfrom pathlib import Path\n"
            f"binary = {str(NAOME)!r}\n"
            "if sys.argv[-2:] == ['archive', 'search-session']:\n"
            f"    with Path({str(launches)!r}).open('a') as log: log.write(str(os.getpid()) + '\\n')\n"
            f"    if Path({str(revoke)!r}).exists():\n"
            f"        config = Path({str(config)!r})\n"
            "        data = json.loads(config.read_text())\n"
            "        data['projects']['calibration']['labs']['B'].pop('search_head')\n"
            "        config.write_text(json.dumps(data))\n"
            "os.execv(binary, [binary, *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o700)
        settings = {"naome": str(wrapper), "projects": {"calibration": {"workspace": str(workspace),
                    "default_lab": "A", "labs": {"A": {"directory": str(lab_a), "search_head": head_a},
                                                   "B": {"directory": str(lab_b)}}}}}

        def configure():
            config.write_text(json.dumps(settings))

        configure()
        env = dict(os.environ, NAOME_CODEX_CONFIG=str(config), NAOME_CODEX_STATE=str(root / "journal.sqlite"))

        def bind(turn, prompt):
            response = subprocess.check_output([sys.executable, ENTRY, "hook"], text=True, env=env,
                input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "search-fixture",
                                  "turn_id": turn, "prompt": prompt, "cwd": str(workspace)}))
            token = re.search(r"binding_token ([A-Za-z0-9_-]+)", response)
            assert token, response
            return token.group(1)

        with subprocess.Popen([sys.executable, ENTRY, "mcp"], text=True, env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) as server:
            def call(token, query, **extra):
                server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "search_research", "arguments": {"binding_token": token, "query": query, **extra},
                }}) + "\n")
                server.stdin.flush()
                return json.loads(server.stdout.readline())["result"]

            def result(token, query, **extra):
                reply = call(token, query, **extra)
                assert not reply["isError"], reply
                return json.loads(reply["content"][0]["text"])

            try:
                token = bind("a", "Find previous calibration work.")
                assert call(token, "detector", head=newer_head)["isError"]
                assert call(token, "detector", mode="semantic")["isError"]
                assert not launches.exists()
                exact = result(token, approved, mode="semantic")
                assert exact["head"] == head_a and exact["mode"] == "lexical"
                assert exact["hits"][0]["record_id"] == approved and exact["hits"][0]["exact_id"]
                assert exact["hits"][0]["links"] == [{"role": "references", "target": control}]
                assert foreign not in json.dumps(exact)
                assert result(token, foreign)["hits"] == []
                assert result(token, newer)["hits"] == []
                lexical = result(token, "detector")
                assert lexical["head"] == head_a and lexical["mode"] == "lexical" and lexical["index_complete"]
                assert lexical["hits"][0]["record_id"] == approved
                assert len(launches.read_text().splitlines()) == 1

                model = os.environ.get("NAOME_TEST_SEARCH_MODEL")
                if model:
                    settings["search"] = {"model": model}
                    if os.environ.get("NAOME_TEST_SEARCH_PYTHON"):
                        settings["search"]["python"] = os.environ["NAOME_TEST_SEARCH_PYTHON"]
                    configure()
                    semantic = result(token, "Welche Wartung glich die langsame Veränderung der Messwerte aus?",
                                      mode="semantic")
                    assert semantic["semantic_index_complete"] and semantic["mode"] == "semantic"
                    assert semantic["hits"][0]["record_id"] == approved
                    assert semantic["hits"][0]["links"] == [{"role": "references", "target": control}]
                    native_worker = launches.read_text()
                    hybrid = result(token, "detector drift")
                    assert hybrid["mode"] == "hybrid" and hybrid["hits"][0]["record_id"] == approved
                    assert launches.read_text() == native_worker
                    print("PASS: an offline German paraphrase and hybrid query found the approved result.")

                token_b = bind("b", "/lab B\nLook up the thermal run.")
                assert call(token, approved)["isError"]
                assert call(token_b, only_b)["isError"]
                lab_settings = settings["projects"]["calibration"]["labs"]["B"]
                lab_settings["search_head"] = head_b
                configure()
                revoke.touch()
                rejected = call(token_b, only_b)
                assert rejected["isError"] and only_b not in json.dumps(rejected)
                assert "changed while the query was running" in rejected["content"][0]["text"]
                assert call(token_b, only_b)["isError"]
                revoke.unlink()
                configure()
                response = result(token_b, only_b)
                assert response["lab"] == "B" and response["head"] == head_b
                assert response["hits"][0]["record_id"] == only_b
                assert result(token_b, approved)["hits"] == []
                assert all(not cli(lab, "status")["running"] for lab in (lab_a, lab_b))
            finally:
                server.stdin.close()
                assert server.wait(timeout=5) == 0
    print("PASS: approved offline snapshots, project and link filtering, native worker reuse, and live revocation.")


if __name__ == "__main__":
    main()
