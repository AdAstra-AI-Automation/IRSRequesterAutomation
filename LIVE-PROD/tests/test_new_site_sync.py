"""Exercise real parsing/processing/API formatting with all external I/O mocked."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import app as api
from qa_irs_pin import processor, sharepoint_lookup
from qa_irs_pin.parser import parse_input_records
from utils.client import ConnectMutationResult, ConnectQAClient

OLD_SITE = "Original site, NY, USA"
NEW_SITE = "New department, Albany, NY, USA"


class NewSiteSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "audit.db"
        # sqlite's context manager commits but does not close. Track the existing
        # registry connections so temporary test databases are removable on Windows.
        connections = []
        get_connection = processor.registry.get_connection

        def tracked_connection(*args, **kwargs):
            connection = get_connection(*args, **kwargs)
            connections.append(connection)
            return connection

        connection_patch = patch.object(processor.registry, "get_connection", side_effect=tracked_connection)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)
        def close_connections():
            for connection in connections:
                connection.close()

        self.addCleanup(close_connections)
        self.client = Mock(spec=ConnectQAClient)
        self.client.get_sites_for_customer.return_value = [OLD_SITE]
        self.client.resolve_teid.return_value = {"siteExists": False, "currentMaxTeid": "0001"}
        self.client.get_pin_context.return_value = {"fK_Customer": 1219, "fK_Location": 20, "maxPinCode": None}
        self.client.pin_context_with_site_name_for_teid.return_value = (OLD_SITE, {
            "fK_Customer": 1219, "fK_Location": 20, "maxPinCode": "000112345",
        })
        self.client.search_user_by_seid.return_value = []
        self.client.create_user.return_value = ConnectMutationResult(True, "new-guid", "Created", {"text": "Created"})
        self.client.deactivate_user.return_value = ConnectMutationResult(True, "old-guid", "Deactivated", {})
        self.client.update_user.return_value = ConnectMutationResult(True, "old-guid", "Updated", {})
        self.client.get_account_detail.return_value = {
            "code": "old-guid", "fK_Customer": 1219, "pinCode": 112345,
            "pinCodeString": "000112345", "address": OLD_SITE,
        }
        self.sync_patch = patch.object(processor.sharepoint_sync, "append_new_site", return_value={
            "status": "Appended", "message": "Site appended", "teid": "0002",
        })
        self.sync = self.sync_patch.start()
        self.addCleanup(self.sync_patch.stop)
        lookup_patch = patch.object(sharepoint_lookup, "_get_tables", return_value=({}, {}))
        lookup_patch.start()
        self.addCleanup(lookup_patch.stop)
        network_patch = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected HTTP request"))
        network_patch.start()
        self.addCleanup(network_patch.stop)

    def row(self, **overrides):
        row = {"BOD": "TAS", "First Name": "Test", "Last Name": "Operator",
               "SEID": "TEST01", "Site Name": NEW_SITE, "Contact Status": "Add"}
        row.update(overrides)
        return row

    def run_rows(self, *rows):
        return processor.process_rows(parse_input_records(list(rows)), client=self.client,
                                      db_path=self.db, write_output=False)

    def modify_row(self, **overrides):
        row = self.row(**{"Contact Status": "Modify-Function Change", "Site Name": OLD_SITE,
                          "Site ID": "0001", "Current User PIN": "000112345",
                          "Employee ID": "12345", "New Site": NEW_SITE})
        row.update(overrides)
        self.client.search_user_by_seid.return_value = [{
            "account_status": "active", "pin_code": "000112345", "teid": "0001",
            "connect_guid": "old-guid", "raw": {"address": OLD_SITE},
        }]
        return row

    def test_successful_new_site_add_syncs_after_connect_create(self):
        def appended(*args):
            self.client.create_user.assert_called_once()
            return {"status": "Appended", "message": "Site appended"}
        self.sync.side_effect = appended
        result = self.run_rows(self.row())
        self.assertEqual(result.row_results[0].status, "Created")
        self.sync.assert_called_once_with(NEW_SITE, "0002")
        self.assertEqual(result.row_results[0].sharepoint_sync["status"], "Appended")
        self.assertIn("Site appended", api._build_result_message(result.row_results[0].to_dict()))

    def test_failed_add_does_not_sync(self):
        self.client.create_user.return_value = ConnectMutationResult(False, None, "Create failed", {})
        result = self.run_rows(self.row())
        self.assertEqual(result.row_results[0].status, "Failed")
        self.sync.assert_not_called()

    def test_existing_site_add_does_not_sync(self):
        result = self.run_rows(self.row(**{"Site Name": OLD_SITE, "Site ID": "0001"}))
        self.assertEqual(result.row_results[0].status, "Created")
        self.sync.assert_not_called()

    def test_new_site_with_explicit_teid_and_manual_name_syncs(self):
        result = self.run_rows(self.row(**{"Site ID": "0002", "Manual Site Name": NEW_SITE}))
        self.assertEqual(result.row_results[0].status, "Created")
        self.sync.assert_called_once_with(NEW_SITE, "0002")

    def test_repeated_new_site_rows_sync_only_once(self):
        result = self.run_rows(self.row(), self.row(SEID="TEST02"))
        self.assertEqual([row.status for row in result.row_results], ["Created", "Created"])
        self.sync.assert_called_once_with(NEW_SITE, "0002")

    def test_failed_first_create_does_not_suppress_later_success(self):
        self.client.create_user.side_effect = [ConnectMutationResult(False, None, "Failed", {}),
                                               ConnectMutationResult(True, "new-guid", "Created", {})]
        result = self.run_rows(self.row(), self.row(SEID="TEST02"))
        self.assertEqual([row.status for row in result.row_results], ["Failed", "Created"])
        self.sync.assert_called_once_with(NEW_SITE, "0002")

    def test_failed_sync_preserves_connect_success_and_surfaces_warning(self):
        self.sync.return_value = {"status": "Failed", "message": "Graph HTTP 403"}
        result = self.run_rows(self.row())
        row = result.row_results[0]
        self.assertEqual(row.status, "Created")
        self.assertEqual(result.summary["Created"], 1)
        response = api._format_rows_response([self.row()], result.to_dict(), debug=False)
        output = response["results"][0]["result"]
        self.assertEqual(output["status"], "Created")
        self.assertIn("SharePoint sync needs attention", output["message"])
        self.assertEqual(output["sharepoint_sync"]["status"], "Failed")

    def test_unexpected_sync_exception_cannot_fail_connect(self):
        self.sync.side_effect = RuntimeError("Test unexpected error")
        result = self.run_rows(self.row())
        self.assertEqual(result.row_results[0].status, "Created")
        self.assertEqual(result.row_results[0].sharepoint_sync["status"], "Failed")

    def test_duplicate_sheet_entry_preserves_connect_success_and_end_message(self):
        self.sync.return_value = {"status": "Skipped", "message": "Site or TEID already exists in IRS Site Listing updated.xlsx. Please review and safely add manually if appropriate."}
        result = self.run_rows(self.row())
        self.assertEqual(result.row_results[0].status, "Created")
        output = api._format_rows_response([self.row()], result.to_dict(), debug=False)["results"][0]["result"]
        self.assertEqual(output["status"], "Created")
        self.assertEqual(output["sharepoint_sync"]["status"], "Skipped")
        self.assertIn("Please review", output["message"])

    def test_successful_modify_syncs_only_destination_after_create(self):
        result = self.run_rows(self.modify_row())
        self.assertEqual(result.row_results[0].status, "Created")
        self.client.deactivate_user.assert_called_once()
        self.sync.assert_called_once_with(NEW_SITE, "0002")
        self.assertEqual(result.row_results[0].sharepoint_sync["status"], "Appended")

    def test_modify_with_explicit_destination_teid_syncs(self):
        result = self.run_rows(self.modify_row(**{"New Site:Site ID": "0002"}))
        self.assertEqual(result.row_results[0].status, "Created")
        self.sync.assert_called_once_with(NEW_SITE, "0002")

    def test_failed_modify_create_does_not_sync(self):
        row = self.modify_row()
        self.client.create_user.return_value = ConnectMutationResult(False, None, "Failed", {})
        result = self.run_rows(row)
        self.assertEqual(result.row_results[0].status, "Manual Intervention Required")
        self.sync.assert_not_called()

    def test_failed_modify_deactivation_does_not_create_or_sync(self):
        row = self.modify_row()
        self.client.deactivate_user.return_value = ConnectMutationResult(False, None, "Failed", {})
        result = self.run_rows(row)
        self.assertEqual(result.row_results[0].status, "Failed")
        self.client.create_user.assert_not_called()
        self.sync.assert_not_called()

    def test_cross_account_modify_uses_destination_site_and_customer(self):
        result = self.run_rows(self.modify_row(**{"New BOD": "SPEC"}))
        self.assertEqual(result.row_results[0].status, "Created")
        self.assertIn("SPEC", result.row_results[0].customer_name)
        self.sync.assert_called_once_with(NEW_SITE, "0002")

    def test_demo_account_never_adds_test_sites_to_irs_sheet(self):
        result = self.run_rows(self.row(BOD="Z-DEMO"))
        self.assertEqual(result.row_results[0].status, "Created")
        self.sync.assert_not_called()

    def test_deactivate_and_activate_never_sync(self):
        self.modify_row()  # Provide one existing active requester.
        for action, expected in (("Deactivate", "Deactivated"), ("Activate", "Activated")):
            result = self.run_rows(self.row(**{"Contact Status": action}))
            self.assertEqual(result.row_results[0].status, expected)
        self.sync.assert_not_called()

    def test_already_exists_and_invalid_rows_never_sync(self):
        self.modify_row()
        result = self.run_rows(self.row(**{"Site Name": OLD_SITE, "Site ID": "0001"}))
        self.assertEqual(result.row_results[0].status, "Already Exists")
        result = self.run_rows(self.row(SEID=""))
        self.assertEqual(result.row_results[0].status, "Error")
        self.sync.assert_not_called()

    def test_review_does_not_create_or_sync(self):
        api.app.state.client = self.client
        result = api._review_rows([api.InputRowRequest.model_validate(self.row())], debug=False)
        self.assertEqual(result["results"][0]["status"], "Reviewed")
        self.client.create_user.assert_not_called()
        self.sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
