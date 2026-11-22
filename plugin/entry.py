#!/usr/bin/python3
"""Codex hook handler and local stdio MCP tools for the NAOME research lab."""

import json
import os
import signal
import sqlite3
import subprocess
import sys

from archive_search import MAX_HITS, MODES, QUERY_LIMIT, SearchSession, search_research
from bridge import Bridge, DeliveryError, ID, KINDS, digest, encode
from project import PROJECT, create_project
from research_card import (
    RECENT_DETAILS_KEY, RESOURCE, TOOL_META, ResourceNotFound, card_data, read_resource, recent_card_data,
)

ERRORS = (DeliveryError, OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.TimeoutExpired)
INPUT_LIMIT = 8 * 1024 * 1024
TOKEN = {"type": "string", "description": "The local lab token supplied by the current turn's Naome hook."}
TOKENS = {"binding_token": TOKEN}
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}
MAX_RECENT_RECORDS = 10
LAB = {"type": "string", "description": "An exact lab name from the enrolled project catalog."}
READ_TARGET = {
    "project": {"type": "string", "description": "The exact enrolled project the user wants to read. Omit for the workspace project."},
    "lab": LAB,
}
SEARCH_QUERY = {"type": "string", "maxLength": QUERY_LIMIT,
                "description": "A question, keywords, or a complete record ID."}
