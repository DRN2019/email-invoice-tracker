import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from huggingface_hub import hf_hub_download
from llama_cpp import Llama

from .sheets_client import GoogleSheetsClient

MODEL_REPO = "Qwen/Qwen2.5-3B-Instruct-GGUF"
MODEL_FILE = "qwen2.5-3b-instruct-q4_k_m.gguf"
MODEL_DIR = Path(__file__).resolve().parent.parent / "models"

SYSTEM_PROMPT = (
    "You are an assistant that reads emails and identifies whether they are an invoice or bill. "
    "Respond with ONLY a JSON object, no other text, matching this schema:\n"
    '{"is_invoice": bool, "vendor": string|null, "amount": number|null, "currency": string|null, '
    '"invoice_number": string|null, "invoice_date": string|null, "due_date": string|null}\n'
    "Dates must be in YYYY-MM-DD format if present. "
    'If the email is not an invoice or bill, respond with {"is_invoice": false}.'
)


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
    """Reads emails through a local GGUF model to detect and structure invoice
    data, then forwards genuine invoices on to Google Sheets."""

    def __init__(self):
        """Fields start empty and are populated by read_environment_variables/
        register_api, since extract() drives the whole setup sequence itself."""
        self.google_sheet_id: str | None = None
        self.google_service_account_path: str | None = None

        self.llm: Llama | None = None
        self.sheets_client = None

    def extract(self, emails: list[dict]) -> list[InvoiceData]:
        """Entry point: loads config and the model once, then runs each email
        through extraction, forwarding only genuine invoices to the sheet."""
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
        """Still required even though the LLM itself no longer needs an API key,
        since Sheets output depends on these being set."""
        load_dotenv()
        self.google_sheet_id = os.environ["GOOGLE_SHEET_ID"]
        self.google_service_account_path = os.environ["GOOGLE_SERVICE_ACCOUNT_PATH"]

    def register_api(self) -> None:
        """Downloads the GGUF weights into models/ only on first run, then loads
        them into memory once so extract_invoice_amount can reuse self.llm per email."""
        # Check if variables were initialized correctly
        if self.google_sheet_id is None or self.google_service_account_path is None:
            raise Exception("Invalid google sheet id or service account path!")
        
        model_path = MODEL_DIR / MODEL_FILE
        if not model_path.exists():
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILE, local_dir=MODEL_DIR)

        self.llm = Llama(model_path=str(model_path), n_ctx=4096, verbose=False)
        self.sheets_client = GoogleSheetsClient(self.google_sheet_id, self.google_service_account_path)

    def extract_invoice_amount(self, email: dict) -> InvoiceData:
        """Prompts the local model with a response_format schema so its output
        is grammar-constrained to match InvoiceData's shape."""
        # Check for llm existence
        if self.llm is None:
            raise Exception("LLM is not defined")
        
        response = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self._build_prompt(email)},
            ],
            temperature=0,
            max_tokens=300,
            response_format={
                "type": "json_object",
                "schema": {
                    "type": "object",
                    "properties": {
                        "is_invoice": {"type": "boolean"},
                        "vendor": {"type": "string"},
                        "amount": {"type": "number"},
                        "currency": {"type": "string"},
                        "invoice_number": {"type": "string"},
                        "invoice_date": {"type": "string"},
                        "due_date": {"type": "string"},
                    },
                    "required": ["is_invoice"],
                },
            },
        )

        for message, i in iter(response):
            print(f"Message {i}: ")
            print(message + "\n")

        return InvoiceData(is_invoice = False)
        # raw_output = response["choices"][0]["message"]["content"]
        # return self._parse_response(raw_output)

    @staticmethod
    def _build_prompt(email: dict) -> str:
        """Feeds subject + body verbatim; the plain-text body is expected to already
        come from the source (e.g. Graph's Prefer header), not raw HTML."""
        return f"Subject: {email.get('subject', '')}\n\nBody:\n{email.get('body', '')}"

    @staticmethod
    def _parse_response(raw_output: str) -> InvoiceData:
        """Falls back to is_invoice=False on any malformed output rather than
        raising, so one bad email doesn't kill extraction for the rest of the batch."""
        try:
            start = raw_output.index("{")
            end = raw_output.rindex("}") + 1
            data = json.loads(raw_output[start:end])
        except (ValueError, json.JSONDecodeError):
            return InvoiceData(is_invoice=False)

        return InvoiceData(
            is_invoice=bool(data.get("is_invoice", False)),
            vendor=data.get("vendor"),
            amount=data.get("amount"),
            currency=data.get("currency"),
            invoice_number=data.get("invoice_number"),
            invoice_date=data.get("invoice_date"),
            due_date=data.get("due_date"),
        )

    def output_to_google_sheet(self, invoice_data: InvoiceData) -> None:
        """Appends one row per invoice, plus when the tracker recorded it (separate
        from the invoice's own date) so you can tell when it was actually caught."""
        if self.sheets_client is None:
            raise Exception("Sheets client is uninitialized!")

        row = [
            invoice_data.vendor,
            invoice_data.amount,
            invoice_data.currency,
            invoice_data.invoice_number,
            invoice_data.invoice_date,
            invoice_data.due_date,
            datetime.now().isoformat(timespec="seconds"),
        ]
        self.sheets_client.append_row(row)
