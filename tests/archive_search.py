"""Exercise the released native search worker through the normal plugin MCP server."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from integration import ENTRY, NAO, cli, initialize


def main():
    with tempfile.TemporaryDirectory(prefix="naome-codex-search-") as scratch:
        root = Path(scratch)
        lab_a, lab_b, workspace = (root / name for name in ("lab-a", "lab-b", "work"))
        workspace.mkdir()
        for lab in (lab_a, lab_b):
            initialize(lab)

        def put(lab, scope, title, summary, links=()):
            document = root / "record.json"
            document.write_text(json.dumps({"kind": "claim", "scope": scope, "subject": title,
                                            "fields": {"summary": summary}, "inputs": [
                                                {"role": "references", "target": value} for value in links
                                            ]}))
            return cli(lab, "put", document)

        control = put(lab_a, "calibration", "Daily calibration log", "The equipment was calibrated before collection.")
        foreign = put(lab_a, "another-project", "Foreign private log", "External evidence from another project.")
        approved = put(lab_a, "calibration", "Detector drift over time",
                       "Detector readings slowly drifted. The instrument was recalibrated every morning.",
                       [control, foreign])
        head_a = cli(lab_a, "snapshot", "Approved project A", "--root", approved)
        newer = put(lab_a, "calibration", "Unapproved newer record", "Later evidence received after approval.")
        newer_head = cli(lab_a, "snapshot", "Unapproved newer inventory")
        cli(lab_a, "search-head", newer_head)
        only_b = put(lab_b, "calibration", "Oscillator thermal run", "Temperatures stayed inside the expected range.")
        head_b = cli(lab_b, "snapshot", "Approved project B", "--root", only_b)

        config = root / "config.json"
        launches = root / "launches"
        revoke = root / "revoke"
        wrapper = root / "native.py"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\nfrom pathlib import Path\n"
            f"binary = {str(NAO)!r}\n"
            "if sys.argv[-1:] == ['search-session']:\n"
            "    assert Path.cwd() == Path(sys.argv[sys.argv.index('--db') + 1]).parent\n"
            f"    with Path({str(launches)!r}).open('a') as log: log.write(str(os.getpid()) + '\\n')\n"
            f"    if Path({str(revoke)!r}).exists():\n"
            f"        config = Path({str(config)!r})\n"
            "        data = json.loads(config.read_text())\n"
            "        data['projects']['calibration']['labs']['B'].pop('search_head')\n"
            "        config.write_text(json.dumps(data))\n"
            "os.execv(binary, [binary, *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o700)
        old_cli = root / "old-native.py"
        old_cli.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "if 'search-session' in sys.argv:\n"
            "    sys.exit(\"error: unrecognized subcommand 'search-session'\")\n"
            f"os.execv({str(NAO)!r}, [{str(NAO)!r}, *sys.argv[1:]])\n"
        )
        old_cli.chmod(0o700)
        settings = {"nao": str(wrapper), "projects": {"calibration": {"workspace": str(workspace),
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

        launch_dir = root / "plugin-cache"
        launch_dir.mkdir()
        with subprocess.Popen([sys.executable, ENTRY, "mcp"], text=True, env=env, cwd=launch_dir,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) as server:
            server.stdin.write('{"jsonrpc":"2.0","id":0,"method":"initialize"}\n')
            server.stdin.flush()
            assert "tools" in json.loads(server.stdout.readline())["result"]["capabilities"]
            # Reinstalling the plugin can remove a live server's original working directory.
            launch_dir.rmdir()
            def call(token, query, **extra):
                server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "search_research", "arguments": {"binding_token": token, "query": query, **extra},
                }}) + "\n")
                server.stdin.flush()
                return json.loads(server.stdout.readline())["result"]

            def result(token, query, **extra):
                reply = call(token, query, **extra)
                assert not reply["isError"], reply
                report = json.loads(reply["content"][0]["text"])
                if report["hits"] or report.get("coverage") in {"index_incomplete", "limited"}:
                    assert reply["structuredContent"] == report and "_meta" not in reply
                else:
                    assert "structuredContent" not in reply
                return report

            try:
                token = bind("a", "Find previous calibration work.")
                settings["nao"] = str(old_cli)
                configure()
                outdated = call(token, approved)
                message = outdated["content"][0]["text"]
                assert outdated["isError"] and all(part in message for part in (
                    str(old_cli), "standalone NAOME Protocol CLI", "v0.1.6",
                )), message
                settings["nao"] = str(wrapper)
                configure()
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

                assert call(token, only_b, lab="B")["isError"]
                lab_settings = settings["projects"]["calibration"]["labs"]["B"]
                lab_settings["search_head"] = head_b
                configure()
                revoke.touch()
                rejected = call(token, only_b, lab="B")
                assert rejected["isError"] and only_b not in json.dumps(rejected) and "structuredContent" not in rejected
                assert "changed while the query was running" in rejected["content"][0]["text"]
                assert call(token, only_b, lab="B")["isError"]
                revoke.unlink()
                configure()
                response = result(token, only_b, lab="B")
                assert response["lab"] == "B" and response["head"] == head_b
                assert response["hits"][0]["record_id"] == only_b
                assert result(token, approved, lab="B")["hits"] == []
                assert result(token, approved)["lab"] == "A"
                next_token = bind("b", "/lab B\nLook up the thermal run.")
                assert call(token, approved)["isError"]
                assert result(next_token, approved)["lab"] == "A"

                for index in range(33):
                    put(lab_b, "calibration", f"Cold index record {index}", "Additional calibration evidence.")
                lab_settings["search_head"] = cli(lab_b, "snapshot", "Fresh search inventory")
                configure()
                partial = result(next_token, "nonexistentkeyword", lab="B", mode="lexical")
                assert partial["hits"] == [] and partial["coverage"] == "index_incomplete", partial
                complete = result(next_token, "oscillator", lab="B", mode="lexical")
                assert complete["coverage"] == "complete" and complete["hits"][0]["record_id"] == only_b, complete
            finally:
                server.stdin.close()
                assert server.wait(timeout=5) == 0
    print("PASS: approved offline snapshots, project and link filtering, native worker reuse, and live revocation.")


if __name__ == "__main__":
    main()
