"""Presentation of confirmed research records and authorized search hits over MCP Apps."""

import base64
from functools import lru_cache
from pathlib import Path

from bridge import ID

URI = "ui://naome/research-card-v3.html"
LEGACY_URIS = ("ui://naome/research-card-v1.html", "ui://naome/research-card-v2.html")
MIME_TYPE = "text/html;profile=mcp-app"
DETAILS_KEY = "naome/research-block"
RECENT_DETAILS_KEY = "naome/research-details"
RESOURCE = {"uri": URI, "name": "Naome research block", "mimeType": MIME_TYPE}
TOOL_META = {"ui": {"resourceUri": URI}, "openai/ui": {"preferredModelDisplayMode": "inline"}}
ASSETS = Path(__file__).resolve().parent / "assets"


class ResourceNotFound(ValueError):
    """The server does not provide the requested resource URI."""


@lru_cache(maxsize=1)
def card_html():
    html = (ASSETS / "research-card.html").read_text(encoding="utf-8")
    css = (ASSETS / "research-card.css").read_text(encoding="utf-8")
    logo = base64.b64encode((ASSETS / "naome-card-logo.png").read_bytes()).decode("ascii")
    return html.replace("__NAOME_CARD_STYLES__", css).replace("__NAOME_CARD_LOGO__", f"data:image/png;base64,{logo}")


def read_resource(uri):
    if uri != URI and uri not in LEGACY_URIS:
        raise ResourceNotFound("Unknown Naome resource.")
    return {"contents": [{**RESOURCE, "uri": uri, "text": card_html(), "_meta": {
        "ui": {
            "prefersBorder": False,
            "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": []},
        },
        "openai/ui": {"availableDisplayModes": ["inline"]},
        "openai/widgetShowCodexWidgetInline": True,
        "openai/widgetMinFrameHeight": 0,
        "openai/widgetHeightHint": 104,
    }}]}


def card_data(receipt, category, title, fields, references):
    record_id = receipt["local_record"]
    if not isinstance(record_id, str) or not ID.fullmatch(record_id):
        return {}
    summary = fields["summary"]
    return {
        "structuredContent": {"record_id": record_id, "title": title, "category": category,
                              "project": receipt["project"], "lab": receipt["lab"], "summary": summary_preview(summary)},
        "_meta": {DETAILS_KEY: {"record_id": record_id, "summary": summary, "sources": fields["sources"],
                               "linked_records": references}},
    }


def recent_card_data(receipts):
    records = []
    details = {}
    for receipt in receipts:
        record = receipt.copy()
        summary = record.pop("summary")
        detail = {"sources": record.pop("sources"), "linked_records": record.pop("linked_records")}
        if isinstance(summary, str) and summary.strip():
            record["preview"] = summary_preview(summary)
            detail["summary"] = summary
        if any(detail.values()):
            details[record["record_id"]] = detail
        records.append(record)
    return records, details


def summary_preview(summary):
    return summary if len(summary) <= 160 else summary[:159].rstrip() + "…"
