"""Telegram bot runner that registers chats via password and sends new tenders."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

import requests

from tender_service import TenderService, TenderSummary, format_for_telegram
from google_sheets_service import GoogleSheetsService

DEFAULT_INTERVAL_HOURS = 3

DEFAULT_ACCESS_PASSWORD = "DeveloperAccess#123"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
PASSWORD_PROMPT = "🔐 Iltimos, parolni kiriting."

# Command constants
COMMAND_SENDALL = "/sendall"
COMMAND_REFRESH = "/refresh" 
COMMAND_STATUS = "/status"
COMMAND_HELP = "/help"


def get_help_message(has_sheets: bool = False) -> str:
    """Get help message with available commands"""
    msg = """📋 Mavjud buyruqlar:

/sendall - Barcha saqlangan tenderlarni ko'rish
/refresh - Yangi tenderlarni qidirish va yuborish
/help - Bu yordam xabarini ko'rsatish"""
    
    if has_sheets:
        msg += "\n/status - Google Sheets holati"
    
    return msg


class SentTenderStore:
    """Persist the identifiers of tenders already sent to Telegram."""

    def __init__(self, path: Path) -> None:
        self._path = path
        data = self._load()
        self._ids: Set[str] = data["ids"]
        self._records: dict[str, dict] = data["records"]

    def filter_new(self, summaries: Iterable[TenderSummary]) -> List[TenderSummary]:
        fresh: List[TenderSummary] = []
        for summary in summaries:
            key = self._summary_key(summary)
            if key not in self._ids:
                fresh.append(summary)
        return fresh

    def mark_sent(self, summaries: Iterable[TenderSummary]) -> None:
        updated = False
        for summary in summaries:
            key = self._summary_key(summary)
            if key not in self._ids:
                self._ids.add(key)
                self._records[key] = summary.to_dict()
                updated = True
        if updated:
            self._save()

    def all_summaries(self) -> List[TenderSummary]:
        results: List[TenderSummary] = []
        for record in self._records.values():
            enriched = {"tender_id": None, **record}
            results.append(TenderSummary(**enriched))
        return results

    def _load(self) -> dict:
        if not self._path.exists():
            return {"ids": set(), "records": {}}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logging.warning("Failed to parse %s, starting with empty store", self._path)
            return {"ids": set(), "records": {}}

        ids = payload.get("sent_ids")
        if ids is None:
            ids = payload.get("sent", [])
        records = payload.get("records", {})
        return {"ids": set(ids), "records": records}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sent_ids": sorted(self._ids),
            "records": self._records,
        }
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _summary_key(summary: TenderSummary) -> str:
        if summary.unique_name:
            return summary.unique_name
        return f"{summary.name}|{summary.customer_name}|{summary.address}"


class ChatRegistry:
    """Stores authorized chat IDs and polling offsets."""

    def __init__(self, path: Path) -> None:
        self._path = path
        data = self._load()
        self._chat_ids: Set[str] = set(data.get("chat_ids", []))
        self._next_offset: int = data.get("update_offset", 0)

    def chat_ids(self) -> List[str]:
        return sorted(self._chat_ids)

    def add_chat(self, chat_id: str) -> bool:
        chat_id = str(chat_id)
        if chat_id in self._chat_ids:
            return False
        self._chat_ids.add(chat_id)
        self._save()
        logging.info("Registered new chat %s", chat_id)
        return True

    def ensure_chats(self, chat_ids: Sequence[str]) -> int:
        added = 0
        for chat_id in chat_ids:
            if self.add_chat(chat_id):
                added += 1
        return added

    def is_registered(self, chat_id: str) -> bool:
        return str(chat_id) in self._chat_ids

    @property
    def next_offset(self) -> int:
        return self._next_offset

    def advance_offset(self, offset: int) -> None:
        if offset > self._next_offset:
            self._next_offset = offset
            self._save()

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logging.warning("Failed to parse %s, starting with empty chat registry", self._path)
            return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "chat_ids": sorted(self._chat_ids),
            "update_offset": self._next_offset,
        }
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class TelegramNotifier:
    def __init__(self, token: str, timeout: int = 30) -> None:
        self._token = token
        self._timeout = timeout

    def send_messages(
        self,
        chat_ids: Sequence[str],
        messages: Sequence[str],
        reply_markup: Optional[dict] = None,
    ) -> None:
        if not messages or not chat_ids:
            return
        for chat_id in chat_ids:
            for message in messages:
                self._send_single(chat_id, message, reply_markup=reply_markup)

    def _send_single(self, chat_id: str, text: str, reply_markup: Optional[dict] = None) -> None:
        url = TELEGRAM_API_URL.format(token=self._token)
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = requests.post(url, json=payload, timeout=self._timeout)
        try:
            response.raise_for_status()
        except requests.HTTPError:
            logging.exception("Failed to send message to chat %s: %s", chat_id, response.text)
            return
        logging.info("Sent Telegram message to chat %s", chat_id)


