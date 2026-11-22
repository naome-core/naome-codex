#!/usr/bin/python3
"""Codex hook handler and local stdio MCP tools for the NAOME research lab."""

import json
import os
import signal
import sqlite3
import subprocess
import sys

from archive_search import MAX_HITS, MODES, QUERY_LIMIT, SearchSession, search_research
from autostart import schedule
from bridge import Bridge, DeliveryError, ID, KINDS, digest, encode
from project import PROJECT, create_project
from research_card import (
    RECENT_SUMMARIES_KEY, RESOURCE, TOOL_META, ResourceNotFound, card_data, read_resource, recent_card_data,
)

ERRORS = (DeliveryError, OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.TimeoutExpired)
INPUT_LIMIT = 8 * 1024 * 1024
TOKEN = {"type": "string", "description": "The local lab token supplied by the current turn's Naome hook."}
TOKENS = {"binding_token": TOKEN}
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
MAX_RECENT_RECORDS = 10


def tool(name, description, properties, required, **metadata):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object", "properties": properties,
            "required": required, "additionalProperties": False,
        },
        **metadata,
    }


TOOLS = [
    tool("create_project", "Create and enroll a new local research workspace at the user's request. "
         "The default creates an offline lab archive. Network mode starts a dedicated lab and orchestrator "
         "on this computer and verifies their connection. Available before the workspace is enrolled.",
         {
             "project": {"type": "string", "pattern": f"^{PROJECT}$",
                         "description": "A short lowercase name for the new project and its folder."},
             "mode": {"type": "string", "enum": ["local", "network"], "default": "local"},
         }, ["project"]),
    tool("research_status", "Show pending work for the selected lab and across this chat, and the current assignment.",
         TOKENS, ["binding_token"], annotations=READ_ONLY),
    tool("list_recent_research", "List the latest explicitly recorded Naome Codex research receipts across chats "
         "for the current project and selected lab. Use for requests about what was recorded most recently. "
         "Returns titles, categories, capture times, summaries and IDs for confirmed local research records. "
         "Nonempty results have an inline card. Excludes automatic hook events and pending writes. "
         "Works without a search snapshot or model.",
         {
             **TOKENS,
             "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RECENT_RECORDS, "default": 1,
                       "description": "Use one for the latest record. Increase this only for requests for multiple records."},
         }, ["binding_token"], _meta=TOOL_META, annotations=READ_ONLY),
    tool("search_research", "Search the current project's approved snapshot in the selected local lab. "
         "Uses the native archive search and may prepare its local derived index. Returns scoped records, "
         "verified links, the searched snapshot and search coverage. Nonempty results have an inline card. "
         "An exact record ID needs no model.",
         {
             **TOKENS,
             "query": {"type": "string", "maxLength": QUERY_LIMIT,
                       "description": "A question, keywords, or a complete record ID."},
             "limit": {"type": "integer", "minimum": 1, "maximum": MAX_HITS, "default": 5},
             "mode": {"type": "string", "enum": list(MODES), "default": "auto"},
         }, ["binding_token", "query"], _meta=TOOL_META, annotations=READ_ONLY),
    tool("record_research", "Append a structured research record to the selected local lab using evidence "
         "observed in this turn. With delivery=local it stays in the lab and is not queued for submission. "
         "With delivery=assigned (the default), an active assignment can also send a report to its configured "
         "orchestrator. Use the destination authorized by the user and workspace.",
         {
             **TOKENS,
             "category": {"type": "string", "enum": list(KINDS)},
             "title": {"type": "string", "maxLength": 180},
             "summary": {"type": "string", "maxLength": 6000},
             "sources": {
                 "type": "array", "items": {"type": "string", "maxLength": 500},
                 "maxItems": 12,
                 "description": "Evidence references such as URLs, a pinned revision and path, or an observed tool event.",
             },
             "linked_records": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
             "delivery": {"type": "string", "enum": ["local", "assigned"], "default": "assigned",
                          "description": "Choose local archival only or allow submission to the active assignment."},
         },
         ["binding_token", "category", "title", "summary", "sources"], _meta=TOOL_META,
         annotations={"readOnlyHint": False, "openWorldHint": True}),
    tool("retry_research_delivery", "Retry local capture and delivery of any assigned research reports.",
         TOKENS, ["binding_token"]),
]


def tool_result(receipt, *, card=False):
    result = {"content": [{"type": "text", "text": encode(receipt)}], "isError": False}
    if card:
        result["structuredContent"] = receipt
    return result


