"""Search one approved lab inventory through the native NAOME process."""

import json
import os
import select
import signal
import subprocess
import time
from pathlib import Path

from bridge import Bridge, DeliveryError, ID, encode

MODES = ("auto", "lexical", "semantic", "hybrid")
QUERY_LIMIT = 1200
MAX_HITS = 10
TIMEOUT = 25
INDEX_BUDGET = 32
VECTOR_BUDGET = 5000
RESPONSE_LIMIT = 128 * 1024
COVERAGE = ("index_complete", "semantic_index_complete", "limited_coverage", "vector_budget_exhausted", "query_limited")


class SearchSession:
    """Keep at most one native worker for the currently searched destination."""

    def __init__(self):
        self.process = None
        self.destination = None

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()

    def close(self):
        if self.process is None:
            return
        process, self.process = self.process, None
        self.destination = None
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        finally:
            process.stdout.close()

    def connect(self, executable, binding, head, model, python):
        destination = (executable, binding["project"], binding["lab"], head, model, python)
        if self.process is not None and self.destination == destination and self.process.poll() is None:
            return
        self.close()
        env = dict(os.environ)
        for name, value in (("NAOME_SEARCH_MODEL", model), ("NAOME_SEARCH_PYTHON", python)):
            env.pop(name, None)
            if value:
                env[name] = value
        try:
            # A plugin reinstall can remove the server's original working directory.
            self.process = subprocess.Popen(
                [executable, "--directory", binding["lab"], "--json", "archive", "search-session"],
                cwd=binding["lab"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, start_new_session=True, env=env,
            )
        except OSError as error:
            raise DeliveryError("The local NAOME search executable is unavailable.") from error
        self.destination = destination

    def find(self, head, scope, query, mode, limit, deadline, *, prepare=False):
        request = encode({"op": "find", "head": head, "scope": scope, "query": query,
                          "mode": mode, "limit": limit, "index_budget": INDEX_BUDGET,
                          "vector_budget": VECTOR_BUDGET, "prepare": prepare}).encode() + b"\n"
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError
            if self.process.stdin.write(request) != len(request):
                raise OSError("Incomplete native search request.")
            raw = bytearray()
            while not raw.endswith(b"\n"):
                remaining = max(0, deadline - time.monotonic())
                if not select.select([self.process.stdout], [], [], remaining)[0]:
                    raise TimeoutError
                chunk = os.read(self.process.stdout.fileno(), min(8192, RESPONSE_LIMIT + 1 - len(raw)))
                if not chunk or len(raw) + len(chunk) > RESPONSE_LIMIT:
                    raise ValueError("Invalid native search response length.")
                raw.extend(chunk)
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise ValueError("Invalid native search response.")
            if isinstance(response.get("error"), str):
                raise DeliveryError("NAOME could not search the approved snapshot with the selected local runtime.")
            report = response["ok"]
            if (not isinstance(report, dict) or report.get("head") != head or report.get("mode") != mode
                    or not isinstance(report.get("hits"), list) or len(report["hits"]) > limit
                    or any(key not in report for key in COVERAGE)):
                raise ValueError("Unexpected native search report.")
            return report
        except TimeoutError as error:
            self.close()
            raise DeliveryError("The local NAOME search timed out.") from error
        except (OSError, ValueError, KeyError) as error:
            self.close()
            raise DeliveryError("The local NAOME search session did not return a valid result.") from error


def approved_head(bridge, binding):
    lab = bridge.configured_lab(binding)
    head = lab.get("search_head") if lab else None
    if not isinstance(head, str) or not ID.fullmatch(head):
        raise DeliveryError("The selected lab has no approved search snapshot. Configure its search_head.")
    return head


def matches_scope(hit, scope):
    return (isinstance(hit, dict) and hit.get("scope") == scope
            and isinstance(hit.get("id"), str) and ID.fullmatch(hit["id"]))


def search_research(bridge, binding, arguments, session):
    query, mode, limit = arguments.get("query"), arguments.get("mode", "auto"), arguments.get("limit", 5)
    if not (arguments.keys() <= {"binding_token", "query", "mode", "limit", "project", "lab"}
            and isinstance(query, str) and 0 < len(query.strip()) <= QUERY_LIMIT and "\0" not in query
            and mode in MODES and type(limit) is int and 1 <= limit <= MAX_HITS):
        raise ValueError("Invalid research search arguments.")
    head = approved_head(bridge, binding)
    bridge.require_lab(binding["lab"])
    scope = binding["project"]
    runtime = bridge.config.get("search", {})
    if not isinstance(runtime, dict):
        raise ValueError("Configure the local search runtime as an object.")
    model, python = runtime.get("model"), runtime.get("python")
    if any(value is not None and (not isinstance(value, str) or not Path(value).is_absolute())
           for value in (model, python)):
        raise ValueError("Search model and Python paths must be absolute.")
    query = query.strip()
    if ID.fullmatch(query):
        mode = "lexical"
    elif mode == "auto":
        mode = "hybrid" if model else "lexical"
    if mode != "lexical" and not model:
        raise DeliveryError("Configure a local search.model to run semantic or hybrid search.")
    deadline = time.monotonic() + TIMEOUT
    session.connect(bridge.naome, binding, head, model, python)
    report = session.find(head, scope, query, mode, limit, deadline, prepare=mode != "lexical")

    candidates = [hit for hit in report["hits"] if matches_scope(hit, scope)]
    verified = {}
    hits = []
    for hit in candidates:
        links = []
        candidate_links = hit.get("links")
        for link in candidate_links[:8] if isinstance(candidate_links, list) else ():
            if not isinstance(link, dict) or not isinstance(link.get("role"), str):
                continue
            target = link.get("target")
            if not isinstance(target, str) or not ID.fullmatch(target):
                continue
            if target not in verified:
                exact = session.find(head, scope, target, "lexical", 1, deadline)
                verified[target] = any(
                    matches_scope(match, scope) and match["id"] == target and match.get("exact_id") is True
                    for match in exact["hits"]
                )
            if verified[target]:
                links.append({"role": link["role"], "target": target})
        hits.append({"record_id": hit["id"], "links": links, **{
            key: hit[key] for key in ("kind", "subject", "snippet", "location", "exact_id",
                                      "lexical_rank", "semantic_rank", "semantic_similarity") if key in hit
        }})

    current = Bridge()
    authorized = current.authorized(arguments["binding_token"])
    try:
        selected = current.read_destination(authorized, arguments) if authorized else None
    except ValueError:
        selected = None
    if not selected or (selected["project"], selected["lab"]) != (scope, binding["lab"]):
        session.close()
        raise DeliveryError("Research search authorization changed while the query was running.")
    current_lab = current.configured_lab(selected)
    if not current_lab or current_lab.get("search_head") != head:
        session.close()
        raise DeliveryError("The approved search snapshot changed while the query was running.")
    return {"project": selected["project_name"], "lab": binding["lab_name"], "head": head, "mode": mode, "hits": hits,
            **{key: report[key] for key in COVERAGE}}
