---
name: naome-lab-research
description: Create a local NAOME research project or record questions, methods, evidence, findings, limitations, and corrections in its lab.
---

When the user explicitly asks to create a new Naome project, call `create_project` with the requested project name. Use its default `local` mode for an offline lab. Use `network` only if the user asks for an independent lab and orchestrator network. Creation can run before the workspace is enrolled and takes no binding token. Report the workspace returned by the tool and the verified network state, if requested.

Use this workflow when the Naome `UserPromptSubmit` hook supplies a binding token. Use only the current turn's token and only in Naome MCP tool arguments. The hook fixes the lab for the turn. If no lab was selected and the user asks to record evidence, ask which configured lab to use.

- Use `research_status` to check local archive status and any current assignment.
- Record the question, material hypotheses, and the intended method before work when they are available.
- Use `record_research` at material observation and result boundaries. Cite observed tool event IDs, files and pinned revisions, external URLs, or archived record IDs. Keep observed outcomes, interpretations, and unsupported assumptions distinct.
- Record an uncertainty as a limitation with the missing evidence. A correction links to the original archived record.
- Keep credentials, private raw transcripts, and unrelated information out of records. Submit only material authorized for this project.
- Use the local archive even when the conversation has no orchestrator or assignment. Treat pending local records as queued until `archive put` is acknowledged. Treat pending reports as separate from local capture, and retry at the next useful checkpoint.