def do_tool(name, arguments, search):
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object.")
    bridge = Bridge()
    if name == "create_project":
        return tool_result(create_project(bridge, arguments))
    binding = bridge.authorized(arguments.get("binding_token"))
    if not binding:
        raise ValueError("The active Codex turn has no valid Naome lab token.")
    if name == "research_status":
        return tool_result({"project": binding["project"], "lab": binding["lab_name"], "job": binding["job"],
                            **bridge.status(binding, include_chat=True)})
    if name == "list_recent_research":
        limit = arguments.get("limit", 1)
        if not (arguments.keys() <= {"binding_token", "limit"}
                and type(limit) is int and 1 <= limit <= MAX_RECENT_RECORDS):
            raise ValueError("Invalid recent research arguments.")
        records, summaries = recent_card_data(bridge.recent_research(binding, limit))
        reply = tool_result({"project": binding["project"], "lab": binding["lab_name"],
                             "source": "codex_archive_receipts", "records": records}, card=bool(records))
        if summaries:
            reply["_meta"] = {RECENT_SUMMARIES_KEY: summaries}
        return reply
    if name == "search_research":
        result = search_research(bridge, binding, arguments, search)
        return tool_result(result, card=bool(result["hits"]))
    if name == "retry_research_delivery":
        return tool_result(bridge.flush(binding))
    if name != "record_research":
        raise ValueError("Unknown NAOME tool.")
    category = arguments["category"]
    title, summary = arguments["title"], arguments["summary"]
    sources, supplied = arguments["sources"], arguments.get("linked_records", [])
    if not (
        category in KINDS and isinstance(title, str) and 0 < len(title.strip()) <= 180
        and isinstance(summary, str) and 0 < len(summary.strip()) <= 6000
        and isinstance(sources, list) and len(sources) <= 12
        and all(isinstance(s, str) and 0 < len(s.strip()) <= 500 for s in sources)
        and isinstance(supplied, list) and len(supplied) <= 12
        and all(isinstance(s, str) and ID.fullmatch(s) for s in supplied)
    ):
        raise ValueError("Invalid research record fields.")
    if category in {"observation", "result", "correction"} and not (sources or supplied):
        raise ValueError("Observations, results, and corrections need evidence references.")
    if category == "correction" and not supplied:
        raise ValueError("A correction must link to the original archived record.")
    delivery = arguments.get("delivery", "assigned")
    if delivery not in ("local", "assigned"):
        raise ValueError("Invalid research delivery mode.")
    references = sorted(set(supplied))
    title = title.strip()
    fields = {"summary": summary.strip(), "sources": sources, "recorded_by": "Codex MCP",
              "codex_turn": binding["turn"]}
    identity = [category, title, fields, references]
    if delivery == "local" and binding["job"]:
        # Keep the local record distinct from a previously queued assigned write of the same content.
        identity.append("local")
        binding = {**binding, "job": None}
    event = "research:" + digest(identity)
    offset = bridge.enqueue(binding, event, category, title, fields, references)
    local = bridge.archive(binding, offset)
    delivered = bridge.flush(binding) if binding["job"] else {}
    receipt = {"offset": offset, "lab": binding["lab_name"], "job": binding["job"],
               **delivered, **bridge.status(binding, offset),
               **{key: value for key, value in local.items() if key == "local_retry"}}
    return {**tool_result(receipt), **card_data(receipt, category, title, fields, references)}


def context_output(binding, progress):
    if binding["job"]:
        association = f"New records are associated with NAOME assignment {binding['job']}."
    else:
        association = "Research is recorded in the local lab archive. No assignment is selected."
    context = (
        f"Naome selected lab {binding['lab_name']} for this turn. {association} "
        "For substantive R&D in this enrolled workspace, apply the naome-lab-research skill. "
        "Follow the user and workspace instructions for permission to record and send evidence. "
        "A lab selection and binding token identify a destination and do not grant recording permission. "
        "When recording is authorized, before substantive analysis or experiments, use record_research for separate "
        "question and method entries, and for any relevant hypothesis. Record supported results and material limitations. "
        "Use delivery=local unless assignment delivery is authorized. "
        "If recording is restricted or an approval is denied, continue the allowed work without retrying the write. "
        "Check the write result and report unresolved archive failures. "
        "For routine edits, maintenance and status checks, rely on automatic hook capture unless the user asks for a research record. "
        "Use list_recent_research for the latest research records in this project and lab. "
        "The list uses local archive acknowledgments and works without a search snapshot. "
        "For simple record lookups, use the displayed research card as the answer. "
        "Add requested analysis or any material limit the card does not show. "
        f"Use binding_token {binding['token']} only for this turn. "
        f"Keep the token out of research records and chat replies. "
        f"Report unknowns explicitly and use only evidence authorized for the project. "
        f"Local archive pending: {progress['pending_local']}. "
        f"Assigned reports pending submission: {progress['pending_reports']}. "
        f"Across this chat's labs: {progress['chat_pending_local']} pending local writes and "
        f"{progress['chat_pending_reports']} pending report submissions."
    )
    return prompt_context(context)


def prompt_context(context):
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}


