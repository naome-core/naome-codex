"""Create a local project and enroll its lab in Naome."""

import fcntl
import json
import os
import re
import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path

from bridge import SUPPORT, Bridge, DeliveryError, archive_file, git_common_dir

PROJECT = r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?"
DEFAULT_WORKSPACE_ROOT = Path.home() / "Documents" / "NAOME Projects"
DEFAULT_ARCHIVE_ROOT = SUPPORT / "Archives"


def create_project(bridge, arguments):
    name = arguments.get("project")
    if not isinstance(name, str) or not re.fullmatch(PROJECT, name) or set(arguments) - {"project", "mode"}:
        raise ValueError("Provide a project name of 1–48 lowercase letters, digits, or internal hyphens.")
    if arguments.get("mode", "local") != "local":
        raise ValueError("Naome Codex creates local archive projects.")

    path = bridge.config_path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_name("provisioning.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DeliveryError("Another Naome project is being created. Retry in a moment.") from error
        bridge = Bridge()
        configured = bridge.config.get("projects", {})
        project = configured.get(name)
        if project is None:
            if name in bridge.projects or name in bridge.project_scopes:
                raise ValueError(f"The project name or archive scope {name} is already registered.")
            project = initialize_project(bridge, name)
            created = True
        else:
            # Earlier managed projects stored their mode instead of a managed flag.
            if project.get("managed") is not True and project.get("mode") not in {"local", "network"}:
                raise ValueError(f"Project {name} already exists with a different setup.")
            created = False
        if not Path(project["workspace"]).is_dir():
            raise DeliveryError(f"The registered workspace for {name} is unavailable.")
        lab = project["lab"]
        bridge.require_lab(lab)
        return {"project": name, "created": created, "workspace": project["workspace"],
                "lab": {"directory": lab, "archive": str(archive_file(lab))}}


def initialize_project(bridge, name):
    settings = bridge.config.get("provisioning", {})
    workspace_root = Path(settings.get("workspace_root", DEFAULT_WORKSPACE_ROOT))
    archive_root = Path(settings.get("archive_root", settings.get("node_root", DEFAULT_ARCHIVE_ROOT)))
    if not workspace_root.is_absolute() or not archive_root.is_absolute():
        raise ValueError("Naome provisioning roots must be absolute paths.")
    workspace, lab = workspace_root.resolve() / name, archive_root.resolve() / name
    if workspace.is_relative_to(lab) or lab.is_relative_to(workspace):
        raise ValueError("The workspace and archive directories must be separate.")
    if any(path.exists() or path.is_symlink() for path in (workspace, lab)):
        raise ValueError(f"An existing workspace or archive directory already uses the name {name}.")
    for enrolled in bridge.projects.values():
        prior = Path(enrolled["workspace"]).resolve()
        if workspace.is_relative_to(prior) or prior.is_relative_to(workspace):
            raise ValueError("The new workspace overlaps an enrolled project.")

    workspace.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lab.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if git_common_dir(workspace.parent) is not None:
        raise ValueError("Create the new project outside an existing Git checkout.")

    with ExitStack() as rollback:
        lab.mkdir(mode=0o700)
        rollback.callback(shutil.rmtree, lab)
        workspace.mkdir(mode=0o700)
        rollback.callback(workspace.rmdir)
        bridge.call(lab, "init", timeout=8)
        bridge.require_lab(lab)
        project = {"workspace": str(workspace), "lab": str(lab), "managed": True}
        enroll(bridge, name, project)
        rollback.pop_all()
    return project


def enroll(bridge, name, project):
    path = bridge.config_path
    amended = {**bridge.config, "projects": {**bridge.config.get("projects", {}), name: project}}
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(amended, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
            current = path.read_bytes() if path.is_file() else None
            if bridge.config_bytes != current:
                raise DeliveryError("The Naome configuration changed during setup. Retry the project creation.")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
