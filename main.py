import argparse
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from util.graph_client import GraphEmailClient
from util.invoice_extractor import InvoiceExtractor
from util.logger import setup_logger

LOOKBACK_HOURS = 24


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", help="Log raw LLM output for each email")
    parser.add_argument(
        "--hours", type=float, default=LOOKBACK_HOURS, help=f"How many hours back to check for emails (default: {LOOKBACK_HOURS})"
    )
    args = parser.parse_args()

    logger = setup_logger()
    load_dotenv()
    client_id = os.environ["AZURE_CLIENT_ID"]

    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    # TODO: Retrieve list of email senders to be added to the list. Sender will be a column added later
    graph_client = GraphEmailClient(client_id)
    emails = graph_client.fetch_recent_emails(since_iso=since_iso)
    logger.info("Fetched %d email(s) from the last %s hours.", len(emails), args.hours)

    extractor = InvoiceExtractor()
    invoices = extractor.extract(emails, verbose=args.verbose)
    logger.info("Found %d invoice(s); rows appended to the sheet.", len(invoices))


if __name__ == "__main__":
    main()
