"""Offline contract tests for append-only Excel writes."""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from qa_irs_pin import sharepoint_lookup as lookup, sharepoint_sync as sync


class FakeGraphSession:
    def __init__(self, rows=None, *, column_index=0, row_index=0):
        self.rows = deepcopy(rows if rows is not None else [
            ["Site Name", "TE ID", "State", "Unrelated"],
            ["Existing site", "0001", "NY", "Keep this"],
        ])
        self.original_rows = deepcopy(self.rows)
        self.column_index = column_index
        self.row_index = row_index
        self.headers = {}
        self.writes = []
        self.target = {"values": [["", "", ""]], "formulas": [["", "", ""]]}
        self.write_error = None
        self.bad_readback = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def request(self, method, url, **kwargs):
        response = Mock()
        if "usedRange" in url:
            data = {"values": self.rows, "rowIndex": self.row_index,
                    "rowCount": len(self.rows), "columnIndex": self.column_index}
        elif method == "PATCH":
            self.writes.append((url, kwargs["json"]))
            if self.write_error:
                raise self.write_error
            self.target = {"values": deepcopy(kwargs["json"]["values"])}
            data = self.target
        else:
            data = {"values": [["wrong"]]} if self.writes and self.bad_readback else self.target
        response.json.return_value = deepcopy(data)
        return response


