---
name: naome-lab-research
description: Record research in the enrolled local NAOME lab, including hypotheses, methods, evidence, findings, limitations, and corrections.
---

Use this workflow when the NAOME session hook supplies a binding token. Use the token only in NAOME MCP tool arguments.

- Use `research_status` to check local archive status and any current assignment.
- Record the question, material hypotheses, and the intended method before work when they are available.
- Use `record_research` at material observation and result boundaries. Cite observed tool event IDs, files and pinned revisions, external URLs, or archived record IDs. Keep observed outcomes, interpretations, and unsupported assumptions distinct.
- Record an uncertainty as a limitation with the missing evidence. A correction links to the original archived record.
- Keep credentials, private raw transcripts, and unrelated information out of records. Submit only material authorized for this project.
- Use the local archive even when the conversation has no orchestrator or assignment. Treat pending local records as queued until `archive put` is acknowledged. Treat pending reports as separate from local capture, and retry at the next useful checkpoint.
