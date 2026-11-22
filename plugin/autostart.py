"""Start the selected lab's local network without delaying the recorder."""

import fcntl
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bridge import Bridge, DeliveryError, digest
from network import managed_nodes, node_settings, start_node


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


def start(bridge, role, directory, expected_peer):
    try:
        if start_node(bridge, directory, expected_peer):
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
    dedicated = project.get("orchestrator") and Path(project.get("lab", "")).resolve() == lab
    if dedicated:
        nodes = managed_nodes(project, lab, settings)
    elif len(settings["peers"]) == 1:
        nodes = {"lab": (lab, None)}
    else:
        return

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
