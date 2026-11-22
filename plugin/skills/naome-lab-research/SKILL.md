---
name: naome-lab-research
description: Create Naome projects and record significant research steps, evidence, limitations, and corrections in the lab selected for the current Codex turn.
---

# Naome research workflow

## Use the lab selected for the turn

Use the binding token supplied by the current `UserPromptSubmit` hook. Pass it only in Naome tool arguments. Keep tokens out of records and replies. If a requested write has no selected lab, ask once which configured lab to use. Use the selected lab even when the orchestrator is unavailable or the turn has no research assignment.

## Record material research milestones

- At the start of substantive research, record the question and any material hypothesis and method already known. For an experiment, capture the inputs, comparison, and the observation that would change the conclusion before running it.
- Use `record_research` when an observation, method change, finding, or limitation affects the research. The hooks already capture supported tool and turn events. Cite their receipt IDs, pinned revisions and file paths, observed URLs, or supplied record IDs as evidence for a new research entry.
- Describe what was directly observed and what you infer from it. State the tested scope and identify missing evidence that limits the conclusion. Use `correction` for a revision to an archived finding and include the original record in `linked_records`.
- Keep manual entries concise. Routine commands and repeated status checks need no extra research entry. Include only material authorized for this project and omit credentials, private transcripts, and raw tool output.

## Check storage and delivery

- Use the hook context or the latest tool result for routine status. Call `research_status` when the storage or assignment state is unclear and affects the next action, or when the user asks.
- A returned `local_record` confirms storage for that entry. A failed local write stays in the journal for retry. Report submission and remote receipt are separate events. Use confirmed readback at the orchestrator when remote receipt matters.
- Let the hooks retry pending work during normal use. Call `retry_research_delivery` at a useful checkpoint when a prior failure can now be retried, or when the user explicitly requests it. Keep the existing entry and report any queue still pending.

## Create a project on request

When the user explicitly asks to create a project, call `create_project` with its name. Use the default `local` mode for an offline lab. Use `network` when the user asks for a dedicated lab and orchestrator. Creation needs no binding token. Return the workspace and any verified network status.
