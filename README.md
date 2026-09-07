# email-invoice-tracker

Scans your inbox for invoice/bill emails and logs the details (vendor, amount, due date, etc.)
to a Google Sheet — automatically, once a day, with no third-party AI API required.

**How it works:**
1. `util/graph_client.py` pulls recent emails from a personal Outlook/Microsoft mailbox via the Microsoft Graph API.
2. `util/invoice_extractor.py` feeds each email to a small language model (Qwen2.5-3B-Instruct)
   running **locally** on your machine to detect invoices and extract structured data — no
   Claude/OpenAI/etc. API key needed.
3. `util/sheets_client.py` appends each detected invoice as a row in a Google Sheet.

`main.py` ties all three together and is what you actually run.

## Requirements

- **Python 3.12.** (Not 3.14 — as of this writing, `llama-cpp-python` doesn't publish prebuilt
  Windows wheels for 3.14, which forces a from-source build that needs a C++ compiler and can fail
  on Windows due to path-length limits. 3.12 has prebuilt wheels and just works.)
- A **personal Microsoft account** (outlook.com / hotmail.com / live.com) — the mailbox to read.
- A **Google account** — to hold the output spreadsheet.
- ~2GB free disk space for the local model (downloaded once, on first run).

## Setup

### 1. Clone the repo and create a virtual environment

```bash
git clone <this-repo-url>
cd email-invoice-tracker
py -3.12 -m venv .venv
.venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

`requirements.txt` already points `llama-cpp-python` at a prebuilt-wheel index
(`abetlen.github.io/llama-cpp-python/whl/cpu`), so this should install without needing a
compiler.

### 3. Set up Microsoft Graph access (to read your email)

1. Go to [portal.azure.com](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Name it anything (e.g. `email-invoice-tracker`). Under **Supported account types**, choose
   **"Personal Microsoft accounts only."**
3. After it's created, copy the **Application (client) ID** shown on the overview page.
4. Go to **Authentication** → **Advanced settings** → set **"Allow public client flows"** to **Yes** → Save.
5. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions** → add **`Mail.Read`**.

No client secret is needed — this app authenticates via an interactive one-time device-code login
(see "First run" below), not a stored secret.

### 4. Set up Google Sheets access (to log invoices)

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create/select a project, then
   enable the **Google Sheets API** under **APIs & Services → Library**.
2. Go to **APIs & Services → Credentials → Create Credentials → Service Account**. Give it any name.
3. Open the new service account → **Keys** → **Add Key → Create new key → JSON**. This downloads a
   `.json` key file — save it somewhere on disk (see the security note below about keeping it out
   of git).
4. Open that JSON file and copy the `client_email` value.
5. Create a Google Sheet (or use an existing one) and rename one of its tabs to exactly **`Invoices`**.
6. Click **Share** on the sheet and share it with the `client_email` address from step 4, as an **Editor**.
7. Copy the spreadsheet ID from its URL: `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`.

### 5. Configure environment variables

Copy `.env.example` to `.env` and fill in the values gathered above:

```
GOOGLE_SHEET_ID=<spreadsheet id from step 4.7>
GOOGLE_SERVICE_ACCOUNT_PATH=<path to the json key file from step 4.3>
AZURE_CLIENT_ID=<application client id from step 3.3>
SENDERS=
```

`SENDERS` is optional: a comma-separated allowlist of sender email addresses to restrict which
emails get scanned (e.g. `billing@vendor.com, invoices@saas.com`). Leave it blank to scan all
recent emails.

If you keep the Google service account JSON key file inside this project folder, add its filename
to `.gitignore` before committing anything — it's a credential, not source code.

### 6. Run it

```bash
python main.py
```

**First run:** two things happen that only happen once:
- The local model (~2GB) downloads into `models/`.
- You'll be prompted with a URL and code to approve a one-time sign-in for the Microsoft Graph
  connection (device code flow). After that, a `token_cache.bin` file is saved so future runs
  don't need you to log in again.

Subsequent runs are unattended — schedule `python main.py` via Task Scheduler (Windows) or cron
(Linux/Raspberry Pi) to run once a day.

## Notes

- **Raspberry Pi:** this is designed to run comfortably on a Pi (4GB+ RAM) since it uses a
  quantized GGUF model via `llama-cpp-python`, which has solid ARM support. Speed isn't a concern
  for a once-a-day job.
- **Lookback window:** `main.py` only fetches emails from the last 24 hours by default
  (`LOOKBACK_HOURS` in `main.py`), so re-running it daily won't re-log the same invoices.
