"""Create a local project and enroll its lab in Naome."""

import fcntl
import json
import os
import re
import shutil
import socket
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from bridge import Bridge, DeliveryError, git_common_dir
from network import LOOPBACK_TCP, managed_nodes, node_settings, start_node

PROJECT = r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?"
DEFAULT_WORKSPACE_ROOT = Path.home() / "Documents" / "NAOME Projects"
DEFAULT_NODE_ROOT = Path.home() / "Library/Application Support/NAOME Network/Projects"


def create_project(bridge, arguments):
    name, mode = arguments.get("project"), arguments.get("mode", "local")
    if not isinstance(name, str) or not re.fullmatch(PROJECT, name) or set(arguments) - {"project", "mode"}:
        raise ValueError("Provide a project name of 1–48 lowercase letters, digits, or internal hyphens.")
    if mode not in ("local", "network"):
        raise ValueError("Choose local or network as the project mode.")

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
        if name in configured and (not isinstance(project, dict) or project.get("mode") != mode):
            raise ValueError(f"Project {name} already exists with a different setup.")
        if project is None:
            if name in bridge.projects or name in bridge.project_scopes:
                raise ValueError(f"The project name or archive scope {name} is already registered.")
            project = initialize_project(bridge, name, mode)
            created = True
        else:
            created = False
        return project_status(bridge, name, project, created)


def initialize_project(bridge, name, mode):
    settings = bridge.config.get("provisioning", {})
    workspace_root = Path(settings.get("workspace_root", DEFAULT_WORKSPACE_ROOT))
    node_root = Path(settings.get("node_root", DEFAULT_NODE_ROOT))
    if not workspace_root.is_absolute() or not node_root.is_absolute():
        raise ValueError("Naome provisioning roots must be absolute paths.")
    workspace, nodes = workspace_root.resolve() / name, node_root.resolve() / name
    if workspace.is_relative_to(nodes) or nodes.is_relative_to(workspace):
        raise ValueError("The workspace and node directories must be separate.")
    if any(path.exists() or path.is_symlink() for path in (workspace, nodes)):
        raise ValueError(f"An existing workspace or node directory already uses the name {name}.")
    for enrolled in bridge.projects.values():
        prior = Path(enrolled["workspace"]).resolve()
        if workspace.is_relative_to(prior) or prior.is_relative_to(workspace):
            raise ValueError("The new workspace overlaps an enrolled project.")

    workspace.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    nodes.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if git_common_dir(workspace.parent) is not None:
        raise ValueError("Create the new project outside an existing Git checkout.")

    with ExitStack() as rollback:
        nodes.mkdir(mode=0o700)
        rollback.callback(shutil.rmtree, nodes)
        workspace.mkdir(mode=0o700)
        rollback.callback(workspace.rmdir)
        lab = nodes / "lab"
        try:
            lab_id = bridge.call(lab, "archive", "init", timeout=8)["peer_id"]
            project = {"workspace": str(workspace), "lab": str(lab), "mode": mode, "lab_peer_id": lab_id}
            if mode == "network":
                orchestrator = nodes / "orchestrator"
                orchestrator_id = bridge.call(orchestrator, "archive", "init", timeout=8)["peer_id"]
                with socket.socket() as lab_port, socket.socket() as orchestrator_port:
                    lab_port.bind(("127.0.0.1", 0))
                    orchestrator_port.bind(("127.0.0.1", 0))
                    lab_addr = f"{LOOPBACK_TCP}{lab_port.getsockname()[1]}"
                    orchestrator_addr = f"{LOOPBACK_TCP}{orchestrator_port.getsockname()[1]}"
                    write_node(lab, "lab", lab_addr, [{"id": orchestrator_id, "address": orchestrator_addr}])
                    write_node(orchestrator, "orchestrator", orchestrator_addr, [{"id": lab_id, "address": lab_addr}])
                project.update(orchestrator=str(orchestrator), orchestrator_peer_id=orchestrator_id)
            else:
                write_node(lab, "lab", f"{LOOPBACK_TCP}0", [])
        except DeliveryError as error:
            raise DeliveryError("Naome could not initialize the project archives. Check the installed naome CLI.") from error

        enroll(bridge, name, project)
        rollback.pop_all()
    return project


def write_node(directory, role, address, peers):
    config = {"role": role, "listen": address, "peers": peers}
    (directory / "node.json").write_text(json.dumps(config, indent=2) + "\n")


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


def project_status(bridge, name, project, created):
    lab, mode = project["lab"], project["mode"]
    if not Path(project["workspace"]).is_dir():
        raise DeliveryError(f"The registered workspace for {name} is unavailable.")
    result = {"project": name, "mode": mode, "created": created, "workspace": project["workspace"],
              "lab": {"directory": lab, "peer_id": project["lab_peer_id"]}}
    if mode == "local":
        bridge.require_lab(lab)
        result["lab"]["running"] = bridge.call(lab, "status")["running"]
        return result

    try:
        nodes = managed_nodes(project, Path(lab), node_settings(Path(lab), "lab"))
        for directory, peer_id in nodes.values():
            start_node(bridge, directory, peer_id)
        orchestrator, orchestrator_peer_id = nodes["orchestrator"]
        _, lab_peer_id = nodes["lab"]
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            lab_status = bridge.call(lab, "status")
            orchestrator_status = bridge.call(orchestrator, "status")
            if (orchestrator_peer_id in lab_status["connected_peers"]
                    and lab_peer_id in orchestrator_status["connected_peers"]):
                result["lab"]["running"] = True
                result["orchestrator"] = {"directory": str(orchestrator), "peer_id": orchestrator_peer_id, "running": True}
                result["connected"] = True
                return result
            time.sleep(0.1)
        raise DeliveryError("Both nodes did not report the expected peer connection.")
    except (DeliveryError, OSError, ValueError, KeyError, TypeError) as error:
        raise DeliveryError(f"Project {name} is registered, but its network is not ready. Retry the same setup. {error}") from error
