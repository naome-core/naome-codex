"""Exercise hooks and stdio tools against the standalone Protocol release."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "plugin" / "entry.py"
NAO = Path(os.environ.get("NAO_TEST_BINARY", str(Path.home() / ".cargo/bin/nao")))


def cli(lab, *args):
    raw = subprocess.check_output([NAO, "--db", lab / "archive.sqlite", *args], text=True)
    return json.loads(raw) if raw.strip() else None


def initialize(lab):
    lab.mkdir(parents=True)
    cli(lab, "init")


def invoke(env, mode, payload):
    raw = subprocess.check_output([sys.executable, ENTRY, mode], input=json.dumps(payload) + "\n",
                                  text=True, env=env, timeout=35)
    return json.loads(raw)


def call(env, name, **arguments):
    return invoke(env, "mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": name, "arguments": arguments,
    }})["result"]


def value(env, name, error=False, **arguments):
    result = call(env, name, **arguments)
    assert result["isError"] == error, result
    text = result["content"][0]["text"]
    return text if error else json.loads(text)


def prompt(env, workspace, session, turn):
    result = invoke(env, "hook", {"hook_event_name": "UserPromptSubmit", "cwd": str(workspace),
                                 "session_id": session, "turn_id": turn})
    context = result["hookSpecificOutput"]["additionalContext"]
    return re.search(r"binding_token ([A-Za-z0-9_-]+)", context)[1], context


def main():
    release = json.loads(Path(__file__).with_name("naome-release.json").read_text(encoding="utf-8"))
    expected_version = f"nao {release['protocol_version']}"
    installed_version = subprocess.check_output([NAO, "--version"], text=True).strip()
    assert installed_version == expected_version, f"Expected {expected_version}, found {installed_version}"
    with tempfile.TemporaryDirectory(prefix="naome-codex-integration-") as scratch:
        temp = Path(scratch)
        lab = temp / "lab"
        initialize(lab)
        installed_protocol = cli(lab, "capabilities")["version"]
        assert installed_protocol == release["protocol_version"], installed_protocol
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
        config.write_text(json.dumps({"nao": str(NAO), "projects": projects}))
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
            assert "use capture_research for separate question and method entries" in context
            assert "Follow the user and workspace instructions for permission to record evidence." in context
            assert "Research is stored in the local lab archive." in context
            assert "If recording is restricted or an approval is denied, continue the allowed work" in context
            assert "For routine edits, maintenance and status checks, rely on automatic hook capture" in context
            assert "Use lookup_research for reads during analysis" in context
            assert "use the displayed research card as the answer" in context
            assert re.search(r"binding_token [A-Za-z0-9_-]+", context)

        uri = "ui://naome/research-card-v3.html"
        legacy_uri = "ui://naome/research-card-v1.html"
        previous_uri = "ui://naome/research-card-v2.html"
        responses = mcp([{"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                         {"method": "notifications/initialized"}, {"id": 2, "method": "tools/list"},
                         {"id": 3, "method": "resources/list"},
                         {"id": 4, "method": "resources/read", "params": {"uri": uri}},
                         {"id": 5, "method": "resources/read", "params": {"uri": "ui://naome/unknown.html"}},
                         {"id": 6, "method": "resources/templates/list"},
                         {"id": 7, "method": "resources/read", "params": {"uri": legacy_uri}},
                         {"id": 8, "method": "resources/read", "params": {"uri": previous_uri}}])
        assert responses[0]["result"]["protocolVersion"] == "2025-11-25"
        assert "resources" in responses[0]["result"]["capabilities"]
        descriptors = {item["name"]: item for item in responses[1]["result"]["tools"]}
        assert descriptors.keys() == {
            "create_project", "research_status", "select_lab", "list_recent_research", "search_research",
            "lookup_research", "record_research", "capture_research", "retry_research_delivery",
        }
        assert descriptors["search_research"]["annotations"]["readOnlyHint"] is True
        assert descriptors["list_recent_research"]["annotations"]["readOnlyHint"] is True
        assert descriptors["record_research"]["annotations"]["readOnlyHint"] is False
        assert descriptors["capture_research"]["annotations"] == descriptors["record_research"]["annotations"]
        assert descriptors["lookup_research"]["annotations"] == descriptors["search_research"]["annotations"]
        assert descriptors["record_research"]["inputSchema"]["properties"]["delivery"]["enum"] == ["local"]
        card_tools = {name: item["_meta"] for name, item in descriptors.items() if "_meta" in item}
        assert card_tools.keys() == {"record_research", "list_recent_research", "search_research"}
        assert all(meta == {"ui": {"resourceUri": uri}, "openai/ui": {"preferredModelDisplayMode": "inline"}}
                   for meta in card_tools.values())
        mime = "text/html;profile=mcp-app"
        assert responses[2]["result"]["resources"] == [{"uri": uri, "name": "Naome research block", "mimeType": mime}]
        resource, = responses[3]["result"]["contents"]
        assert resource["uri"] == uri and resource["mimeType"] == mime
        assert resource["_meta"]["openai/ui"]["availableDisplayModes"] == ["inline"]
        assert resource["_meta"]["openai/widgetShowCodexWidgetInline"] is True
        assert resource["_meta"]["openai/widgetMinFrameHeight"] == 0
        assert resource["_meta"]["openai/widgetHeightHint"] == 104
        assert "data:image/png;base64," in resource["text"] and "__NAOME_CARD_" not in resource["text"]
        assert "ui/initialize" in resource["text"]
        assert responses[4]["error"]["code"] == -32002
        assert responses[5]["result"] == {"resourceTemplates": []}
        assert responses[6]["result"]["contents"] == [{**resource, "uri": legacy_uri}]
        assert responses[7]["result"]["contents"] == [{**resource, "uri": previous_uri}]

        def show_local(node, identifier):
            snapshot = cli(node, "snapshot", "Integration local readback",
                           "--root", identifier)
            return cli(node, "show", snapshot, identifier)

        token = token_for(work, "repository-session", "offline-capture")
        assert json.loads(tool("research_status", token)[1])["pending_local"] == 0
        empty = call_tool("list_recent_research", token)
        assert json.loads(empty["content"][0]["text"])["records"] == []
        assert "structuredContent" not in empty
        assert tool("search_research", token, query="calibration")[0]
        hypothesis = call_tool("record_research", token, category="hypothesis",
                               title="Offline hypothesis", summary="The archive is available locally.", sources=[])
        status = json.loads(hypothesis["content"][0]["text"])
        assert not hypothesis["isError"]
        assert status["pending_local"] == 0
        origin = status["local_record"]
        offline = show_local(lab, origin)
        assert offline["subject"] == "Offline hypothesis" and "naome_job" not in offline["fields"]
        assert hypothesis["structuredContent"]["record_id"] == origin
        failed, message = tool("record_research", token, category="hypothesis", title="Assigned",
                               summary="This call requests a removed destination.", sources=[], delivery="assigned")
        assert failed and "stores research records locally" in message

        summary = "The recovered archive contained the record and its references. " * 4
        sources = ["Synthetic fixture: <sample label>", "Synthetic fixture: archive readback"]
        recorded = call_tool("record_research", token, category="result", title="  Offline readback  ",
                             summary=summary, sources=sources, linked_records=[origin, origin])
        assert not recorded["isError"]
        record = json.loads(recorded["content"][0]["text"])["local_record"]
        card = recorded["structuredContent"]
        assert card["record_id"] == record
        assert card["project"] == "repo-research"
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
        short_receipt = call_tool("capture_research", token, category="result", title=card["title"],
                                  summary=summary, sources=sources, linked_records=[origin])
        assert set(short_receipt) == {"isError", "content"} and not short_receipt["isError"]
        assert json.loads(short_receipt["content"][0]["text"])["local_record"] == record

        # A newer receipt in a different project must stay separate even on a shared lab.
        other_token = token_for(desk, "other-project-receipts")
        other = call_tool("record_research", other_token, category="hypothesis",
                          title="Other project's receipt", summary="This lab is shared by two projects.", sources=[])
        assert not other["isError"] and other["structuredContent"]["record_id"] != record
        assert hook("Stop", work, "repository-session", turn_id="offline-capture") == {}
        reader = token_for(work, "reader-session")

        config.write_text(json.dumps({"nao": str(temp / "unavailable"), "projects": projects}))
        queued = call_tool("record_research", token, category="method",
                           title="Deferred locally", summary="Repeat the readback.", sources=[])
        status = json.loads(queued["content"][0]["text"])
        assert not queued["isError"] and status["pending_local"] == 1 and status["local_record"] is None
        assert "structuredContent" not in queued and "_meta" not in queued
        pending_receipt = call_tool("capture_research", token, category="method",
                                    title="Deferred locally", summary="Repeat the readback.", sources=[])
        assert set(pending_receipt) == {"isError", "content"} and not pending_receipt["isError"]
        pending = json.loads(pending_receipt["content"][0]["text"])
        assert pending["local_record"] is None and pending["pending_local"] == 1 and pending["local_retry"]
        rejected_capture = call_tool("capture_research", token, category="result",
                                     title="Missing evidence", summary="This result needs provenance.", sources=[])
        assert rejected_capture["isError"] and set(rejected_capture) == {"isError", "content"}
        assert "need evidence references" in rejected_capture["content"][0]["text"]
        recent_result = call_tool("list_recent_research", reader, limit=10)
        assert not recent_result["isError"], recent_result
        recent = json.loads(recent_result["content"][0]["text"])
        assert recent_result["structuredContent"] == recent
        assert recent_result["_meta"] == {"naome/research-details": {
            record: {"summary": summary.strip(), "sources": sources, "linked_records": [origin]},
            origin: {"summary": "The archive is available locally.", "sources": [], "linked_records": []},
        }}
        assert (recent["project"], recent["lab"], recent["source"]) == (
            "repo-research", "default", "codex_archive_receipts")
        assert [item["record_id"] for item in recent["records"]] == [record, origin]
        assert recent["records"][0] == {
            "record_id": record, "title": "Offline readback", "category": "result",
            "captured_at": archived["fields"]["captured_at"],
            "preview": card["summary"],
        }
        assert summary.strip() not in json.dumps(recent)
        assert sources[0] not in json.dumps(recent)
        background = call_tool("lookup_research", reader)
        assert set(background) == {"isError", "content"} and not background["isError"]
        lookup, = json.loads(background["content"][0]["text"])["records"]
        assert (lookup["record_id"], lookup["summary"], lookup["sources"], lookup["linked_records"]) == (
            record, summary.strip(), sources, [origin],
        )
        assert json.loads(tool("list_recent_research", reader)[1])["records"] == recent["records"][:1]
        assert tool("list_recent_research", reader, limit=True)[0]
        assert tool("list_recent_research", reader, lab="other")[0]
        config.write_text(json.dumps({"nao": str(NAO), "projects": projects}))
        expired = token
        token = token_for(work, "repository-session", "offline-retry")
        rejected = call_tool("record_research", expired, category="method",
                             title="Expired", summary="A previous turn cannot write.", sources=[])
        assert rejected["isError"] and "structuredContent" not in rejected and "_meta" not in rejected
        assert tool("list_recent_research", expired)[0]
        recovered = json.loads(tool("research_status", token)[1])
        assert recovered["pending_local"] == 0
        assert show_local(lab, recovered["local_record"])["subject"] == "Deferred locally"
        assert json.loads(tool("list_recent_research", reader)[1])["records"][0]["record_id"] == (
            recovered["local_record"])

        # Rename the enrolled workspace and preserve its journal, then read a second
        # project in the same chat. Both projects use a lab named default.
        other_record = other["structuredContent"]["record_id"]
        other_head = cli(lab, "snapshot", "Approved project switch fixture", "--root", other_record)
        own_head = cli(lab, "snapshot", "Approved renamed project fixture", "--root", record)
        renamed = {
            "repo": {**projects["repo-research"], "archive_scope": "repo-research", "search_head": own_head},
            "other-research": {**projects["desk-research"], "archive_scope": "desk-research", "search_head": other_head},
        }
        config.write_text(json.dumps({"nao": str(NAO), "projects": renamed}))
        for turn, prompt in (
            ("spoken-project", "und im other research?"),
            ("explicit-project", "/project other-research\n/lab default\nShow the latest record."),
        ):
            context = hook("UserPromptSubmit", work, "reader-session", turn_id=turn, prompt=prompt)[
                "hookSpecificOutput"]["additionalContext"]
            assert "The workspace project is repo." in context
            assert '"other-research":["default"]' in context
            previous = reader
            reader = re.search(r"binding_token ([A-Za-z0-9_-]+)", context)[1]
            assert tool("list_recent_research", previous)[0]
            own = call_tool("list_recent_research", reader)["structuredContent"]
            assert own["project"] == "repo" and own["records"][0]["record_id"] == recovered["local_record"]

        other_target = {"project": "other-research", "lab": "default"}
        selected = call_tool("list_recent_research", reader, **other_target)["structuredContent"]
        assert (selected["project"], selected["lab"]) == ("other-research", "default")
        assert [item["record_id"] for item in selected["records"]] == [other_record]
        search = call_tool("search_research", reader, query=other_record, **other_target)["structuredContent"]
        assert search["project"] == "other-research" and search["hits"][0]["record_id"] == other_record
        background = call_tool("lookup_research", reader, query=other_record, **other_target)
        assert set(background) == {"isError", "content"} and not background["isError"]
        assert json.loads(background["content"][0]["text"]) == search
        failed, body = tool("search_research", reader, query=record, **other_target)
        assert not failed and json.loads(body)["hits"] == []
        assert tool("list_recent_research", reader, project="repo-research")[0]
        assert tool("list_recent_research", reader, project="other research")[0]
        assert tool("list_recent_research", reader, project="other-research", lab="unknown")[0]
        assert tool("record_research", reader, category="method", title="Unexpected write",
                    summary="A read target cannot authorize a write.", sources=[], **other_target)[0]
        assert json.loads(tool("research_status", reader)[1])["project"] == "repo"
        search = call_tool("search_research", reader, query=record)["structuredContent"]
        assert search["project"] == "repo" and search["hits"][0]["record_id"] == record
        assert archived["scope"] == "repo-research"

        invalid = {**renamed, "other-research": {**renamed["other-research"], "default_lab": "absent"}}
        config.write_text(json.dumps({"nao": str(NAO), "projects": invalid}))
        reader = token_for(work, "reader-session", "invalid-neighbor")
        assert call_tool("list_recent_research", reader)["structuredContent"]["project"] == "repo"
        failed, error = tool("list_recent_research", reader, **other_target)
        assert failed and "other-research" in error and "default_lab" in error, error
        own_config_error = hook("UserPromptSubmit", desk, "desk-session", turn_id="invalid-own")[
            "hookSpecificOutput"]["additionalContext"]
        assert "other-research" in own_config_error and "default_lab" in own_config_error
        assert "binding_token " not in own_config_error

        config.write_text(json.dumps({"nao": str(NAO), "projects": projects}))

        def git(*args):
            subprocess.run(["git", "-C", work, *args], check=True, capture_output=True)

        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
            "commit", "--allow-empty", "-m", "Fixture")
        linked, nested = temp / "linked-worktree", work / "nested-repo"
        git("worktree", "add", "--detach", str(linked))
        git("init", "-q", str(nested))
        token_for(work, "reader-session", "worktree-hook")
        event = {"turn_id": "worktree-hook", "tool_name": "Bash", "tool_use_id": "linked",
                 "tool_input": {}, "tool_response": {"exit_code": 0}}
        assert "in the local archive" in json.dumps(hook("PostToolUse", linked, "reader-session", **event))
        assert hook("PostToolUse", nested, "reader-session", **{**event, "tool_use_id": "nested"}) == {}
        assert hook("PostToolUse", desk, "reader-session", **{**event, "tool_use_id": "other"}) == {}

        print("PASS: local archives, journal recovery, scoped reads, worktree routing and MCP cards.")


if __name__ == "__main__":
    main()
