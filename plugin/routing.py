"""Validate enrolled lab names and resolve explicit lab arguments."""

import re
from pathlib import Path

NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}"


def configured_labs(project):
    labs = project.get("labs")
    if labs is None:
        labs = {"default": {"directory": project["lab"], "job": project.get("job"),
                            "search_head": project.get("search_head")}}
    if not isinstance(labs, dict) or not labs:
        raise ValueError("Configure at least one lab for the NAOME project.")
    resolved = {}
    for name, settings in labs.items():
        if not re.fullmatch(NAME, name) or name.casefold() in resolved:
            raise ValueError("NAOME lab names must be distinct letters, numbers, dashes or underscores.")
        path = Path(settings["directory"])
        if not path.is_absolute():
            raise ValueError("Lab directories must be absolute.")
        resolved[name.casefold()] = {"name": name, "path": str(path.resolve()), "job": settings.get("job"),
                                     "search_head": settings.get("search_head")}
    if len({lab["path"] for lab in resolved.values()}) != len(resolved):
        raise ValueError("Configure each lab directory only once in a project.")
    default = project.get("default_lab")
    if default is not None and (not isinstance(default, str) or default.casefold() not in resolved):
        raise ValueError("default_lab must name a configured NAOME lab.")
    return resolved, default.casefold() if default else next(iter(resolved)) if len(resolved) == 1 else None


def named_lab(labs, name):
    if not isinstance(name, str) or name.casefold() not in labs:
        raise ValueError("The specified lab is not enrolled in this project.")
    return labs[name.casefold()]