def hook(payload):
    bridge = Bridge()
    event = payload.get("hook_event_name")
    session = payload.get("session_id")
    turn = payload.get("turn_id")
    if not all(isinstance(value, str) and 0 < len(value) <= 160 for value in (session, turn)):
        return {}
    project = bridge.project_at(payload.get("cwd"))
    if not project:
        if event == "UserPromptSubmit":
            bridge.leave_project(session, turn)
        return {}
    if event == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            return {}
        binding, issue, labs = bridge.begin_turn(project, session, turn, prompt)
        if not binding:
            choices = ", ".join(lab["name"] for lab in labs.values())
            context = f"{issue} No Naome writes are enabled for this turn. Do not reuse a previous token."
            if choices:
                context += (f" If the user asked to record evidence, ask once which configured lab to use ({choices})."
                            " The user can put `/lab <name>` on the first line of the next message.")
            return prompt_context(context)
        progress = bridge.flush(binding)
        notice = schedule(bridge, binding)
        response = context_output(binding, progress)
        if notice:
            response["systemMessage"] = notice
        return response
    binding = bridge.turn_binding(project, session, turn)
    if not binding:
        return {}
    if event == "PostToolUse":
        name, call_id = payload.get("tool_name"), payload.get("tool_use_id")
        if (not isinstance(name, str) or not name or len(name) > 180
                or not isinstance(call_id, str) or not call_id or len(call_id) > 180):
            return {}
        if "naome" in name.lower() and "mcp" in name.lower():
            return {}
        response = payload.get("tool_response")
        fields = {"codex_turn": turn, "tool_call_id": call_id, "tool_name": name,
                  "tool_input_sha256": digest(payload.get("tool_input")),
                  "tool_response_sha256": digest(response), "recorded_by": "Codex PostToolUse"}
        if isinstance(response, dict):
            for key in ("exit_code", "isError"):
                if type(response.get(key)) in (bool, int):
                    fields[key] = response[key]
        key = f"tool:{turn}:{call_id}"
        offset = bridge.enqueue(binding, key, "observation", "Observed Codex tool completion", fields)
        local = bridge.archive(binding, offset)
        receipt = local["local_record"]
        location = f"in the local archive as {receipt}" if receipt else "in the pending local queue"
        return {"hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": f"NAOME tool event {key} is {location}.",
        }}
    elif event in {"Stop", "Interrupt"}:
        offset = bridge.enqueue(binding, f"{event}:{turn}", "observation", "Observed Codex turn event",
                                {"codex_turn": turn, "hook_event": event, "recorded_by": "Codex hook"})
        if event == "Interrupt":
            bridge.archive(binding, offset, budget=0.8)
        else:
            progress = bridge.flush(binding)
            if any(progress.get(key) for key in ("chat_pending_local", "chat_pending_reports", "local_retry", "report_retry")):
                return {"systemMessage": "NAOME still has local records or assigned reports queued."}
    return {}


def dispatch(request, search):
    method = request["method"]
    details = request.get("params", {})
    if not isinstance(details, dict):
        raise ValueError("JSON-RPC params must be an object.")
    if method == "initialize":
        supported = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
        chosen = details.get("protocolVersion")
        return {"protocolVersion": chosen if isinstance(chosen, str) and chosen in supported else "2025-06-18",
                "capabilities": {"tools": {}, "resources": {}}, "serverInfo": {"name": "Naome", "version": "0.1.0"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "resources/list":
        return {"resources": [RESOURCE]}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "resources/read":
        return read_resource(details["uri"])
    if method == "ping":
        return {}
    if method == "tools/call":
        try:
            return do_tool(details["name"], details.get("arguments", {}), search)
        except ERRORS as error:
            message = str(error) if isinstance(error, (ValueError, DeliveryError)) else (
                "The local NAOME research bridge could not process the request."
            )
            return {"content": [{"type": "text", "text": message}], "isError": True}
    return None


def mcp(search):
    for line in sys.stdin.buffer:
        request_id = None
        try:
            if len(line) > INPUT_LIMIT:
                raise ValueError("MCP request too large")
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(
                request.get("method"), str
            ):
                raise ValueError("Invalid JSON-RPC request")
            if "id" not in request:
                continue
            request_id = request["id"]
            result = dispatch(request, search)
            body = {"result": result} if result is not None else {
                "error": {"code": -32601, "message": "Method not found"}
            }
        except ResourceNotFound as error:
            body = {"error": {"code": -32002, "message": str(error)}}
        except OSError:
            body = {"error": {"code": -32603, "message": "Naome could not read the UI resource."}}
        except (KeyError, TypeError, ValueError):
            body = {"error": {"code": -32600, "message": "Invalid request"}}
        print(encode({"jsonrpc": "2.0", "id": request_id, **body}), flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    if len(sys.argv) != 2 or sys.argv[1] not in {"mcp", "hook"}:
        sys.exit("Usage: entry.py [mcp|hook]")
    if sys.argv[1] == "mcp":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        with SearchSession() as search:
            mcp(search)
    else:
        try:
            data = sys.stdin.buffer.read(INPUT_LIMIT + 1)
            if len(data) > INPUT_LIMIT:
                raise ValueError("Hook input exceeds the recorder limit.")
            print(encode(hook(json.loads(data))))
        except ERRORS as error:
            print(encode({"systemMessage": f"NAOME recorder could not process this event ({type(error).__name__})."}))