class TelegramUpdatePoller:
    """Handles polling Telegram for updates and processing commands."""
    
    def __init__(
        self,
        token: str,
        password: str,
        registry: ChatRegistry,
        notifier: TelegramNotifier,
        store: SentTenderStore,
        service: TenderService,
        sheets_service: Optional[GoogleSheetsService] = None,
        timeout: int = 40,
    ) -> None:
        self._token = token
        self._password = password
        self._registry = registry
        self._notifier = notifier
        self._store = store
        self._service = service
        self._sheets_service = sheets_service
        self._timeout = timeout
        self._has_sheets = sheets_service is not None and sheets_service.is_configured()
        
        # Deduplication cache to prevent processing the same update multiple times
        self._processed_updates: Set[int] = set()
        self._max_processed_cache = 100
        
        self._clear_webhook()
    
    def _clear_webhook(self) -> None:
        """Clear any existing webhook to ensure getUpdates polling works."""
        url = f"https://api.telegram.org/bot{self._token}/deleteWebhook"
        try:
            response = requests.post(url, json={"drop_pending_updates": True}, timeout=10)
            if response.status_code == 200:
                logging.info("Webhook cleared successfully")
        except requests.RequestException:
            logging.warning("Failed to clear webhook, continuing anyway")

    def poll(self) -> None:
        """Poll for new Telegram updates using long polling."""
        url = f"https://api.telegram.org/bot{self._token}/getUpdates"
        params = {
            "timeout": 30,
            "allowed_updates": ["message"],
            "offset": self._registry.next_offset,
        }
        
        logging.debug("Polling with offset=%d", self._registry.next_offset)
        
        try:
            response = requests.get(url, params=params, timeout=self._timeout)
            response.raise_for_status()
        except requests.RequestException:
            logging.exception("Failed to poll Telegram updates")
            return

        payload = response.json()
        if not payload.get("ok"):
            logging.error("Telegram API error: %s", payload)
            return
            
        updates = payload.get("result", [])
        if not updates:
            logging.debug("No new updates")
            return
        
        self._process_updates(updates)
    
    def _process_updates(self, updates: List[dict]) -> None:
        """Process a batch of updates and advance the offset."""
        max_update_id = max(u.get("update_id", 0) for u in updates)
        
        logging.debug("Processing %d updates (offset: %d, max_id: %d)", 
                     len(updates), self._registry.next_offset, max_update_id)
        
        for update in updates:
            self._process_single_update(update)
        
        # Always advance offset to prevent reprocessing
        if max_update_id >= self._registry.next_offset:
            self._registry.advance_offset(max_update_id + 1)
            logging.debug("Advanced offset to %d", max_update_id + 1)
    
    def _process_single_update(self, update: dict) -> None:
        """Process a single update with deduplication and error handling."""
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return
        
        # Deduplication checks
        if self._should_skip_update(update_id):
            return
        
        message = update.get("message")
        if not message:
            return
        
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "")
        logging.info("Processing update %d from chat %s: '%s'", update_id, chat_id, text)
        
        try:
            self._handle_message(message)
            logging.info("Successfully handled update %d", update_id)
        except Exception:
            logging.exception("Error handling message from update %d", update_id)
        
        self._mark_update_processed(update_id)
    
    def _should_skip_update(self, update_id: int) -> bool:
        """Check if an update should be skipped (already processed or too old)."""
        if update_id in self._processed_updates:
            logging.debug("Skipping duplicate update %d", update_id)
            return True
        
        if update_id < self._registry.next_offset:
            logging.debug("Skipping old update %d (offset: %d)", update_id, self._registry.next_offset)
            return True
        
        return False
    
    def _mark_update_processed(self, update_id: int) -> None:
        """Mark an update as processed and trim cache if needed."""
        self._processed_updates.add(update_id)
        
        if len(self._processed_updates) > self._max_processed_cache:
            sorted_updates = sorted(self._processed_updates)
            self._processed_updates = set(sorted_updates[-self._max_processed_cache:])

    def _handle_message(self, message: dict) -> None:
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        chat_id_str = str(chat_id)

        # Handle /start command specifically
        if text == "/start":
            if not self._registry.is_registered(chat_id_str):
                welcome_msg = f"🤖 Добро пожаловать! {PASSWORD_PROMPT}"
                self._notifier.send_messages([chat_id_str], [welcome_msg])
            else:
                welcome_msg = "✅ Вы уже авторизованы!\n\n" + get_help_message(self._has_sheets)
                self._notifier.send_messages([chat_id_str], [welcome_msg])
            return

        if not text:
            return

        if not self._registry.is_registered(chat_id_str):
            self._handle_unregistered(chat_id_str, text)
        else:
            self._handle_registered(chat_id_str, text)

    def _handle_unregistered(self, chat_id: str, text: str) -> None:
        if text == self._password:
            if self._registry.add_chat(chat_id):
                reply = "✅ Siz muvaffaqiyatli obuna bo'ldingiz!\n\n" + get_help_message(self._has_sheets)
            else:
                reply = "ℹ️ Siz allaqachon obuna bo'lgansiz!\n\n" + get_help_message(self._has_sheets)
            self._notifier.send_messages([chat_id], [reply])
        else:
            self._notifier.send_messages(
                [chat_id],
                [PASSWORD_PROMPT],
            )

    def _handle_registered(self, chat_id: str, text: str) -> None:
        """Handle commands from registered users."""
        if text == COMMAND_SENDALL:
            self._send_all_saved(chat_id)
        elif text == COMMAND_REFRESH:
            self._handle_refresh_command(chat_id)
        elif text == COMMAND_STATUS and self._has_sheets:
            self._send_sheets_status(chat_id)
        elif text == COMMAND_HELP or text.startswith("/"):
            self._send_help(chat_id)
        else:
            self._send_help(chat_id)
    
    def _handle_refresh_command(self, chat_id: str) -> None:
        """Handle the /refresh command to fetch new tenders."""
        count = fetch_and_send(
            self._service, 
            self._store, 
            self._notifier, 
            self._registry.chat_ids(), 
            self._sheets_service
        )
        
        if count:
            message = f"⚡️ {count} ta yangi tender yuborildi."
            if self._sheets_service and self._sheets_service.is_configured():
                message += " Google Sheets ham yangilandi."
        else:
            message = "ℹ️ Yangi tenderlar topilmadi."
        
        self._notifier.send_messages([chat_id], [message])
    
    def _send_help(self, chat_id: str) -> None:
        """Send help message to user."""
        help_msg = get_help_message(self._has_sheets)
        self._notifier.send_messages([chat_id], [help_msg])

    def _send_all_saved(self, chat_id: str) -> None:
        """Send all saved tenders to the user."""
        summaries = self._store.all_summaries()
        if not summaries:
            self._notifier.send_messages([chat_id], ["📂 Hozircha saqlangan tenderlar yo'q."])
            return
        
        count_msg = f"📦 Jami {len(summaries)} ta tender topildi:"
        self._notifier.send_messages([chat_id], [count_msg])
        
        messages = format_for_telegram(summaries)
        self._notifier.send_messages([chat_id], messages)
        
        final_msg = "✅ Barcha tenderlar yuborildi."
        self._notifier.send_messages([chat_id], [final_msg])

    def _send_sheets_status(self, chat_id: str) -> None:
        """Send Google Sheets integration status."""
        if not self._sheets_service:
            message = "📊 Google Sheets integratsiyasi o'chirilgan."
        elif not self._sheets_service.is_configured():
            message = "📊 Google Sheets sozlanmagan:\n\n❌ Credential fayli yoki Spreadsheet ID topilmadi"
        else:
            try:
                count = self._sheets_service.get_tender_count()
                message = (
                    f"📊 Google Sheets holati:\n\n"
                    f"✅ Ulangan\n"
                    f"📈 Jami tenderlar: {count}\n"
                    f"🔄 Avtomatik yangilanish: Yoqilgan"
                )
            except Exception as e:
                message = f"📊 Google Sheets holati:\n\n⚠️ Xatolik: {str(e)}"
        
        self._notifier.send_messages([chat_id], [message])


