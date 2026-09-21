import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from util.graph_client import GraphEmailClient
from util.invoice_extractor import InvoiceExtractor
from util.logger import setup_logger

LOOKBACK_HOURS = 24


def main() -> None:
    logger = setup_logger()
    load_dotenv()
    client_id = os.environ["AZURE_CLIENT_ID"]

    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    # TODO: Retrieve list of email senders to be added to the list. Sender will be a column added later
    graph_client = GraphEmailClient(client_id)
    emails = graph_client.fetch_recent_emails(since_iso=since_iso)
    logger.info("Fetched %d email(s) from the last %d hours.", len(emails), LOOKBACK_HOURS)

    extractor = InvoiceExtractor()
    invoices = extractor.extract(emails)
    logger.info("Found %d invoice(s); rows appended to the sheet.", len(invoices))


if __name__ == "__main__":
    main()
