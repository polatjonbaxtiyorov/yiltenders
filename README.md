# Tender fetcher for Telegram bots

This mini-project wraps the [`apisitender.mc.uz`](https://apisitender.mc.uz/api/tenders) endpoint and
applies two business rules:

1. When the API request explicitly targets region `10` or `14`, return only those tenders whose
  customer name contains the substring ``YO`LLAR`` (case-insensitive).
2. When no `region_id` query parameter is used, return *only* tenders whose customer has `id = 374`.

The CLI now sends **three** back-to-back requests (region 10, region 14, and a nationwide pull)
by default, merging the results while removing duplicates. Use `--single-request` if you need to
inspect only one payload for debugging.

Each tender is reduced to the exact fields you requested, making it easy to plug the
output into a Telegram bot.

## Project layout

- `tender_service.py` – core client and formatter. Provides `TenderService.fetch_and_filter()` and `format_for_telegram()` helpers.
- `tests/fixtures/sample_response.json` – trimmed response you can reuse in tests.
- `tests/test_tender_service.py` – unit coverage for the filtering and formatting logic.
- `bot_runner.py` – minimal Telegram loop that fetches every 6 hours, skips previously sent tenders,
  and broadcasts fresh ones to your configured chats.
- `tests/test_bot_runner.py` – store + notification tests.
- `authorized_chats.json` – created automatically by `bot_runner.py` to remember which chat IDs
  have successfully provided the password (default `12052006yil`).

## Quick start

```powershell
# Install dependencies (preferably inside a virtual environment)
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

# Run the default multi-request workflow (regions 10 & 14 + nationwide filtered list)
python tender_service.py

# Run a single-request debug session for region 10
python tender_service.py --single-request --region-id 10

# Inspect the raw API payload + detailed logs
python tender_service.py --show-response --debug

# Fetch nationwide tenders (only customer 374 will remain) and return raw JSON data
python tender_service.py --dump-json

# Start the Telegram loop (runs forever, fetching every 6 hours).
# Chats must send the password (default: 12052006yil) to the bot once to subscribe.
# After that they get two buttons inside Telegram:
#   • 📦 Barcha tenderlar  — sends every stored tender again.
#   • ⚡️ Yangi tenderlarni yuborish — forces an immediate refresh cycle.
set TELEGRAM_BOT_TOKEN=123:ABC   # optional: defaults to the provided token if unset
set TENDER_BOT_PASSWORD=12052006yil   # optional override
python bot_runner.py --debug

# Run a single cycle (cron style) and custom interval
python bot_runner.py --once --interval-hours 1
```

## Using inside a Telegram bot

```python
from tender_service import TenderService, format_for_telegram

service = TenderService()
tenders = service.fetch_and_filter(region_id=10)  # or None
messages = format_for_telegram(tenders)

for chat_id in subscribed_users:
    for text in messages:
        bot.send_message(chat_id=chat_id, text=text)
```

## Running tests

```powershell
pytest
```

## Notes

- `TenderService` is built on top of `requests.Session` so you can inject your own
  session (with proxies, retries, etc.).
- The module raises a `ValueError` if anyone requests a region outside `{10, 14}` to
  catch mistakes early.
- Use `--debug` to enable INFO/DEBUG logs (request params, status codes, item counts)
  and `--show-response` to dump the raw JSON straight from the server (all three requests).
- Pass `--single-request` together with `--region-id` if you want the old behavior
  of issuing just one API call.
- `bot_runner.py` persists sent tender IDs in `sent_tenders.json` (configurable) so each news
  blast contains only fresh tenders even if the script restarts.
- `bot_runner.py` also stores authorized chat IDs (and update offsets) in `authorized_chats.json`.
  Anyone who sends the password (default `12052006yil`) to the bot is added automatically, and you
  can seed IDs with `--chat-ids` or the `TELEGRAM_CHAT_IDS` env variable if needed.
- All output fields (name, unique name, start price, required percent, placement term,
  complexity category, work days, customer name, address) are always present in
  the dictionaries returned by `TenderSummary.to_dict()`.

## Telegram bot UX

### New Features (Fixed Issues):
- **Continuous Polling**: Bot now runs continuously, responding immediately to new users and button clicks
- **Inline Keyboard Support**: Interactive buttons that work properly with callback queries
- **Improved /start Command**: New users can use `/start` to initiate bot interaction
- **Better User Experience**: Immediate responses to all user interactions

### Bot Interaction Flow:
- **New Users**: Send `/start` or any message to begin. Bot prompts with `🔐 Iltimos, parolni kiriting.`
- **Password Authentication**: Send the correct password (default: `12052006yil`) to register
- **Successful Registration**: Replies with `✅ Siz muvaffaqiyatli obuna bo'ldingiz.` and shows interactive buttons
- **Interactive Buttons**:
  - `📦 Barcha tenderlar` - View all stored tenders from `sent_tenders.json`
  - `⚡️ Yangi tenderlarni yuborish` - Force immediate tender refresh cycle
- **Continuous Operation**: Bot polls for updates every few seconds while running scheduled tender fetches based on `--interval-hours`

### Technical Improvements:
- **Long Polling**: Uses 10-second timeout for responsive message handling
- **Callback Query Support**: Handles button clicks properly with `answerCallbackQuery` 
- **Webhook Cleanup**: Automatically clears any existing webhooks on startup
- **Error Recovery**: Handles connection errors gracefully with automatic retry
- **Dual Keyboard Support**: Uses inline keyboards for better UX with fallback support

## Google Sheets Integration

### Overview
The bot now supports automatic logging of tender data to Google Sheets for enhanced tracking, analysis, and data management.

### Features
- **Automatic Data Logging**: New tenders are automatically added to your Google Sheet
- **Duplicate Prevention**: Prevents duplicate entries using tender IDs
- **Real-time Updates**: Sheet is updated immediately when new tenders are found
- **Status Monitoring**: Check Google Sheets status via bot commands
- **Structured Data**: Clean, organized spreadsheet with proper headers

### Setup Instructions

#### 1. Google Cloud Console Setup
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing one
3. Enable the following APIs:
   - **Google Sheets API**
   - **Google Drive API**

#### 2. Service Account Creation
1. Navigate to **IAM & Admin > Service Accounts**
2. Click **"Create Service Account"**
3. Fill in service account details and create
4. Click on the created service account
5. Go to **"Keys"** tab → **"Add Key"** → **"Create new key"**
6. Choose **JSON format** and download the file
7. Rename downloaded file to `google_credentials.json`
8. Place it in the same directory as `bot_runner.py`

#### 3. Google Sheets Setup
1. Create a new Google Sheet or use existing one
2. Copy the spreadsheet ID from the URL:
   ```
   https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
   ```
3. **Share the sheet** with your service account email (from step 2)
4. Give **"Editor"** permissions to the service account

#### 4. Environment Configuration
```bash
# Set environment variable
export GOOGLE_SPREADSHEET_ID='your_spreadsheet_id_here'

# Or create .env file
echo "GOOGLE_SPREADSHEET_ID=your_spreadsheet_id_here" > .env
```

#### 5. Quick Setup Script
```bash
# Run the automated setup helper
python setup_sheets.py --create-sample
```

### Running with Google Sheets

```bash
# Enable Google Sheets integration
python bot_runner.py --enable-sheets --debug

# Custom credentials file location
python bot_runner.py --enable-sheets --google-credentials /path/to/creds.json

# Custom spreadsheet ID
python bot_runner.py --enable-sheets --spreadsheet-id "your_sheet_id"

# Custom worksheet name (default: "Tenders")
python bot_runner.py --enable-sheets --worksheet-name "MyTenders"
```

### Testing Google Sheets Connection
```bash
# Test connection without running bot
python google_sheets_service.py --test --spreadsheet-id YOUR_SHEET_ID

# Create sample credentials file
python google_sheets_service.py --create-sample
```

### Bot Commands with Sheets
When Google Sheets is enabled, users get an additional button:
- **📊 Google Sheets holati** - Shows integration status and tender count

### Data Structure
The Google Sheet will contain these columns:
- **Tender ID**: Unique tender identifier
- **Name**: Project/tender name
- **Unique Name**: Alternative identifier
- **Start Price**: Initial tender price
- **Required Percent**: Required percentage
- **Placement Term**: Submission deadline
- **Complexity Category**: Complexity classification
- **Work Days**: Duration in days
- **Customer Name**: Client organization
- **Address**: Project location
- **Date Added**: When record was created

### Troubleshooting

#### Common Issues:
1. **"Credentials file not found"**
   - Ensure `google_credentials.json` exists in bot directory
   - Check file permissions

2. **"Spreadsheet not found"**
   - Verify spreadsheet ID is correct
   - Ensure sheet is shared with service account email

3. **"Permission denied"**
   - Service account needs "Editor" access to the sheet
   - Re-share the sheet with proper permissions

4. **"API not enabled"**
   - Enable Google Sheets API and Google Drive API in Cloud Console

#### Debug Commands:
```bash
# Verbose Google Sheets logging
python bot_runner.py --enable-sheets --debug

# Test specific spreadsheet
python google_sheets_service.py --test --spreadsheet-id "your_id"
```
