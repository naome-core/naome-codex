"""Resolve a user prompt to one configured lab without inferring permission from mentions."""

import re
from pathlib import Path

NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}"
CURRENT = re.compile(
    rf"(?:/lab\s+({NAME})|für diesen schritt bitte labor\s+({NAME})\s+verwenden"
    rf"|use lab\s+({NAME})\s+for this turn)[.!]?", re.IGNORECASE,
)
PERSISTENT = re.compile(
    rf"(?:/lab\s+default\s+({NAME})|ab jetzt labor\s+({NAME})(?:\s+verwenden)?"
    rf"|use lab\s+({NAME})\s+from now on)[.!]?", re.IGNORECASE,
)
DIRECTIVE = re.compile(r"^(?:/lab\b|für diesen schritt bitte labor\b|ab jetzt labor\b|use lab\b)", re.I)
MENTION = re.compile(
    rf"\blab(?:ore?|s)?\s+({NAME}(?:(?:\s+(?:and|or|und|oder)\s+|\s*[,/]\s*)(?:lab(?:or)?\s+)?{NAME})*)",
    re.IGNORECASE,
)


def configured_labs(project):
    labs = project.get("labs")
    if labs is None:
        labs = {"default": {"directory": project["lab"], "job": project.get("job")}}
    if not isinstance(labs, dict) or not labs:
        raise ValueError("Configure at least one lab for the NAOME project.")
    resolved = {}
    for name, settings in labs.items():
        if not re.fullmatch(NAME, name) or name.casefold() in resolved:
            raise ValueError("NAOME lab names must be distinct letters, numbers, dashes or underscores.")
        path = Path(settings["directory"])
        if not path.is_absolute():
            raise ValueError("Lab directories must be absolute.")
        resolved[name.casefold()] = {"name": name, "path": str(path.resolve()), "job": settings.get("job")}
    if len({lab["path"] for lab in resolved.values()}) != len(resolved):
        raise ValueError("Configure each lab directory only once in a project.")
    default = project.get("default_lab")
    if default is not None and (not isinstance(default, str) or default.casefold() not in resolved):
        raise ValueError("The project's default must name a configured NAOME lab.")
    return resolved, default.casefold() if default else next(iter(resolved)) if len(resolved) == 1 else None


def choose_lab(prompt, labs, default):
    lines = prompt.strip().splitlines()
    first = lines[0].strip() if lines else ""
    if DIRECTIVE.match(first):
        if len(lines) > 1 and DIRECTIVE.match(lines[1].strip()):
            return None, False, "This turn contains more than one lab selection."
        persistent = PERSISTENT.fullmatch(first)
        match = persistent or CURRENT.fullmatch(first)
        if not match:
            return None, False, "The lab selection is incomplete or ambiguous."
        name = next(group for group in match.groups() if group).casefold()
        if name not in labs:
            return None, False, "The selected lab is not configured for this project."
        return labs[name], persistent is not None, None

    mentions = {
        name.casefold() for match in MENTION.finditer(prompt)
        for name in re.findall(NAME, match[1]) if name.casefold() in labs
    }
    if default is None or mentions - {default}:
        return None, False, "This turn has no unambiguous destination lab."
    return labs[default], False, None
