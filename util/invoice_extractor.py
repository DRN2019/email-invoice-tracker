import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from huggingface_hub import hf_hub_download
from llama_cpp import Llama

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
    def __init__(self):
        self.google_sheet_id: str | None = None
        self.google_service_account_path: str | None = None

        self.llm: Llama | None = None
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
        self.google_sheet_id = os.environ["GOOGLE_SHEET_ID"]
        self.google_service_account_path = os.environ["GOOGLE_SERVICE_ACCOUNT_PATH"]

    def register_api(self) -> None:
        model_path = MODEL_DIR / MODEL_FILE
        if not model_path.exists():
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILE, local_dir=MODEL_DIR)

        self.llm = Llama(model_path=str(model_path), n_ctx=4096, verbose=False)
        # self.sheets_client = build("sheets", "v4", credentials=Credentials.from_service_account_file(self.google_service_account_path))

    def extract_invoice_amount(self, email: dict) -> InvoiceData:
        response = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self._build_prompt(email)},
            ],
            temperature=0,
            max_tokens=300,
        )
        raw_output = response["choices"][0]["message"]["content"]
        return self._parse_response(raw_output)

    @staticmethod
    def _build_prompt(email: dict) -> str:
        return f"Subject: {email.get('subject', '')}\n\nBody:\n{email.get('body', '')}"

    @staticmethod
    def _parse_response(raw_output: str) -> InvoiceData:
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
        raise NotImplementedError
