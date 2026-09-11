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

DEFAULT_MAX_BODY_CHARS = 12000

CLASSIFY_SYSTEM_PROMPT = (
    "You are an assistant that reads only the subject line of an email and decides whether it is "
    "an invoice or bill (this includes notifications that a bill/invoice is ready to view, e.g. "
    "'Your bill from X is now available', 'Invoice #1234 from Y', 'Payment due'). "
    'Respond with ONLY a JSON object, no other text, matching this schema: {"is_invoice": bool}'
)

EXTRACT_SYSTEM_PROMPT = (
    "You are an assistant that reads an email already confirmed to be an invoice or bill, and "
    "extracts its structured details. Respond with ONLY a JSON object, no other text, matching "
    "this schema:\n"
    '{"vendor": string|null, "amount": number|null, "currency": string|null, '
    '"invoice_number": string|null, "invoice_date": string|null, "due_date": string|null}\n'
    "Dates must be in YYYY-MM-DD format if present. Pay close attention to correctly identifying "
    "the total amount due, even if it is labeled as 'Total Balance' or similar rather than 'Amount'."
)

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {"is_invoice": {"type": "boolean"}},
    "required": ["is_invoice"],
}

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": {"type": "string"},
        "amount": {"type": "number"},
        "currency": {"type": "string"},
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string"},
        "due_date": {"type": "string"},
    },
    "required": [],
}


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
        self.max_body_chars: int = DEFAULT_MAX_BODY_CHARS

    def extract(self, emails: list[dict]) -> list[InvoiceData]:
        """Entry point: loads config and the model once, then runs each email
        through extraction, forwarding only genuine invoices to the sheet."""
        self.read_environment_variables()
        self.register_api()

        results = []
        for i, email in enumerate(emails):
            print(f"Processing email {i}!")
            invoice_data = self.extract_invoice_amount(email)
            if invoice_data.is_invoice:
                print("Invoice found! Adding row to google sheet")
                self.output_to_google_sheet(invoice_data)
                results.append(invoice_data)
        return results

    def read_environment_variables(self) -> None:
        """Still required even though the LLM itself no longer needs an API key,
        since Sheets output depends on these being set."""
        load_dotenv()
        self.google_sheet_id = os.environ["GOOGLE_SHEET_ID"]
        self.google_service_account_path = os.environ["GOOGLE_SERVICE_ACCOUNT_PATH"]
        self.max_body_chars = int(os.environ.get("MAX_BODY_CHARS", DEFAULT_MAX_BODY_CHARS))

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

        self.llm = Llama(model_path=str(model_path), n_ctx=8192, verbose=False)
        self.sheets_client = GoogleSheetsClient(self.google_sheet_id, self.google_service_account_path)
        self.sheets_client.ensure_header_row()

    def extract_invoice_amount(self, email: dict, verbose: bool = False) -> InvoiceData:
        """Two-stage detection: first classify from the subject line alone (cheap,
        and free of the marketing/tracking-link noise that buries the signal in
        many bill bodies), then only run the heavier full-body extraction call
        for emails that pass that gate."""
        
        if not self._classify_from_subject(email, verbose=verbose):
            return InvoiceData(is_invoice=False)

        return self._extract_details(email, verbose=verbose)

    def _classify_from_subject(self, email: dict, verbose: bool = False) -> bool:
        """Stage 1: subject-only yes/no gate."""

        # Check for llm existence
        if self.llm is None:
            raise Exception("LLM is not defined")
        
        response = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": f"Subject: {email.get('subject', '')}"},
            ],
            temperature=0,
            max_tokens=50,
            stream=False,
            response_format={"type": "json_object", "schema": CLASSIFY_SCHEMA},
        )

        assert isinstance(response, dict)
        raw_output = response["choices"][0]["message"]["content"]
        if verbose:
            print(f"raw classify output: {raw_output!r}")
        if raw_output is None:
            return False

        try:
            start = raw_output.index("{")
            end = raw_output.rindex("}") + 1
            data = json.loads(raw_output[start:end])
        except (ValueError, json.JSONDecodeError):
            return False

        return bool(data.get("is_invoice", False))

    def _extract_details(self, email: dict, verbose: bool = False) -> InvoiceData:
        """Stage 2: given an email already classified as an invoice/bill, pulls
        out the structured fields (vendor, amount, dates, etc.) from the full body."""

        # Check for llm existence
        if self.llm is None:
            raise Exception("LLM is not defined")

        response = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": self._build_prompt(email)},
            ],
            temperature=0,
            max_tokens=300,
            stream=False,
            response_format={"type": "json_object", "schema": EXTRACT_SCHEMA},
        )

        # stream=False guarantees a single dict, not the Iterator variant of the return union
        assert isinstance(response, dict)
        raw_output = response["choices"][0]["message"]["content"]
        if verbose:
            print(f"raw extract output: {raw_output!r}")
        if raw_output is None:
            return InvoiceData(is_invoice=True)
        return self._parse_extract_response(raw_output)

    def _build_prompt(self, email: dict) -> str:
        """Truncates the body defensively so one oversized email (long footer,
        quoted thread, etc.) can't push the prompt past the model's context window."""
        body = email.get("body", "")[: self.max_body_chars]
        return f"Subject: {email.get('subject', '')}\n\nBody:\n{body}"

    @staticmethod
    def _parse_extract_response(raw_output: str) -> InvoiceData:
        """Falls back to a bare is_invoice=True on any malformed output rather than
        raising, so one bad email doesn't kill extraction for the rest of the batch --
        the classify stage has already established this email is an invoice."""
        try:
            start = raw_output.index("{")
            end = raw_output.rindex("}") + 1
            data = json.loads(raw_output[start:end])
        except (ValueError, json.JSONDecodeError):
            return InvoiceData(is_invoice=True)

        return InvoiceData(
            is_invoice=True,
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
