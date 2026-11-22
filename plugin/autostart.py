"""Start the selected lab's local network without delaying the recorder."""

import fcntl
import json
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bridge import Bridge, DeliveryError, digest


def schedule(bridge, binding):
    project, lab = binding["project"], binding["lab"]
    settings = bridge.projects[project]
    if settings.get("auto_start") is not True or settings.get("mode") == "local":
        return None

    lock_path = bridge.state.parent / f"autostart-{digest(lab)[:24]}.lock"
    try:
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            with bridge.state.with_name("autostart.log").open("ab") as log:
                # The detached worker keeps the lock until both startup attempts finish.
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), project, lab],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True, pass_fds=(lock.fileno(),),
                )
    except OSError:
        return "Naome could not schedule network startup. Local research capture is still available."
    return None


def node_settings(directory, role):
    if not all((directory / name).is_file() for name in ("identity.key", "archive.sqlite", "node.json")):
        raise DeliveryError(f"The {role} directory is incomplete.")
    settings = json.loads((directory / "node.json").read_text())
    if settings["role"] != role:
        raise DeliveryError(f"The selected directory has a different node role than {role}.")
    return settings


def local_orchestrator(project, lab, settings):
    directory = Path(project["orchestrator"])
    if not directory.is_absolute() or directory.resolve() == lab:
        raise DeliveryError("The dedicated orchestrator must have its own absolute directory.")
    directory = directory.resolve()
    config = node_settings(directory, "orchestrator")
    local_address = config["listen"]
    if not (
        local_address.startswith("/ip4/127.0.0.1/tcp/")
        and settings["peers"] == [{"id": project["orchestrator_peer_id"], "address": local_address}]
        and {"id": project["lab_peer_id"], "address": settings["listen"]} in config["peers"]
    ):
        raise DeliveryError("The dedicated orchestrator does not match the selected lab's peer settings.")
    return directory, project["orchestrator_peer_id"]


def start(bridge, role, directory, expected_peer):
    try:
        status = bridge.call(directory, "status")
        was_running = status["running"]
        if not was_running:
            status = bridge.call(directory, "start", timeout=32)
        if not status["running"] or (expected_peer and status["peer_id"] != expected_peer):
            raise DeliveryError("The node did not report the expected running identity.")
        if not was_running:
            logging.info("Started %s at %s", role, directory)
    except (DeliveryError, OSError, ValueError, KeyError, TypeError) as error:
        logging.error("Could not start %s at %s: %s", role, directory, error)


def run(project_name, lab_path):
    bridge = Bridge()
    project = bridge.projects.get(project_name, {})
    binding = {"project": project_name, "lab": lab_path}
    if project.get("auto_start") is not True or project.get("mode") == "local" or not bridge.lab_allowed(binding):
        return

    lab = Path(lab_path)
    settings = node_settings(lab, "lab")
    if len(settings["peers"]) != 1:
        return
    dedicated = project.get("orchestrator") and Path(project.get("lab", "")).resolve() == lab
    nodes = {"lab": (lab, project.get("lab_peer_id") if dedicated else None)}
    if dedicated:
        try:
            nodes["orchestrator"] = local_orchestrator(project, lab, settings)
        except (DeliveryError, OSError, ValueError, KeyError, TypeError) as error:
            logging.error("Could not use the configured orchestrator: %s", error)

    with ThreadPoolExecutor(max_workers=len(nodes)) as workers:
        for role, (directory, expected_peer) in nodes.items():
            workers.submit(start, bridge, role, directory, expected_peer)


if __name__ == "__main__":
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        run(*sys.argv[1:])
    except (DeliveryError, OSError, ValueError, KeyError, TypeError) as error:
        logging.error("Naome autostart could not run: %s", error)
