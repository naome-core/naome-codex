# NAOME Codex

NAOME Codex creates research projects and records work in their local NAOME lab archives. It uses the released `naome` CLI and Python 3.9 or later on macOS.

## Create a project

Ask Naome to create a project with a short name such as `calibration-study`. The `create_project` tool creates a fresh workspace under `~/Documents/NAOME Projects` and a lab directory under `~/Library/Application Support/NAOME Network/Projects`. It enrolls the workspace in the plugin configuration. You can then open the returned workspace in Codex and record evidence with trusted hooks.

- The default **local** mode initializes an offline archive and leaves the node stopped. The lab can record evidence without network connectivity or a research assignment.
- The optional **network** mode creates a dedicated lab and orchestrator with separate archives and peer identities. Both nodes run on the same computer and listen on loopback. Naome waits until they report a connection to each other. Afterward the orchestrator can assign work and receive evidence from its lab.

The tool needs a working `naome` binary and no lab token. It preserves existing projects and refuses to take over an existing workspace or node directory. Repeating the same managed project name and mode returns its existing workspace, and restarts or checks the dedicated nodes in network mode. If network startup fails after enrollment, repeat the same request to retry the setup. The optional `provisioning.workspace_root` and `provisioning.node_root` settings in the plugin configuration override the parent directories for new projects.

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

2. Add the repository as a local marketplace. Install **Naome** in Codex desktop, or use the installed CLI.

   ```sh
   codex plugin marketplace add /absolute/path/to/naome-codex
   codex plugin add naome-codex@naome-local
   ```

3. Review and trust the bundled hooks in **Settings → Hooks** or **Review hooks** in the Codex composer. Open or resume a local Codex session inside the workspace. Each user message receives its own recorder context.

## Multiple labs in one chat

For a project with multiple labs, replace its `lab` path and optional `job` with `labs` and an optional `default_lab`:

```json
{
  "workspace": "/absolute/path/to/project",
  "default_lab": "A",
  "labs": {
    "A": {"directory": "/absolute/path/to/lab-a"},
    "B": {"directory": "/absolute/path/to/lab-b"}
  }
}
```

A lab can have its own optional `job` and its own node and orchestrator configuration. Put `/lab B` on the first line to select B for the current turn, or `/lab default B` to select B now and set it as this chat’s default. The equivalent leading sentences `Use Lab B for this turn` and `Use Lab B from now on` also work. Place the task on the following lines. A bare reference to another configured lab or a comparison of labs leaves the destination open unless the prompt explicitly selects one. When several labs are configured and the project has no default, start by selecting one.

Turn routing fixes the local archive and any report assignment when the user message is admitted. Later hooks use that turn’s route. Pending work retains its original destination across retries, even after another turn selects a different lab. Removed lab destinations remain pending until the original lab is configured again. Single-lab configuration and existing journal entries are supported.

## Start a network when a Codex turn begins

Set `"auto_start": true` on a project entry in the local Naome configuration to opt in. When a turn selects a network lab, Naome starts the local lab in a detached process. If the selected lab belongs to a dedicated local network created by `create_project`, the same worker also starts its matching orchestrator. A stopped node will be started again on the next matching turn while the option is enabled.

Offline projects continue to use the local archive on their own. The hook does not wait for either node. Local records and already-known assignments remain usable during startup. Requests that start without an available assignment keep their evidence local. Startup diagnostics are written to `autostart.log` beside the plugin journal.

## Capture and delivery

- The recorder stores tool completion metadata and turn interruptions or stops. A tool receipt includes its observed identities, content digests, and available machine status. Full prompts, raw tool bodies, and transcripts stay outside this automatic capture.
- Codex uses the local MCP tools to record questions, hypotheses, methods, observations, results, limitations, and linked corrections. The plugin packages research instructions that require evidence for findings. Hook coverage and agent participation determine which activities are recorded.
- Each captured record is deposited with `naome archive put` as soon as possible. The local journal retains records if the archive CLI cannot acknowledge them. The MCP status reports archived record IDs and counts pending local writes and report submissions for both the selected lab and the whole chat.
- A uniquely ready assignment enables optional reporting for subsequent records. An ambiguous assignment leaves capture local. Previously unassigned records stay in the lab archive. The bridge sends assigned records through ordered `naome research stream` segments and retries the exact segments after an ambiguous acknowledgment. The lab node handles replication to the orchestrator.
- Use approved material for the pilot. Version 0.2.0 gives inline lab stream records the REP public descriptor. A correction entry documents a revised finding and links its original record. REP policy and admitted notices govern formal reassessment.

The plugin source and local Git history contain no lab data or keys. The enrollment and journal live in the local Application Support directory.

## Verify

The tested Network version, its bundled Protocol version, and the SHA-256 checksum for the `naome-macos-arm64` release asset are pinned in [tests/naome-release.json](tests/naome-release.json). For a release update, set those values in that file. CI uses the pin to download and verify the binary, and the integration test checks the installed versions against the same pin.

Run the tests with the pinned `naome` release in `~/.cargo/bin/naome`, or set `NAOME_TEST_BINARY` to the verified executable.

```sh
python3 tests/integration.py
python3 tests/turn_routing.py
python3 tests/project_setup.py
python3 tests/autostart.py
```

The tests use isolated node and workspace directories. They cover local capture without a running node or assignment, two project scopes, lost report acknowledgments, and readback from real orchestrators. The turn-routing test connects two labs to separate orchestrators and verifies one chat across both, including archive and report delays, late hook events, an ambiguous selection, and a pending entry from an older journal. The project setup test creates both project modes through the MCP tool and checks offline archive writes, a real assignment and replication, and safe retries. The autostart test keeps both nodes stopped while writing and reading a record, then verifies that they start and connect without starting a second pair.
