"""Provision a local archive through the normal MCP entry point."""

import json
import os
import tempfile
from pathlib import Path

from integration import NAO, cli, prompt, value


def main():
    with tempfile.TemporaryDirectory(prefix="naome-project-setup-") as scratch:
        root = Path(scratch).resolve()
        workspaces, archives = root / "workspaces", root / "archives"
        config = root / "config.json"
        existing = {"workspace": str(root / "existing-workspace"), "lab": str(root / "existing-lab")}
        settings = {"nao": str(NAO), "projects": {"existing": existing},
                    "provisioning": {"workspace_root": str(workspaces), "archive_root": str(archives)}}
        config.write_text(json.dumps(settings))
        env = dict(os.environ, NAOME_CODEX_CONFIG=str(config), NAOME_CODEX_STATE=str(root / "journal.sqlite"))

        assert "project name" in value(env, "create_project", error=True, project="../outside")
        assert "local archive projects" in value(env, "create_project", error=True, project="remote", mode="network")
        assert not workspaces.exists() and not archives.exists()
        assert "already exists" in value(env, "create_project", error=True, project="existing")

        project = value(env, "create_project", project="calibration-study")
        workspace, lab = workspaces / "calibration-study", archives / "calibration-study"
        assert project == {"project": "calibration-study", "created": True, "workspace": str(workspace),
                           "lab": {"directory": str(lab), "archive": str(lab / "archive.sqlite")}}
        assert workspace.is_dir() and (lab / "archive.sqlite").is_file()
        assert not (lab / "node.json").exists() and not (lab / "identity.key").exists()
        assert value(env, "create_project", project="calibration-study") == {**project, "created": False}
        token, _ = prompt(env, workspace, "calibration-chat", "one")
        saved = value(env, "record_research", binding_token=token, category="hypothesis",
                      title="Local evidence", summary="The archive is available.", sources=[])
        identifier = saved["local_record"]
        assert identifier and saved["pending_local"] == 0
        head = cli(lab, "snapshot", "Project readback", "--root", identifier)
        assert cli(lab, "show", head, identifier)["subject"] == "Local evidence"

        current = json.loads(config.read_text())
        config.write_text(json.dumps({**current, "nao": str(root / "missing-cli")}))
        assert "unavailable" in value(env, "create_project", error=True, project="rollback")
        assert not (workspaces / "rollback").exists() and not (archives / "rollback").exists()
        assert "rollback" not in json.loads(config.read_text())["projects"]
        config.write_text(json.dumps(current))
        (archives / "reserved").mkdir()
        assert "already uses" in value(env, "create_project", error=True, project="reserved")
        assert not (workspaces / "reserved").exists()
        print("PASS: local project creation, readback, idempotency, and rollback.")


if __name__ == "__main__":
    main()
