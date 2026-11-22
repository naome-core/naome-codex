---
name: naome-lab-research
description: Design experiments, test hypotheses, compare studies, prototypes or benchmarks, and investigate conflicting findings. Record evidence and limitations in the selected Naome lab, look up recent research records, or create a Naome project on request.
---

# Naome research workflow

Apply this workflow to substantive R&D in an enrolled workspace when the request describes the research goal. For straightforward edits, maintenance, and status checks, use automatic hook capture unless the user requests a research record.

## Use the lab selected for the turn

Use the binding token supplied by the current `UserPromptSubmit` hook. Pass it only in Naome tool arguments. Keep tokens out of records and replies. If the work should be recorded in a particular workspace lab, call `select_lab` with its enrolled name before starting the work. Pass `remember: true` when the user asks to keep that lab as the chat default. If no lab is selected and the intended destination is unclear, ask which configured lab to use. Use the selected lab even when the orchestrator is unavailable or the turn has no research assignment.

The lab binding identifies the destination. Follow the user's request and workspace instructions for permission to record and transfer evidence. For authorized local notes, set `delivery` to `local`. Use assigned delivery only when the work authorizes submission to the assigned orchestrator. If workspace policy requires explicit permission and it is absent, continue the allowed research. If a write is necessary to the user-requested outcome, ask once for the specific operation and destination. A host approval denial remains in force. Continue the unaffected work and retry the write only after new authorization.

## Find prior lab evidence

Use `list_recent_research` when the user asks to see recent entries and `search_research` when they ask to see snapshot matches. Request one record for the latest entry. If the user refers to another project or lab, use the enrolled names from the hook as the `project` and `lab` arguments. They affect only the requested read. If several enrolled names fit the request and the context cannot resolve it, ask which one to read.

`list_recent_research` lists confirmed Codex research receipts across chats in the project without a search snapshot. Expand an entry to see its stored summary and available sources or linked records. An empty list means there are no matching Codex receipts. This lookup requires no new research entry.

Use `lookup_research` for reads during analysis. Omit `query` to read recent Codex records with their stored details. Supply `query` to search the approved snapshot. Use a question or keywords for discovery and a complete record ID for exact lookup. Treat ranked excerpts as leads and check the original evidence before using them to support a finding.

Use a displayed research card as the answer when the user only asks to see records. For analysis, present the relevant findings with sources. Include the snapshot and any material coverage limits when reporting search results. If an incomplete index returns no hits, mention that more results may become available.

## Record material research milestones

- At the start of authorized substantive research, use `capture_research` for separate question and method entries. Record a hypothesis when applicable. For an experiment, capture the inputs, comparison, and the observation that would change the conclusion before running it.
- Use `capture_research` when an observation, method change, finding, or limitation affects the research. Use `record_research` when the user asks to see the confirmed entry as a card. The hooks already capture supported tool and turn events. Cite their receipt IDs, pinned revisions and file paths, observed URLs, or supplied record IDs as evidence for a new research entry. Report errors and pending storage.
- Describe what was directly observed and what you infer from it. State the tested scope and identify missing evidence that limits the conclusion. Use `correction` for a revision to an archived finding and include the original record in `linked_records`.
- Keep manual entries concise. Routine commands and repeated status checks need no extra research entry. Include only material authorized for this project and omit credentials, private transcripts, and raw tool output.

## Check storage and delivery

- Use the hook context or the latest tool result for routine status. Call `research_status` when the storage or assignment state is unclear and affects the next action, or when the user asks.
- A returned `local_record` confirms storage for that entry. A failed local write stays in the journal for retry. Report submission and remote receipt are separate events. Use confirmed readback at the orchestrator when remote receipt matters.
- Let the hooks retry pending work during normal use. Call `retry_research_delivery` at a useful checkpoint when a prior failure can now be retried, or when the user explicitly requests it. Keep the existing entry and report any queue still pending.

## Create a project on request

When the user explicitly asks to create a project, call `create_project` with its name. Use the default `local` mode for an offline lab. Use `network` when the user asks for a dedicated lab and orchestrator. Creation needs no binding token. Return the workspace and any verified network status.
