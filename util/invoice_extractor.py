import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from huggingface_hub import hf_hub_download
from llama_cpp import Llama

from .sheets_client import GoogleSheetsClient

logger = logging.getLogger(__name__)

MODEL_REPO = "Qwen/Qwen2.5-3B-Instruct-GGUF"
MODEL_FILE = "qwen2.5-3b-instruct-q4_k_m.gguf"
MODEL_DIR = Path(__file__).resolve().parent.parent / "models"

DEFAULT_MAX_BODY_CHARS = 12000
DEFAULT_VENDOR_OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "vendor_overrides.json"

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
    "Dates must be in YYYY/MM/DD format if present. Pay close attention to correctly identifying "
    "the total amount due, even if it is labeled as 'Total Balance' or similar rather than 'Amount'.\n\n"
    "'vendor' and 'invoice_number' are easy to confuse -- read their definitions carefully:\n"
    "- vendor: the name of the company or organization that issued the bill (e.g. 'Comcast', "
    "'Verizon Wireless', 'Acme Cloud Hosting'). This is always a business name made of words, "
    "never a number or a code.\n"
    "- invoice_number: the invoice/order/account/confirmation identifier, usually labeled 'Invoice #', "
    "'Order Number', 'Account Number', or similar. This is typically a short alphanumeric code, not "
    "a business name.\n"
    "These two fields must never have the same value, and invoice_number must never be copied into "
    "vendor. If you cannot confidently identify the vendor's business name in the email, set "
    "'vendor' to null rather than guessing with the invoice number, an account number, or any other "
    "code."
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
    sender: str | None = None
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
        self.vendor_overrides_path: Path = DEFAULT_VENDOR_OVERRIDES_PATH
        self.vendor_overrides: dict[str, str] = {}

    def extract(self, emails: list[dict]) -> list[InvoiceData]:
        """Entry point: loads config and the model once, then runs each email
        through extraction, forwarding only genuine invoices to the sheet."""
        self.read_environment_variables()
        self.register_api()

        results = []
        for i, email in enumerate(emails):
            logger.debug("Processing email %d: %s", i, email.get("subject", "(no subject)"))
            invoice_data = self.extract_invoice_amount(email)
            if invoice_data.is_invoice:
                invoice_data.sender = email.get("sender", "")
                self._resolve_vendor(invoice_data)
                logger.info("Invoice found in email %d: vendor=%s amount=%s sender=%s", i, invoice_data.vendor, invoice_data.amount, invoice_data.sender)
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
        self.vendor_overrides_path = Path(
            os.environ.get("VENDOR_OVERRIDES_PATH", DEFAULT_VENDOR_OVERRIDES_PATH)
        )
        self.vendor_overrides = self._load_vendor_overrides()

    def _load_vendor_overrides(self) -> dict[str, str]:
        """Reads the domain -> vendor name table used to backstop the LLM's vendor
        guesses. A missing file just means no overrides have been added yet."""
        if not self.vendor_overrides_path.exists():
            return {}

        with open(self.vendor_overrides_path, encoding="utf-8") as f:
            raw: dict[str, Any] = json.load(f)

        return {
            domain.strip().lower(): name
            for domain, name in raw.items()
            if not domain.startswith("_") and isinstance(name, str)
        }

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
            logger.debug("raw classify output: %r", raw_output)
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
            logger.debug("raw extract output: %r", raw_output)
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

    def _resolve_vendor(self, invoice_data: InvoiceData) -> None:
        """Backstops the LLM's vendor guess: fills it in from the sender's domain
        when the model left it null, and overwrites it when it looks like the
        invoice number (or some other code) got copied into the vendor field."""
        vendor = invoice_data.vendor
        vendor_is_suspect = (
            not vendor
            or self._looks_like_code(vendor)
            or (invoice_data.invoice_number is not None and vendor == invoice_data.invoice_number)
        )
        if not vendor_is_suspect:
            return

        fallback = self._vendor_from_sender(invoice_data.sender)
        if fallback:
            invoice_data.vendor = fallback

    def _vendor_from_sender(self, sender: str | None) -> str | None:
        """Maps a sender email address to a vendor name: checks the override table
        against progressively shorter domain suffixes (so an override for
        'comcast.com' also matches a sender at 'billing.comcast.com'), then falls
        back to a title-cased guess from the domain's second-level label."""
        if not sender or "@" not in sender:
            return None

        domain = sender.rsplit("@", 1)[-1].strip().lower()
        if not domain:
            return None

        labels = domain.split(".")
        for i in range(len(labels) - 1):
            candidate = ".".join(labels[i:])
            if candidate in self.vendor_overrides:
                return self.vendor_overrides[candidate]

        label = labels[-2] if len(labels) >= 2 else labels[0]
        return label.replace("-", " ").replace("_", " ").title()

    @staticmethod
    def _looks_like_code(value: str) -> bool:
        """Heuristic guard against the model copying an invoice/account number into
        the vendor field: real vendor names are words, not digit-heavy tokens with
        no spaces."""
        stripped = value.strip()
        if not stripped or " " in stripped:
            return False
        digit_ratio = sum(c.isdigit() for c in stripped) / len(stripped)
        return digit_ratio >= 0.4

    def output_to_google_sheet(self, invoice_data: InvoiceData) -> None:
        """Appends one row per invoice, plus when the tracker recorded it (separate
        from the invoice's own date) so you can tell when it was actually caught."""
        if self.sheets_client is None:
            raise Exception("Sheets client is uninitialized!")

        row = [
            invoice_data.vendor,
            invoice_data.sender,
            invoice_data.amount,
            invoice_data.currency,
            invoice_data.invoice_number,
            invoice_data.invoice_date,
            invoice_data.due_date,
            datetime.now().isoformat(timespec="seconds"),
        ]
        self.sheets_client.append_row(row)
