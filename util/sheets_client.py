from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_RANGE = "Sheet1!A1"
HEADER_RANGE = "Sheet1!A1:G1"

# Must match the column order output_to_google_sheet() appends in invoice_extractor.py.
HEADERS = ["Vendor", "Amount", "Currency", "Invoice Number", "Invoice Date", "Due Date", "Recorded At"]


class GoogleSheetsClient:
    """Appends rows to a Google Sheet using a service account, since this runs
    unattended with no user available to complete an interactive OAuth consent screen."""

    def __init__(self, spreadsheet_id: str, service_account_path: str):
        self.spreadsheet_id = spreadsheet_id
        credentials = Credentials.from_service_account_file(service_account_path, scopes=SCOPES)
        self.service = build("sheets", "v4", credentials=credentials)
        # Cached for ensure_header_row()'s insertDimension call, which needs the
        # sheet's numeric id rather than its name.
        metadata = self.service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        self.sheet_id = metadata["sheets"][0]["properties"]["sheetId"]

    def has_header_row(self) -> bool:
        """True only if row 1 already holds the exact expected header labels,
        as opposed to invoice data that happens to occupy row 1."""
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id, range=HEADER_RANGE
        ).execute()
        values = result.get("values", [])
        return bool(values) and values[0] == HEADERS

    def ensure_header_row(self) -> None:
        """Inserts the header row at the top if it's missing, pushing any
        existing data down rather than overwriting it. Safe to call every run."""
        if self.has_header_row():
            return

        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "requests": [
                    {
                        "insertDimension": {
                            "range": {
                                "sheetId": self.sheet_id,
                                "dimension": "ROWS",
                                "startIndex": 0,
                                "endIndex": 1,
                            },
                            "inheritFromBefore": False,
                        }
                    }
                ]
            },
        ).execute()
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=HEADER_RANGE,
            valueInputOption="USER_ENTERED",
            body={"values": [HEADERS]},
        ).execute()

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