class SharePointAppendTests(unittest.TestCase):
    def setUp(self):
        self.graph = FakeGraphSession()
        self.env = patch.dict(os.environ, {
            "GRAPH_TENANT": "test-tenant", "GRAPH_CLIENT_ID": "test-client",
            "GRAPH_USERNAME": "test-user", "GRAPH_PASSWORD": "test-only",
            "GRAPH_MASTER_SHEET_SYNC": "true",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.session_patch = patch.object(sync, "_session", side_effect=lambda _: self.graph)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.item_patch = patch.object(lookup, "find_workbook_item", return_value={
            "id": "test-item", "parentReference": {"driveId": "test-drive"},
        })
        self.item_patch.start()
        self.addCleanup(self.item_patch.stop)

    def test_append_only_at_end_preserves_existing_rows_and_leading_zeros(self):
        lookup._CACHE["test"] = ({}, {})
        result = sync.append_new_site("New site, Albany, NY, USA", "0002")
        self.assertEqual(result["status"], "Appended")
        self.assertEqual(self.graph.rows, self.graph.original_rows)
        url, body = self.graph.writes[0]
        self.assertTrue(url.endswith("range(address='A3:C3')"))
        self.assertEqual(body, {"values": [["New site, Albany, NY, USA", "0002", "NY"]],
                                "numberFormat": [["@", "@", "@"]]})
        self.assertEqual(lookup._CACHE, {})

    def test_existing_site_is_never_updated_even_with_blank_or_different_teid(self):
        for teid in ("", "9999"):
            self.graph.rows[1][1] = teid
            result = sync.append_new_site("  EXISTING   site  ", "0002", "CA")
            self.assertEqual(result["status"], "Skipped")
            self.assertEqual(self.graph.writes, [])

    def test_distinct_site_with_shared_teid_is_skipped_with_review_message(self):
        for stored_teid in ("0001", "1", 1, 1.0):
            self.graph.rows[1][1] = stored_teid
            result = sync.append_new_site("Another location", "0001")
            self.assertEqual(result["status"], "Skipped")
            self.assertIn("Please review", result["message"])
            self.assertIn(lookup._WORKBOOK_NAME, result["message"])
            self.assertIn("Connect creation is unaffected", result["message"])
            self.assertEqual(self.graph.writes, [])

    def test_unknown_state_is_blank(self):
        sync.append_new_site("IRS New Department", "0002")
        self.assertEqual(self.graph.writes[0][1]["values"][0][2], "")

    def test_explicit_state_and_postal_suffix(self):
        for name, state, expected in (("New site", "CA", "CA"),
                                      ("City, NJ 07102", "", "NJ")):
            self.graph = FakeGraphSession()
            sync.append_new_site(name, "0002", state)
            self.assertEqual(self.graph.writes[0][1]["values"][0][2], expected)

    def test_reordered_columns_and_offset_preserve_intervening_cells(self):
        self.graph = FakeGraphSession([
            ["State", "Other", "Site Name", "TE ID"],
            ["NY", "Retain", "Old site", "0001"],
        ], column_index=2, row_index=4)
        self.graph.target = {"values": [["", "", "", ""]], "formulas": [["", "", "", ""]]}
        self.assertEqual(sync.append_new_site("New site", "0002", "CA")["status"], "Appended")
        url, body = self.graph.writes[0]
        self.assertTrue(url.endswith("range(address='C7:F7')"))
        self.assertEqual(body["values"], [["CA", None, "New site", "0002"]])
        self.assertEqual(body["numberFormat"], [["@", None, "@", "@"]])

    def test_missing_or_duplicate_headers_prevent_writes(self):
        for headers in (["Site Name", "State"], ["Site Name", "TE ID", "State", "State"]):
            self.graph = FakeGraphSession([headers])
            self.assertEqual(sync.append_new_site("New site", "0002")["status"], "Failed")
            self.assertEqual(self.graph.writes, [])

    def test_occupied_target_or_blank_looking_formula_prevents_write(self):
        for target in ({"values": [["Another editor's entry", "", ""]], "formulas": [["", "", ""]]},
                       {"values": [["", "", ""]], "formulas": [['=""', "", ""]]}):
            self.graph.target = target
            self.assertEqual(sync.append_new_site("New site", "0002")["status"], "Failed")
            self.assertEqual(self.graph.writes, [])

    def test_timeout_is_not_retried(self):
        self.graph.write_error = requests.Timeout()
        self.assertEqual(sync.append_new_site("New site", "0002")["status"], "Failed")
        self.assertEqual(len(self.graph.writes), 1)

    def test_incomplete_target_read_prevents_write(self):
        self.graph.target = {}
        self.assertEqual(sync.append_new_site("New site", "0002")["status"], "Failed")
        self.assertEqual(self.graph.writes, [])

    def test_readback_must_match(self):
        self.graph.bad_readback = True
        result = sync.append_new_site("New site", "0002")
        self.assertEqual(result["status"], "Failed")
        self.assertIn("could not be verified", result["message"])

    def test_invalid_teid_never_writes(self):
        for teid in ("", "1", "10000", "123456789", "ABCD"):
            self.assertEqual(sync.append_new_site("New site", teid)["status"], "Failed")
        self.assertEqual(self.graph.writes, [])

    def test_missing_credentials_and_disabled_flag_never_write(self):
        with patch.dict(os.environ, {"GRAPH_PASSWORD": ""}):
            self.assertEqual(sync.append_new_site("New site", "0002")["status"], "Failed")
            self.assertEqual(sync.check_access()["status"], "Failed")
        with patch.dict(os.environ, {"GRAPH_MASTER_SHEET_SYNC": "false"}):
            self.assertEqual(sync.append_new_site("New site", "0002")["status"], "Disabled")
        self.assertEqual(self.graph.writes, [])

    def test_http_error_is_safe_and_fail_open(self):
        response = Mock(status_code=403)
        self.graph.write_error = requests.HTTPError("sensitive response", response=response)
        result = sync.append_new_site("New site", "0002")
        self.assertEqual(result["status"], "Failed")
        self.assertIn("403", result["message"])
        self.assertNotIn("sensitive", result["message"])

    def test_access_check_is_read_only(self):
        self.assertEqual(sync.check_access()["status"], "Accessible")
        self.assertEqual(self.graph.writes, [])


class WorkbookDiscoveryTests(unittest.TestCase):
    def test_shared_teid_does_not_redirect_lookup_to_newest_site(self):
        workbook = MagicMock()
        workbook.sheetnames = [lookup._SHEET_NAME]
        workbook.__getitem__.return_value.iter_rows.return_value = iter([
            ("Site Name", "TE ID", "State"),
            ("Existing New York site", "0001", "NY"),
            ("New California site", "0001", "CA"),
            ("Unique site", "0002", "MD"),
        ])
        lookup._CACHE.clear()
        self.addCleanup(lookup._CACHE.clear)
        with patch.object(lookup, "_get_token", return_value="test-token"), \
             patch.object(lookup, "_download_workbook", return_value=b"test"), \
             patch("openpyxl.load_workbook", return_value=workbook):
            teids, sites = lookup._load_lookup_tables("tenant", "client", "user", "test-only")
        self.assertNotIn("0001", teids)
        self.assertEqual(teids["0002"]["site_name"], "Unique site")
        self.assertEqual(sites["existing new york site"]["teid"], "0001")
        self.assertEqual(sites["new california site"]["teid"], "0001")

    def test_exact_file_search_and_pagination(self):
        session = Mock()
        item = {"name": lookup._WORKBOOK_NAME, "id": "exact", "file": {}}
        first = Mock()
        first.json.return_value = {"value": [{"name": "Copy.xlsx", "file": {}}],
                                   "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"}
        second = Mock()
        second.json.return_value = {"value": [item]}
        site = Mock()
        site.json.return_value = {"id": "site"}
        session.get.side_effect = [site, first, second]
        self.assertEqual(lookup.find_workbook_item(session)["id"], "exact")

    def test_ambiguous_files_are_rejected(self):
        session = Mock()
        site = Mock()
        site.json.return_value = {"id": "site"}
        result = Mock()
        result.json.return_value = {"value": [{"name": lookup._WORKBOOK_NAME, "file": {}}] * 2}
        session.get.side_effect = [site, result]
        with self.assertRaisesRegex(ValueError, "found 2"):
            lookup.find_workbook_item(session)


if __name__ == "__main__":
    unittest.main()