SEARCH_MODE = {"type": "string", "enum": list(MODES), "default": "auto"}
RECORD_FIELDS = {
    **TOKENS,
    "category": {"type": "string", "enum": list(KINDS)},
    "title": {"type": "string", "maxLength": 180},
    "summary": {"type": "string", "maxLength": 6000},
    "sources": {"type": "array", "items": {"type": "string", "maxLength": 500}, "maxItems": 12,
                "description": "Evidence references such as URLs, a pinned revision and path, or an observed tool event."},
    "linked_records": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
    "delivery": {"type": "string", "enum": ["local"], "default": "local",
                 "description": "Optional compatibility argument for local archival."},
}
RECORD_REQUIRED = ["binding_token", "category", "title", "summary", "sources"]
RECORD_ANNOTATIONS = {"readOnlyHint": False, "openWorldHint": False}


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
    tool("create_project", "Create and enroll a new research workspace with a local Protocol archive. "
         "Available before the workspace is enrolled.",
         {
             "project": {"type": "string", "pattern": f"^{PROJECT}$",
                         "description": "A short lowercase name for the new project and its folder."},
         }, ["project"]),
    tool("research_status", "Show pending archive writes for the selected lab and across this chat.",
         TOKENS, ["binding_token"], annotations=READ_ONLY),
    tool("select_lab", "Select a lab within this workspace project before recording evidence in this turn. "
         "Set remember only when the user wants it as this chat's default lab.",
         {**TOKENS, "lab": LAB, "remember": {"type": "boolean", "default": False}},
         ["binding_token", "lab"], annotations={"readOnlyHint": False, "openWorldHint": False}),
    tool("list_recent_research", "Show the latest Naome Codex research receipts when the user asks to see them. "
         "Pass project when the user asks to look in another enrolled project. "
         "Returns titles, categories, capture times, summaries and IDs for confirmed local research records. "
         "Nonempty results have an inline card. Excludes automatic hook events and pending writes. "
         "Works without a search snapshot or model.",
         {
             **TOKENS,
             **READ_TARGET,
             "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RECENT_RECORDS, "default": 1,
                       "description": "Use one for the latest record. Increase this only for requests for multiple records."},
         }, ["binding_token"], _meta=TOOL_META, annotations=READ_ONLY),
    tool("search_research", "Show matches in an approved snapshot when the user asks to see search results. "
         "Pass project when the user asks to look in another enrolled project. "
         "Uses the native archive search and may prepare its local derived index. Returns scoped records, "
         "verified links, the searched snapshot and search coverage. Nonempty results have an inline card. "
         "An exact record ID needs no model.",
         {
             **TOKENS,
             **READ_TARGET,
             "query": SEARCH_QUERY,
             "limit": {"type": "integer", "minimum": 1, "maximum": MAX_HITS, "default": 5},
             "mode": SEARCH_MODE,
         }, ["binding_token", "query"], _meta=TOOL_META, annotations=READ_ONLY),
    tool("lookup_research", "Read Naome research as an intermediate step for an analysis, without an inline card. "
         "Omit query for recent Codex records. Supply query to search the approved snapshot. "
         "Pass project and lab to select an enrolled read destination.",
         {
             **TOKENS, **READ_TARGET, "query": SEARCH_QUERY, "mode": {"type": "string", "enum": list(MODES)},
             "limit": {"type": "integer", "minimum": 1, "maximum": MAX_HITS,
                       "description": "Maximum results. Defaults to one for recent records or five for snapshot search."},
         }, ["binding_token"], annotations=READ_ONLY),
    tool("record_research", "Archive an authorized research record and show its card when the user asks to see it. "
         "The record is stored in the local lab archive.",
         RECORD_FIELDS, RECORD_REQUIRED, _meta=TOOL_META, annotations=RECORD_ANNOTATIONS),
    tool("capture_research", "Archive an authorized research record during research and return a short text receipt. "
         "Check local_record and the pending count to confirm storage.",
         RECORD_FIELDS, RECORD_REQUIRED, annotations=RECORD_ANNOTATIONS),
    tool("retry_research_delivery", "Retry this chat's pending local archive writes.",
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
    background = name in {"lookup_research", "capture_research"}
    if name == "lookup_research":
        name = "search_research" if "query" in arguments else "list_recent_research"
    elif name == "capture_research":
        name = "record_research"
    if name == "select_lab":
        remember = arguments.get("remember", False)
        if not (arguments.keys() <= {"binding_token", "lab", "remember"} and type(remember) is bool):
            raise ValueError("Invalid lab selection arguments.")
        selected = bridge.select_lab(binding, arguments.get("lab"), remember)
        if not selected:
            raise ValueError("This is no longer the active Codex turn.")
        return tool_result({"project": selected["project_name"], "lab": selected["lab_name"]})
    destination = {"project": binding["project_name"], "lab": binding["lab_name"]}
    if name == "research_status":
        return tool_result({**destination, **bridge.status(binding, include_chat=True)})
    if name == "list_recent_research":
        limit = arguments.get("limit", 1)
        if not (arguments.keys() <= {"binding_token", "limit", *READ_TARGET}
                and type(limit) is int and 1 <= limit <= MAX_RECENT_RECORDS):
            raise ValueError("Invalid recent research arguments.")
        target = bridge.read_destination(binding, arguments)
        receipt = {"project": target["project_name"], "lab": target["lab_name"],
                   "source": "codex_archive_receipts", "records": bridge.recent_research(target, limit)}
        if background:
            return tool_result(receipt)
        records, details = recent_card_data(receipt["records"])
        reply = tool_result({**receipt, "records": records}, card=bool(records))
        if details:
            reply["_meta"] = {RECENT_DETAILS_KEY: details}
        return reply
    if name == "search_research":
        result = search_research(bridge, bridge.read_destination(binding, arguments), arguments, search)
        return tool_result(result, card=not background and (bool(result["hits"]) or result["coverage"] != "complete"))
    if name == "retry_research_delivery":
        return tool_result(bridge.flush(binding))
    if name != "record_research":
        raise ValueError("Unknown NAOME tool.")
    if not arguments.keys() <= RECORD_FIELDS.keys():
        raise ValueError("Invalid research record arguments.")
    if not binding["lab"]:
        raise ValueError("Use select_lab before recording in this workspace.")
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
    if arguments.get("delivery", "local") != "local":
        raise ValueError("Naome Codex stores research records locally. Omit delivery or use local.")
    references = sorted(set(supplied))
    title = title.strip()
    fields = {"summary": summary.strip(), "sources": sources, "recorded_by": "Codex MCP",
              "codex_turn": binding["turn"]}
    event = "research:" + digest([category, title, fields, references])
    offset = bridge.enqueue(binding, event, category, title, fields, references)
    local = bridge.archive(binding, offset)
    receipt = {"offset": offset, **destination, **bridge.status(binding, offset),
               **{key: value for key, value in local.items() if key == "local_retry"}}
    reply = tool_result(receipt)
    if not background:
        reply.update(card_data(receipt, category, title, fields, references))
    return reply


def context_output(binding, progress, projects):
    lab = binding["lab_name"]
    selection = (f"Naome selected lab {lab} for this turn. " if lab else "No lab is selected for recording. ")
    selection += f"The workspace project is {binding['project_name']}. "
    reading = (
        "Use list_recent_research or search_research to display results the user asks to see. "
        "Use lookup_research for reads during analysis and capture_research for short research write receipts. "
        "For reads in a project or lab requested by the user, pass the enrolled names as project and lab tool arguments. "
        f"Enrolled projects and labs are {encode(projects)}. "
        "An empty lab list means that project's lab configuration needs attention. "
        "The list uses local archive acknowledgments and works without a search snapshot. "
        "For simple record lookups, use the displayed research card as the answer. "
        "Add requested analysis or any material limit the card does not show. "
        f"Use binding_token {binding['token']} only for this turn. "
        "Keep the token out of research records and chat replies. "
        "Report unknowns explicitly and use only evidence authorized for the project. "
    )
    context = (
        f"{selection}Research is stored in the local lab archive. "
        "If recording requires a lab selection, call select_lab with the intended workspace lab before working or recording. "
        "For substantive R&D in this enrolled workspace, apply the naome-lab-research skill. "
        "Follow the user and workspace instructions for permission to record evidence. "
        "A lab selection and binding token identify a destination and do not grant recording permission. "
        "When recording is authorized, before substantive analysis or experiments, use capture_research for separate "
        "question and method entries, and for any relevant hypothesis. Record supported results and material limitations. "
        "If recording is restricted or an approval is denied, continue the allowed work without retrying the write. "
        "Check the write result and report unresolved archive failures. "
        "For routine edits, maintenance and status checks, rely on automatic hook capture unless the user asks for a research record. "
        f"{reading}"
        f"Local archive pending: {progress['pending_local']}. "
        f"Across this chat's labs: {progress['chat_pending_local']} pending local writes."
    )
    return prompt_context(context)


def prompt_context(context):
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}


