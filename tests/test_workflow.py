import unittest
from pathlib import Path


class WorkflowDefinitionTests(unittest.TestCase):
    def test_monthly_workflow_has_server_side_safety_and_delivery_gates(self):
        workflow = (Path(__file__).parents[1] / ".github/workflows/monthly-vendor-audit.yml").read_text()
        for required in (
            "workflow_dispatch:",
            "schedule:",
            'cron: "0 13 1 * *"',
            'cron: "0 14 1 * *"',
            "concurrency:",
            "cancel-in-progress: false",
            "TZ=America/Chicago",
            "GITHUB_REF",
            "AIRTABLE_API_KEY",
            "GOOGLE_TOKEN_JSON",
            "SLACK_BOT_TOKEN",
            "SLACK_RECIPIENT_USER_ID",
            "if: always()",
            "steps.report.outcome != 'success'",
        ):
            self.assertIn(required, workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertNotIn("runner.temp", workflow)
        self.assertIn("$RUNNER_TEMP", workflow)


if __name__ == "__main__":
    unittest.main()
