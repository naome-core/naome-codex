# NAOME Codex

NAOME Codex records work in the local NAOME lab archive. It uses the released `naome` CLI and Python 3.9 or later on macOS. The pilot is tested with NAOME Network v0.2.0 and REP v0.1.2.

## Use in Codex

1. Enroll a local workspace and lab directory in `~/Library/Application Support/NAOME Codex/config.json`. The lab needs an initialized archive and a `node.json` with `"role":"lab"`. Local archival works with a stopped node, an empty peer list, or an unavailable orchestrator.

   ```json
   {
     "naome": "/absolute/path/to/naome",
     "projects": {
       "research-project": {
         "workspace": "/absolute/path/to/project",
         "lab": "/absolute/path/to/lab"
       }
     }
   }
   ```

   Project keys match NAOME assignment project names. A project without Git may use any dedicated directory. A Git enrollment also covers linked worktrees of the same repository. An optional `job` field restricts automatic association to that assignment.

2. Add the repository as a local marketplace. Install **NAOME Research Lab** in Codex desktop, or use the installed CLI.

   ```sh
   codex plugin marketplace add /absolute/path/to/naome-codex
   codex plugin add naome-codex@naome-local
   ```

3. Review and trust the bundled hooks in **Settings → Hooks** or **Review hooks** in the Codex composer. Open or resume a local Codex session inside the workspace. The hooks supply the session’s local recorder context.

## Capture and delivery

- The recorder stores tool completion metadata and turn interruptions or stops. A tool receipt includes its observed identities, content digests, and available machine status. Full prompts, raw tool bodies, and transcripts stay outside this automatic capture.
- Codex uses the local MCP tools to record questions, hypotheses, methods, observations, results, limitations, and linked corrections. The plugin packages research instructions that require evidence for findings. Hook coverage and agent participation determine which activities are recorded.
- Each captured record is deposited with `naome archive put` as soon as possible. The local journal retains records if the archive CLI cannot acknowledge them. The MCP status distinguishes archived record IDs, pending local writes, and pending report submissions.
- A uniquely ready assignment enables optional reporting for subsequent records. An ambiguous assignment leaves capture local. Previously unassigned records stay in the lab archive. The bridge sends assigned records through ordered `naome research stream` segments and retries the exact segments after an ambiguous acknowledgment. The lab node handles replication to the orchestrator.
- Use approved material for the pilot. Version 0.2.0 gives inline lab stream records the REP public descriptor. A correction entry documents a revised finding and links its original record. REP policy and admitted notices govern formal reassessment.

The plugin source and local Git history contain no lab data or keys. The enrollment and journal live in the local Application Support directory.

## Verify

Run the integration test with the tested `naome` release in `~/.cargo/bin/naome`, or set `NAOME_TEST_BINARY` to the verified executable.

```sh
python3 tests/integration.py
```

The test uses isolated node and workspace directories. It exercises local capture without a running node, configured orchestrator, or assignment. It checks local retry, two project scopes, lost report acknowledgments, and readback from a real orchestrator.
