import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from util.graph_client import GraphEmailClient
from util.invoice_extractor import InvoiceExtractor

LOOKBACK_HOURS = 1000


def main() -> None:
    load_dotenv()
    client_id = os.environ["AZURE_CLIENT_ID"]

    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    graph_client = GraphEmailClient(client_id)
    emails = graph_client.fetch_recent_emails(since_iso=since_iso)
    print(f"Fetched {len(emails)} email(s) from the last {LOOKBACK_HOURS} hours.")

    extractor = InvoiceExtractor()
    invoices = extractor.extract(emails)
    print(f"Found {len(invoices)} invoice(s); rows appended to the sheet.")


if __name__ == "__main__":
    main()
