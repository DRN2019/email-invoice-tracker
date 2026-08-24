import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class InvoiceData:
    is_invoice: bool
    vendor: str | None = None
    amount: float | None = None
    currency: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None


class InvoiceExtractor:
    def __init__(self):
        self.anthropic_api_key: str | None = None
        self.google_sheet_id: str | None = None
        self.google_service_account_path: str | None = None

        self.claude_client = None
        self.sheets_client = None

    def extract(self, emails: list[dict]) -> list[InvoiceData]:
        self.read_environment_variables()
        self.register_api()

        results = []
        for email in emails:
            invoice_data = self.extract_invoice_amount(email)
            if invoice_data.is_invoice:
                self.output_to_google_sheet(invoice_data)
                results.append(invoice_data)
        return results

    def read_environment_variables(self) -> None:
        load_dotenv()
        self.anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]
        self.google_sheet_id = os.environ["GOOGLE_SHEET_ID"]
        self.google_service_account_path = os.environ["GOOGLE_SERVICE_ACCOUNT_PATH"]

    def register_api(self) -> None:
        # self.claude_client = anthropic.Anthropic(api_key=self.anthropic_api_key)
        # self.sheets_client = build("sheets", "v4", credentials=Credentials.from_service_account_file(self.google_service_account_path))
        raise NotImplementedError

    def extract_invoice_amount(self, email: dict) -> InvoiceData:
        raise NotImplementedError

    def output_to_google_sheet(self, invoice_data: InvoiceData) -> None:
        raise NotImplementedError