def fetch_and_send(
    service: TenderService,
    store: SentTenderStore,
    notifier: TelegramNotifier,
    chat_ids: Sequence[str],
    sheets_service: Optional[GoogleSheetsService] = None,
) -> int:
    """Fetch new tenders and send them to registered chats."""
    if not chat_ids:
        logging.info("No authorized chats yet; skipping send cycle")
        return 0

    summaries, _ = service.fetch_required_batches()
    fresh = store.filter_new(summaries)

    if not fresh:
        logging.info("No new tenders to send")
        return 0

    messages = format_for_telegram(fresh)
    notifier.send_messages(chat_ids, messages)
    store.mark_sent(fresh)
    
    _update_google_sheets(sheets_service, fresh)
    
    logging.info("Sent %s new tenders", len(fresh))
    return len(fresh)


def _update_google_sheets(
    sheets_service: Optional[GoogleSheetsService], 
    tenders: List[TenderSummary]
) -> None:
    """Update Google Sheets with new tenders if configured."""
    if not sheets_service or not sheets_service.is_configured():
        return
    
    try:
        success = sheets_service.add_tenders(tenders)
        if success:
            logging.info("Successfully updated Google Sheets with %s new tenders", len(tenders))
        else:
            logging.warning("Failed to update Google Sheets")
    except Exception as e:
        logging.error("Error updating Google Sheets: %s", e)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the tender Telegram bot loop")
    parser.add_argument("--interval-hours", type=float, default=DEFAULT_INTERVAL_HOURS)
    parser.add_argument(
        "--store-path",
        type=Path,
        default=Path("sent_tenders.json"),
        help="File used to remember which tenders have already been sent",
    )
    parser.add_argument(
        "--chat-store-path",
        type=Path,
        default=Path("authorized_chats.json"),
        help="Persistent file for authorized chat IDs",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single fetch/send cycle (useful for cron jobs)",
    )
    parser.add_argument(
        "--chat-ids",
        help="Comma-separated list of Telegram chat IDs to pre-authorize",
    )
    parser.add_argument(
        "--token",
        help="Telegram bot token (defaults to TELEGRAM_BOT_TOKEN env variable or provided default)",
    )
    parser.add_argument(
        "--password",
        help="Access password (defaults to TENDER_BOT_PASSWORD env or provided default)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging")
    
    # Google Sheets options
    parser.add_argument(
        "--enable-sheets",
        action="store_true",
        help="Enable Google Sheets integration for tender data logging"
    )
    parser.add_argument(
        "--google-credentials",
        type=Path,
        default=Path("google_credentials.json"),
        help="Path to Google service account credentials JSON file"
    )
    parser.add_argument(
        "--spreadsheet-id",
        help="Google Sheets spreadsheet ID (can also use GOOGLE_SPREADSHEET_ID env var)"
    )
    parser.add_argument(
        "--worksheet-name",
        default="Tenders",
        help="Name of the worksheet to use for tender data"
    )
    
    return parser.parse_args()


