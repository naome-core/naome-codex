# NAOME Codex

NAOME Codex creates research projects and records, searches, and displays evidence in local NAOME Protocol archives. It uses the standalone [`nao` CLI](https://github.com/naome-core/naome-protocol/releases/tag/v0.1.6) and Python 3.9 or later on macOS.

## Create a project

Ask Naome to create a project with a short name such as `calibration-study`. The `create_project` tool creates a workspace under `~/Documents/NAOME Projects`, initializes its `archive.sqlite` under `~/Library/Application Support/NAOME Codex/Archives`, and enrolls the workspace. Open the returned workspace in Codex to record evidence. Repeating the same managed project name returns the original workspace and archive. Optional `provisioning.workspace_root` and `provisioning.archive_root` settings override the parent directories.

## Use in Codex

1. Set the standalone `nao` executable in `~/Library/Application Support/NAOME Codex/config.json` or make it available on the plugin's PATH. To enroll an existing workspace, specify its working directory and the lab directory containing `archive.sqlite`.

   ```json
   {
     "nao": "/absolute/path/to/nao",
     "projects": {
       "research-project": {
         "workspace": "/absolute/path/to/project",
         "lab": "/absolute/path/to/lab"
       }
     }
   }
   ```

   To initialize an archive for manual enrollment, run `nao --db /absolute/path/to/lab/archive.sqlite init`. A project key also identifies its archive scope by default. To rename an enrollment, set `archive_scope` to the previous identifier so the archive, journal receipts, and chat routes stay accessible. Linked Git worktrees share their repository's enrollment.

2. Add the repository as a local marketplace. Install **Naome** in Codex desktop or use the Codex CLI.

   ```sh
   codex plugin marketplace add /absolute/path/to/naome-codex
   codex plugin add naome-codex@naome-local
   ```

3. Review and trust the bundled hooks in **Settings → Hooks** or **Review hooks** in the Codex composer. Open or resume a local Codex session in the workspace.

### Upgrade existing projects

Keep the existing workspace and lab paths, archive files, search snapshots, and local journal. Install the standalone Protocol executable and set `nao` as shown above. Existing `archive.sqlite` files work at their original paths. Codex retains pending local writes at their original destinations. Historical report metadata remains in the journal, and the plugin no longer submits reports.

### Switch projects or labs

To read another project or lab, ask Codex to pass its enrolled names as `project` and `lab` arguments to the read tool. These arguments apply to that read. Codex records evidence in the project where the workspace is enrolled.

For a project with multiple labs, replace `lab` with `labs` and optionally set `default_lab`.

```json
{
  "workspace": "/absolute/path/to/project",
  "default_lab": "A",
  "labs": {
    "A": {"directory": "/absolute/path/to/a"},
    "B": {"directory": "/absolute/path/to/b"}
  }
}
```

To record in a particular lab, ask Codex to call `select_lab` before recording evidence. Use `remember: true` to keep the selection as that chat's default. The selected lab stays bound to that turn. Pending entries retain their original archive when another turn changes labs. If a lab is removed from the configuration, its entries remain queued until it is enrolled again.

## Read and search records

Use `list_recent_research` to see confirmed Codex research receipts from the selected lab, including earlier chats. It works without a search snapshot and skips automatic hook events and pending writes. Use `lookup_research` for text results during analysis. Omit `query` to read recent receipts with their stored details.

For snapshot search, set `search_head` beside the single `lab` path or on the relevant entry in `labs`. Use an inventory whose records and dependencies may be indexed locally for that project. To include newer evidence, review and approve a newer snapshot. The plugin uses the configured snapshot and checks each returned link in the same project and snapshot.

Use `search_research` to display matches, or supply a `query` to `lookup_research` during analysis. Search uses a question, keywords, or a complete Record ID and reports incomplete index coverage even when the current result is empty. The plugin reuses the native `nao --db FILE search-session` worker while its destination and runtime stay the same.

Exact IDs and keyword search work without a model. For hybrid or semantic search, set the model and optionally the Python path in the local configuration. Follow the [Protocol model setup](https://github.com/naome-core/naome-protocol/blob/v0.1.6/README.md#search-with-a-local-model).

```json
{
  "search": {
    "model": "/absolute/path/to/embeddinggemma-2",
    "python": "/absolute/path/to/inference-environment/bin/python"
  }
}
```

## Capture and cards

- The recorder stores tool completion metadata and turn interruptions or stops. It records identities, content digests, and available machine status. Full prompts, raw tool bodies, and transcripts stay outside automatic capture.
- Codex can record questions, hypotheses, methods, observations, results, limitations, and linked corrections with permission. Records are written with `nao put` and acknowledged in the local journal. If the CLI cannot acknowledge a record, the journal retains it for retry.
- A single result appears as a detailed card. Multiple results appear as a compact list with up to three rows initially. Expand the list for all rows and select **more** on a record for the ID and available details.
- Use `record_research` to display a confirmed write. Use `capture_research` for a short text receipt, `research_status` to see pending writes, and `retry_research_delivery` to retry them locally. Both write tools use the same journal.
- The repository contains synthetic integration fixtures. User archives, configuration, and the journal remain on the local machine.

## Verify

The tested Protocol release and SHA-256 checksum of its macOS arm64 archive are pinned in [tests/naome-release.json](tests/naome-release.json). Set `NAO_TEST_BINARY` to the verified executable or install it at `~/.cargo/bin/nao`.

```sh
python3 tests/integration.py
python3 tests/archive_search.py
python3 tests/turn_routing.py
python3 tests/project_setup.py
python3 tests/legacy_archives.py
node tests/research_card.mjs
```

The tests use isolated archives, including two SQLite archives produced by the prior release. The upgrade test checks existing Record and Snapshot IDs, journal receipts, and new writes and searches in both archives. Set `NAOME_TEST_SEARCH_MODEL` and optionally `NAOME_TEST_SEARCH_PYTHON` to exercise real semantic and hybrid queries.
