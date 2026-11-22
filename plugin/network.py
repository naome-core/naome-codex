"""Validate registered local network peers and start their nodes."""

import json
from pathlib import Path

from bridge import DeliveryError

LOOPBACK_TCP = "/ip4/127.0.0.1/tcp/"


def node_settings(directory, role):
    if not all((directory / name).is_file() for name in ("identity.key", "archive.sqlite", "node.json")):
        raise DeliveryError(f"The {role} directory is incomplete.")
    settings = json.loads((directory / "node.json").read_text(encoding="utf-8"))
    if settings["role"] != role:
        raise DeliveryError(f"The selected directory has a different node role than {role}.")
    return settings


def managed_nodes(project, lab, lab_settings):
    orchestrator = Path(project["orchestrator"])
    if not lab.is_absolute() or not orchestrator.is_absolute() or orchestrator.resolve() == lab.resolve():
        raise DeliveryError("The dedicated network requires distinct absolute node directories.")
    orchestrator = orchestrator.resolve()
    orchestrator_settings = node_settings(orchestrator, "orchestrator")
    lab_peer = {"id": project["lab_peer_id"], "address": lab_settings["listen"]}
    orchestrator_peer = {"id": project["orchestrator_peer_id"], "address": orchestrator_settings["listen"]}
    if not (
        lab_peer["address"].startswith(LOOPBACK_TCP)
        and orchestrator_peer["address"].startswith(LOOPBACK_TCP)
        and lab_settings["peers"] == [orchestrator_peer]
        and lab_peer in orchestrator_settings["peers"]
    ):
        raise DeliveryError("The dedicated network does not match the registered lab and orchestrator peers.")
    return {"orchestrator": (orchestrator, orchestrator_peer["id"]), "lab": (lab, lab_peer["id"])}


def start_node(bridge, directory, expected_peer):
    status = bridge.call(str(directory), "status")
    was_running = status["running"]
    if not was_running:
        # Give the CLI's native startup deadline time to clean up on failure.
        status = bridge.call(str(directory), "start", timeout=32)
    if not status["running"] or (expected_peer and status["peer_id"] != expected_peer):
        raise DeliveryError("The node did not report the expected running identity.")
    return not was_running