def hook(payload):
    event = payload.get("hook_event_name")
    session = payload.get("session_id")
    turn = payload.get("turn_id")
    if not all(isinstance(value, str) and 0 < len(value) <= 160 for value in (session, turn)):
        return {}
    if event == "PostToolUse":
        name, call_id = payload.get("tool_name"), payload.get("tool_use_id")
        if (not isinstance(name, str) or not name or len(name) > 180
                or not isinstance(call_id, str) or not call_id or len(call_id) > 180):
            return {}
        if "naome" in name.lower() and "mcp" in name.lower():
            return {}
    bridge = Bridge()
    cwd = payload.get("cwd")
    if event == "UserPromptSubmit":
        project = bridge.project_at(cwd)
        if not project:
            bridge.leave_project(session, turn)
            return {}
        try:
            binding, issue = bridge.begin_turn(project, session, turn)
        except ValueError as error:
            return prompt_context(str(error))
        if not binding:
            return prompt_context(f"{issue} Do not reuse a previous token.")
        progress = bridge.flush(binding)
        return context_output(binding, progress, bridge.project_catalog())
    binding = bridge.turn_binding(cwd, session, turn)
    if not binding:
        return {}
    if event == "PostToolUse":
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
            if progress["chat_pending_local"] or progress.get("local_retry"):
                return {"systemMessage": "NAOME still has local records queued."}
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
