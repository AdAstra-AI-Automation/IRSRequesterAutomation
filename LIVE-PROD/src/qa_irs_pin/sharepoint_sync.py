"""Append new sites through Excel's range API, never by replacing the workbook."""

from __future__ import annotations

import logging
import os
import re
from threading import Lock
from typing import Any
from urllib.parse import quote

import requests

from . import sharepoint_lookup as lookup

logger = logging.getLogger(__name__)
_APPEND_LOCK = Lock()
_COLUMNS = ("site name", "te id", "state")


def _credentials() -> tuple[str, ...]:
    return tuple(os.getenv(name, "").strip() for name in (
        "GRAPH_TENANT", "GRAPH_CLIENT_ID", "GRAPH_USERNAME", "GRAPH_PASSWORD",
    ))


def _cell_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _sheet_teid(value: Any) -> str:
    """Compare Excel numeric and zero-padded text TEIDs without losing identity."""
    text = _cell_text(value)
    if re.fullmatch(r"[0-9]{1,4}(?:\.0+)?", text):
        return str(int(float(text))).zfill(4)
    return ""


def _column_name(index: int) -> str:
    name = ""
    while index >= 0:
        index, remainder = divmod(index, 26)
        name = chr(65 + remainder) + name
        index -= 1
    return name


def _request(session: requests.Session, method: str, url: str, **kwargs: Any) -> dict:
    # Do not retry writes: a timeout may mean Excel already saved the row.
    response = session.request(method, url, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json()


def _load_sheet(session: requests.Session) -> tuple[str, dict, dict[str, int]]:
    item = lookup.find_workbook_item(session)
    drive = quote(item["parentReference"]["driveId"], safe="")
    item_id = quote(item["id"], safe="")
    sheet = quote(lookup._SHEET_NAME, safe="")
    sheet_url = f"https://graph.microsoft.com/v1.0/drives/{drive}/items/{item_id}/workbook/worksheets/{sheet}"
    data = _request(session, "GET", f"{sheet_url}/usedRange(valuesOnly=true)")
    rows = data.get("values") or []
    if not rows or len(rows) != data.get("rowCount"):
        raise ValueError("Master sheet values are missing or incomplete; no cells were changed.")
    headers = [_cell_text(value).lower() for value in rows[0]]
    if any(headers.count(name) != 1 for name in _COLUMNS):
        raise ValueError("Master sheet requires unique Site Name, TE ID, and State headers; no cells were changed.")
    return sheet_url, data, {name: headers.index(name) for name in _COLUMNS}


def _session(credentials: tuple[str, ...]) -> requests.Session:
    token = lookup._get_token(*credentials)
    if not token:
        raise ValueError("SharePoint authentication failed; check Graph credentials and delegated permissions.")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    return session


def _error_message(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"Microsoft Graph returned HTTP {exc.response.status_code}."
    # Do not expose response bodies, credentials, tokens, or sharing links.
    return f"SharePoint request failed ({type(exc).__name__})."


def check_access() -> dict[str, Any]:
    """Read-only deployment check. Success does not prove that a write was saved."""
    credentials = _credentials()
    if not all(credentials):
        return {"status": "Failed", "message": "Graph credentials are incomplete."}
    try:
        with _session(credentials) as session:
            _, data, _ = _load_sheet(session)
        return {
            "status": "Accessible",
            "workbook": lookup._WORKBOOK_NAME,
            "sheet": lookup._SHEET_NAME,
            "rows_including_header": data["rowCount"],
            "message": "Workbook and required columns read successfully; no cells changed.",
        }
    except Exception as exc:
        return {"status": "Failed", "message": _error_message(exc)}


def append_new_site(site_name: str, teid: str, state: str = "") -> dict[str, str]:
    """Append only when neither the site name nor TEID exists in the sheet.

    Calls are serialized within this API process. Excel range writes are not
    transactions against other coauthors: do not run multiple app instances or
    manually append to this sheet at the same time as automated sync.
    """
    result = {"status": "Failed", "message": "", "site_name": site_name, "teid": teid}
    if os.getenv("GRAPH_MASTER_SHEET_SYNC", "true").strip().lower() not in {"1", "true", "yes"}:
        return {**result, "status": "Disabled", "message": "SharePoint new-site sync is disabled."}
    if not site_name.strip() or not re.fullmatch(r"[0-9]{4}", teid):
        return {**result, "message": "SharePoint sync requires a site name and a four-digit TEID."}
    credentials = _credentials()
    if not all(credentials):
        return {**result, "message": "SharePoint sync unavailable: Graph credentials are incomplete."}

    try:
        with _APPEND_LOCK, _session(credentials) as session:
            sheet_url, data, columns = _load_sheet(session)
            site_key = lookup._normalize_site_key(site_name)
            for row in data["values"][1:]:
                existing_name = row[columns["site name"]] if len(row) > columns["site name"] else ""
                existing_teid = row[columns["te id"]] if len(row) > columns["te id"] else ""
                if lookup._normalize_site_key(_cell_text(existing_name)) == site_key or _sheet_teid(existing_teid) == teid:
                    return {
                        **result,
                        "status": "Skipped",
                        "message": (
                            f"SharePoint entry not added: site name or TEID {teid} already exists in "
                            f"{lookup._WORKBOOK_NAME} ({lookup._SHEET_NAME}). "
                            f"Please review '{site_name}' against the existing entry and, if appropriate, "
                            "safely add it to the SharePoint sheet manually. Connect creation is unaffected."
                        ),
                    }

            next_row = data["rowIndex"] + data["rowCount"] + 1
            if next_row > 1048576:
                raise ValueError("Master sheet has reached Excel's row limit; no cells were changed.")
            offset = data["columnIndex"]
            first_col = offset + min(columns.values())
            last_col = offset + max(columns.values())
            address = f"{_column_name(first_col)}{next_row}:{_column_name(last_col)}{next_row}"
            range_url = f"{sheet_url}/range(address='{address}')"
            width = last_col - first_col + 1

            # Recheck immediately before the write. Never fill a hole in existing data.
            target = _request(session, "GET", range_url)
            for field in ("values", "formulas"):
                cells = target.get(field)
                if not isinstance(cells, list) or len(cells) != 1 or len(cells[0]) != width:
                    raise ValueError("SharePoint append position could not be checked; no cells were changed.")
                if any(_cell_text(cell) for cell in cells[0]):
                    raise ValueError("SharePoint append position is no longer empty; no cells were changed. Retry sync after other edits finish.")

            values: list[Any] = [None] * width
            formats: list[Any] = [None] * width
            # The Connect payload's default Florida value is not evidence of state.
            # Only infer a state from an explicit address suffix if none was supplied.
            if not state:
                match = re.search(r"\b([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?(?:,\s*(?:USA|US|United States))?\s*$", site_name)
                if match and match[1] in lookup._US_STATE_ABBREVS:
                    state = match[1]
            for name, value in zip(_COLUMNS, (site_name.strip(), teid, state.strip())):
                index = offset + columns[name] - first_col
                values[index] = value
                formats[index] = "@"  # Keep leading zeros and literal text.
            _request(session, "PATCH", range_url, json={"values": [values], "numberFormat": [formats]})
            lookup._CACHE.clear()
            saved = _request(session, "GET", range_url).get("values") or []
            if not saved or any(
                index >= len(saved[0]) or _cell_text(saved[0][index]) != value
                for index, value in enumerate(values) if value is not None
            ):
                raise ValueError("SharePoint write was sent but could not be verified. Check the sheet before retrying.")
        return {**result, "status": "Appended", "message": f"New site appended to SharePoint (TEID {teid})."}
    except Exception as exc:
        message = _error_message(exc)
        logger.warning("SharePoint new-site sync: %s", message)
        return {**result, "message": message}
