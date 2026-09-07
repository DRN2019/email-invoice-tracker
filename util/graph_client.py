import os
from pathlib import Path

import msal
import requests
from dotenv import load_dotenv

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Mail.Read"]
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
TOKEN_CACHE_PATH = Path(__file__).resolve().parent.parent / "token_cache.bin"
SENDERS_ENV_VAR = "SENDERS"


class GraphEmailClient:
    """Reads a personal Outlook mailbox via Microsoft Graph using MSAL's public-client
    device-code flow, since personal accounts can't use unattended app-only permissions."""

    def __init__(self, client_id: str):
        load_dotenv()
        self.client_id = client_id
        self.token_cache = self._load_token_cache()
        self.app = msal.PublicClientApplication(
            client_id=self.client_id,
            authority=AUTHORITY,
            token_cache=self.token_cache,
        )

    def fetch_recent_emails(self, since_iso: str | None = None, limit: int = 50) -> list[dict]:
        """Returns emails newest-first as {subject, body} dicts, ready for InvoiceExtractor.

        Requests plain-text bodies via the Prefer header so the LLM prompt isn't fed raw HTML."""
        access_token = self._get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Prefer": 'outlook.body-content-type="text"',
        }
        params = {
            "$orderby": "receivedDateTime desc",
            "$top": min(limit, 50),
        }

        filters = []
        if since_iso:
            filters.append(f"receivedDateTime ge {since_iso}")

        # Checks to see if there's a predefined list of senders to pull emails from; defaults to all emails otherwise
        senders = self._get_senders()
        if senders:
            sender_clauses = " or ".join(f"from/emailAddress/address eq '{sender}'" for sender in senders)
            filters.append(f"({sender_clauses})")
        if filters:
            params["$filter"] = " and ".join(filters)

        emails = []
        url = GRAPH_MESSAGES_URL
        while url and len(emails) < limit:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            payload = response.json()

            for message in payload.get("value", []):
                emails.append(
                    {
                        "subject": message.get("subject", ""),
                        "body": message.get("body", {}).get("content", ""),
                    }
                )
                if len(emails) >= limit:
                    break

            url = payload.get("@odata.nextLink")
            params = None  # nextLink already carries the query params

        return emails

    @staticmethod
    def _get_senders() -> list[str]:
        """Reads SENDERS as a comma-separated allowlist; empty/unset means no
        sender filter, so fetch_recent_emails falls back to retrieving all emails."""
        raw = os.environ.get(SENDERS_ENV_VAR, "")
        return [sender.strip() for sender in raw.split(",") if sender.strip()]

    def _get_access_token(self) -> str:
        """Tries a silent refresh first; only falls back to the interactive device-code
        login when the cache has no usable account (e.g. the very first run)."""
        accounts = self.app.get_accounts()
        if accounts:
            result = self.app.acquire_token_silent(SCOPES, account=accounts[0])
            if result and "access_token" in result:
                self._save_token_cache()
                return result["access_token"]

        flow = self.app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Failed to start device flow: {flow.get('error_description', flow)}")

        print(flow["message"])
        result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(f"Failed to acquire token: {result.get('error_description', result)}")

        self._save_token_cache()
        return result["access_token"]

    def _load_token_cache(self) -> msal.SerializableTokenCache:
        """MSAL's in-memory cache alone wouldn't survive between separate script runs,
        so this restores whatever was persisted last time (empty on the first-ever run)."""
        cache = msal.SerializableTokenCache()
        if TOKEN_CACHE_PATH.exists():
            cache.deserialize(TOKEN_CACHE_PATH.read_text())
        return cache

    def _save_token_cache(self) -> None:
        """Only writes when MSAL flags the cache as changed, since acquire_token_silent
        can rotate the underlying tokens even without a fresh login."""
        if self.token_cache.has_state_changed:
            TOKEN_CACHE_PATH.write_text(self.token_cache.serialize())
