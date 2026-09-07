from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_RANGE = "Sheet1!A1"


class GoogleSheetsClient:
    """Appends rows to a Google Sheet using a service account, since this runs
    unattended with no user available to complete an interactive OAuth consent screen."""

    def __init__(self, spreadsheet_id: str, service_account_path: str):
        self.spreadsheet_id = spreadsheet_id
        credentials = Credentials.from_service_account_file(service_account_path, scopes=SCOPES)
        self.service = build("sheets", "v4", credentials=credentials)

    def append_row(self, row: list) -> None:
        """Sheets' append() locates the next empty row itself, so callers don't
        need to track or read back the current row count."""
        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=SHEET_RANGE,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