def _parse_chat_ids(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [chat.strip() for chat in raw.split(",") if chat.strip()]


def _resolve_settings(args: argparse.Namespace) -> Tuple[str, str, List[str]]:
    # Debug: Check if environment variables are accessible
    token_from_env = os.getenv("TELEGRAM_BOT_TOKEN")
    logging.info(f"TELEGRAM_BOT_TOKEN from env: {'Found' if token_from_env else 'NOT FOUND'}")
    logging.info(f"Available env vars: {', '.join([k for k in os.environ.keys() if 'TELEGRAM' in k or 'GOOGLE' in k])}")
    
    token = args.token or token_from_env
    if not token:
        raise ValueError(
            "Telegram bot token is required. "
            "Set TELEGRAM_BOT_TOKEN environment variable or use --token argument."
        )
    
    password = args.password or os.getenv("TENDER_BOT_PASSWORD") or DEFAULT_ACCESS_PASSWORD
    seed_chats = _parse_chat_ids(args.chat_ids or os.getenv("TELEGRAM_CHAT_IDS"))
    return token, password, seed_chats


def main() -> None:
    """Main entry point for the bot."""
    args = _parse_args()
    _configure_logging(args.debug)
    
    # Debug: Print all environment variables to diagnose Railway issue
    print("=" * 70)
    print("ENVIRONMENT VARIABLES DEBUG")
    print("=" * 70)
    print(f"Total env vars: {len(os.environ)}")
    print("\nRelevant variables:")
    for key in sorted(os.environ.keys()):
        if any(term in key.upper() for term in ['TELEGRAM', 'GOOGLE', 'TENDER', 'BOT', 'TOKEN']):
            # Mask sensitive values
            value = os.environ[key]
            masked = value[:10] + "..." if len(value) > 10 else "***"
            print(f"  {key} = {masked}")
    print("=" * 70)

    token, password, seed_chat_ids = _resolve_settings(args)

    service = TenderService()
    store = SentTenderStore(args.store_path)
    registry = ChatRegistry(args.chat_store_path)
    
    _seed_initial_chats(registry, seed_chat_ids)
    sheets_service = _initialize_sheets_service(args)
    
    notifier = TelegramNotifier(token)
    poller = TelegramUpdatePoller(token, password, registry, notifier, store, service, sheets_service)

    interval_seconds = max(60, int(args.interval_hours * 3600))
    
    logging.info("Starting bot with continuous polling (interval: %.1f hours)...", args.interval_hours)
    
    _run_bot_loop(args, poller, service, store, notifier, registry, sheets_service, interval_seconds)


def _configure_logging(debug: bool) -> None:
    """Configure logging level and format."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def _seed_initial_chats(registry: ChatRegistry, seed_chat_ids: List[str]) -> None:
    """Add initial chat IDs to the registry."""
    newly_added = registry.ensure_chats(seed_chat_ids)
    if newly_added:
        logging.info("Seeded %s chat IDs from CLI/env", newly_added)


def _initialize_sheets_service(args: argparse.Namespace) -> Optional[GoogleSheetsService]:
    """Initialize Google Sheets service if enabled."""
    if not args.enable_sheets:
        return None
    
    spreadsheet_id = args.spreadsheet_id or os.getenv("GOOGLE_SPREADSHEET_ID")
    sheets_service = GoogleSheetsService(
        credentials_path=args.google_credentials,
        spreadsheet_id=spreadsheet_id,
        worksheet_name=args.worksheet_name
    )
    
    if sheets_service.is_configured():
        logging.info("Google Sheets integration enabled")
    else:
        logging.warning("Google Sheets integration requested but not properly configured")
    
    return sheets_service


def _run_bot_loop(
    args: argparse.Namespace,
    poller: TelegramUpdatePoller,
    service: TenderService,
    store: SentTenderStore,
    notifier: TelegramNotifier,
    registry: ChatRegistry,
    sheets_service: Optional[GoogleSheetsService],
    interval_seconds: int,
) -> None:
    """Run the main bot polling loop."""
    last_fetch_time = 0
    
    while True:
        try:
            poller.poll()
            
            current_time = time.time()
            if current_time - last_fetch_time >= interval_seconds:
                logging.info("Running scheduled tender fetch...")
                fetch_and_send(service, store, notifier, registry.chat_ids(), sheets_service)
                last_fetch_time = current_time
                
        except KeyboardInterrupt:
            logging.info("Bot stopped by user")
            break
        except Exception:
            logging.exception("Bot cycle failed")
            time.sleep(5)
            
        if args.once:
            break


if __name__ == "__main__":
    main()