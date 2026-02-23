"""Google Sheets integration for tender data management.

This module provides functionality to automatically update Google Sheets
with new tender data at regular intervals.

Behavior:
- Column A will contain formulas like:
  =HYPERLINK("https://tender.mc.uz/tender-list/tender/225390/view","26411012225390")
  (URL uses last 6 digits, display shows full ID)
"""

from __future__ import annotations

import logging
import os
import json
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

import gspread
from google.auth.exceptions import GoogleAuthError
from google.oauth2.service_account import Credentials

from tender_service import _discount_percent, TenderSummary

logger = logging.getLogger(__name__)

# Default scopes for Google Sheets API
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Column headers for the spreadsheet (row 1)
HEADERS = [
    "Лот рақами",
    "Лойиҳа номи",
    "Лойиҳа манзили",
    "Бошланғич нарх",
    "Чегирма",
    "Таклиф нархи",
    "Мураккаблик тоифаси",
    "Таклифларни топширишнинг охирги муддати",
    "Ишларни бажариш муддати",
    "Буюртмачи",
]

# Range used for clearing data (A2..J1000 covers ten columns)
_CLEAR_RANGE = "A2:J1000"


class GoogleSheetsService:
    """Service for managing tender data in Google Sheets."""

    def __init__(
        self,
        credentials_path: Optional[Path] = None,
        spreadsheet_id: Optional[str] = None,
        worksheet_name: str = "Tenders",
    ) -> None:
        self.credentials_path = Path(credentials_path) if credentials_path else Path("google_credentials.json")
        self.spreadsheet_id = (
            spreadsheet_id
            or os.getenv("GOOGLE_SPREADSHEET_ID")
            or "1H2UE2v3ftFTvffcu12p1XEpyCceK6_u5dlXSsvVF0C8"
        )
        self.worksheet_name = worksheet_name
        self._client: Optional[gspread.Client] = None
        self._worksheet: Optional[gspread.Worksheet] = None

    def _authenticate(self) -> bool:
        """Authenticate with Google Sheets API."""
        try:
            google_creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")

            if google_creds_json:
                logger.info("Using Google credentials from environment variable")
                try:
                    credentials_info = json.loads(google_creds_json)
                    credentials = Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
                except json.JSONDecodeError as e:
                    logger.error("Failed to parse GOOGLE_CREDENTIALS_JSON: %s", e)
                    return False

            elif self.credentials_path and self.credentials_path.exists():
                logger.info("Using Google credentials from file: %s", self.credentials_path)
                credentials = Credentials.from_service_account_file(str(self.credentials_path), scopes=SCOPES)
            else:
                logger.error(
                    "No Google credentials found. Set GOOGLE_CREDENTIALS_JSON env var or provide credentials file at: %s",
                    self.credentials_path,
                )
                return False

            self._client = gspread.authorize(credentials)
            logger.info("Successfully authenticated with Google Sheets API")
            return True

        except GoogleAuthError as e:
            logger.error("Google authentication failed: %s", e)
            return False
        except Exception as e:
            logger.exception("Unexpected error during authentication: %s", e)
            return False

    def _get_worksheet(self) -> Optional[gspread.Worksheet]:
        """Get or create the worksheet."""
        if not self._client:
            if not self._authenticate():
                return None

        try:
            if not self.spreadsheet_id:
                logger.error("No spreadsheet ID provided")
                return None

            logger.debug("Using spreadsheet ID: %s", self.spreadsheet_id)
            spreadsheet = self._client.open_by_key(self.spreadsheet_id)

            try:
                worksheet = spreadsheet.worksheet(self.worksheet_name)
                logger.info("Found existing worksheet: %s", self.worksheet_name)
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(title=self.worksheet_name, rows=1000, cols=len(HEADERS))
                logger.info("Created new worksheet: %s", self.worksheet_name)
                try:
                    worksheet.append_row(HEADERS, value_input_option="USER_ENTERED")
                    logger.info("Added headers to new worksheet")
                except Exception:
                    logger.warning("Could not append headers to newly created worksheet")

            self._worksheet = worksheet
            return worksheet

        except gspread.SpreadsheetNotFound:
            logger.error("Spreadsheet not found with ID: %s", self.spreadsheet_id)
            return None
        except Exception as e:
            error_msg = str(e)
            logger.error("Error accessing worksheet: %s", error_msg)
            if "403" in error_msg or "Forbidden" in error_msg:
                logger.error("Permission denied. Share the spreadsheet with the service account.")
                try:
                    if self.credentials_path.exists():
                        with open(self.credentials_path, "r", encoding="utf-8") as f:
                            creds = json.load(f)
                            service_email = creds.get("client_email", "unknown")
                            logger.error("Service account email: %s", service_email)
                except Exception:
                    logger.debug("Could not read credentials file for service account email")
            return None

    def replace_all_tenders(self, tenders: List[TenderSummary]) -> bool:
        """Completely replace all data in sheet with fresh tenders."""
        worksheet = self._get_worksheet()
        if not worksheet:
            return False

        try:
            # Clear existing data (except header)
            try:
                worksheet.batch_clear([_CLEAR_RANGE])
            except Exception:
                try:
                    worksheet.clear()
                except Exception:
                    logger.warning("Failed to clear worksheet cleanly; continuing")

            # Ensure header exists
            try:
                worksheet.update("A1", [HEADERS], value_input_option="USER_ENTERED")
            except Exception:
                try:
                    worksheet.append_row(HEADERS, value_input_option="USER_ENTERED")
                except Exception:
                    logger.warning("Failed to write header row; continuing")

            base_url = "https://tender.mc.uz/tender-list/tender/"
            new_rows: List[List[str]] = []

            for tender in tenders:
                # --- determine real_id (FULL ID) ---
                real_id = ""
                if getattr(tender, "tender_id", None) is not None:
                    real_id = str(getattr(tender, "tender_id"))
                elif getattr(tender, "unique_name", None):
                    real_id = str(getattr(tender, "unique_name"))

                # Build URL with last6 but display full real_id
                if real_id:
                    last6 = real_id[-6:] if len(real_id) >= 6 else real_id
                    url = f"{base_url}{last6}/view"
                    safe_display = real_id.replace('"', '""')
                    tender_link = f'=HYPERLINK("{url}", "{safe_display}")'
                else:
                    tender_link = ""

                # Debug: log what we write for the first few tenders (or all in debug)
                logger.debug("Prepared tender_link: %s", tender_link)

                # Price / discount handling
                start_price_decimal: Optional[Decimal] = None
                if tender.start_price:
                    try:
                        start_price_decimal = Decimal(str(tender.start_price))
                    except (ValueError, TypeError):
                        start_price_decimal = None

                discount_percent = None
                final_price = None
                if start_price_decimal and getattr(tender, "required_percent", None):
                    discount_percent = _discount_percent(start_price_decimal)
                    if discount_percent:
                        final_price = start_price_decimal - (start_price_decimal * discount_percent)

                row = [
                    tender_link,
                    tender.name or "",
                    tender.address or "",
                    tender.start_price or "",
                    str(discount_percent) if discount_percent else "",
                    str(final_price) if final_price else "",
                    str(getattr(tender, "complexity_category_id", "")) if getattr(tender, "complexity_category_id", None) else "",
                    tender.placement_term or "",
                    str(getattr(tender, "end_term_work_days", "")) if getattr(tender, "end_term_work_days", None) else "",
                    tender.customer_name or "",
                ]
                new_rows.append(row)

            if new_rows:
                worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")
                logger.info("Appended %d rows to sheet", len(new_rows))
            else:
                logger.info("No rows to append")

            try:
                total_rows = len(worksheet.get_all_values())
                logger.info("Sheet now contains %d rows (including header)", total_rows)
            except Exception:
                logger.debug("Could not fetch sheet row count after replace")

            return True

        except Exception as e:
            logger.exception("Error replacing sheet data: %s", e)
            return False

    def add_tenders(self, tenders: List[TenderSummary]) -> bool:
        """Add new tender data to the spreadsheet."""
        if not tenders:
            logger.info("No tenders to add to spreadsheet")
            return True

        worksheet = self._get_worksheet()
        if not worksheet:
            return False

        try:
            # Read existing display IDs from column A (handle HYPERLINK formulas)
            existing_ids = set()
            try:
                existing_data = worksheet.get_all_values()[1:]  # skip header
                for row in existing_data:
                    if not row:
                        continue
                    first = row[0]
                    if not first:
                        continue
                    if "HYPERLINK" in first:
                        # formula looks like: =HYPERLINK("url","display")
                        parts = first.split('"')
                        if len(parts) >= 4:
                            display = parts[3]
                            existing_ids.add(display)
                        else:
                            existing_ids.add(first)
                    else:
                        existing_ids.add(first)
            except Exception as e:
                logger.warning("Could not fetch existing data: %s", e)

            base_url = "https://tender.mc.uz/tender-list/tender/"
            new_rows: List[List[str]] = []

            for tender in tenders:
                # determine real_id
                real_id = ""
                if getattr(tender, "tender_id", None) is not None:
                    real_id = str(getattr(tender, "tender_id"))
                elif getattr(tender, "unique_name", None):
                    real_id = str(getattr(tender, "unique_name"))

                display_id = real_id or ""
                if display_id and display_id in existing_ids:
                    logger.debug("Skipping existing tender id: %s", display_id)
                    continue

                if real_id:
                    last6 = real_id[-6:] if len(real_id) >= 6 else real_id
                    url = f"{base_url}{last6}/view"
                    safe_display = real_id.replace('"', '""')
                    tender_link = f'=HYPERLINK("{url}", "{safe_display}")'
                else:
                    tender_link = ""

                logger.debug("Prepared tender_link (add): %s", tender_link)

                # Price / discount
                start_price_decimal: Optional[Decimal] = None
                if tender.start_price:
                    try:
                        start_price_decimal = Decimal(str(tender.start_price))
                    except (ValueError, TypeError):
                        start_price_decimal = None

                discount_percent = None
                final_price = None
                if start_price_decimal and getattr(tender, "required_percent", None):
                    discount_percent = _discount_percent(start_price_decimal)
                    if discount_percent:
                        final_price = start_price_decimal - (start_price_decimal * discount_percent)

                row = [
                    tender_link,
                    tender.name or "",
                    tender.address or "",
                    tender.start_price or "",
                    str(discount_percent) if discount_percent else "",
                    str(final_price) if final_price else "",
                    str(getattr(tender, "complexity_category_id", "")) if getattr(tender, "complexity_category_id", None) else "",
                    tender.placement_term or "",
                    str(getattr(tender, "end_term_work_days", "")) if getattr(tender, "end_term_work_days", None) else "",
                    tender.customer_name or "",
                ]

                new_rows.append(row)

            if not new_rows:
                logger.info("No new tenders to add (all already exist)")
                return True

            worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")
            logger.info("Added %d new tenders to spreadsheet", len(new_rows))
            return True

        except Exception as e:
            logger.exception("Error adding tenders to spreadsheet: %s", e)
            return False

    def get_tender_count(self) -> int:
        worksheet = self._get_worksheet()
        if not worksheet:
            return 0
        try:
            all_values = worksheet.get_all_values()
            return max(0, len(all_values) - 1)
        except Exception as e:
            logger.exception("Error getting tender count: %s", e)
            return 0

    def clear_all_data(self) -> bool:
        worksheet = self._get_worksheet()
        if not worksheet:
            return False
        try:
            worksheet.batch_clear([_CLEAR_RANGE])
            logger.info("Cleared all tender data from spreadsheet")
            return True
        except Exception as e:
            logger.exception("Error clearing spreadsheet data: %s", e)
            return False

    def is_configured(self) -> bool:
        has_credentials = os.getenv("GOOGLE_CREDENTIALS_JSON") is not None or (
            self.credentials_path is not None and self.credentials_path.exists()
        )
        return bool(has_credentials and self.spreadsheet_id)


def create_sample_credentials_file() -> None:
    sample_creds = {
        "type": "service_account",
        "project_id": "your-project-id",
        "private_key_id": "your-private-key-id",
        "private_key": "-----BEGIN PRIVATE KEY-----\nYOUR_PRIVATE_KEY_HERE\n-----END PRIVATE KEY-----\n",
        "client_email": "your-service-account@your-project.iam.gserviceaccount.com",
        "client_id": "your-client-id",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/your-service-account%40your-project.iam.gserviceaccount.com",
    }
    sample_path = Path("google_credentials_sample.json")
    with open(sample_path, "w", encoding="utf-8") as f:
        json.dump(sample_creds, f, indent=2)
    print(f"Sample credentials file created: {sample_path}")
    print("\nTo set up Google Sheets integration:")
    print("1. Go to Google Cloud Console (https://console.cloud.google.com/)")
    print("2. Create a new project or select existing one")
    print("3. Enable Google Sheets API and Google Drive API")
    print("4. Create a Service Account and download the JSON key")
    print("5. Rename the key file to 'google_credentials.json' or set GOOGLE_CREDENTIALS_JSON env var")
    print("6. Share your Google Sheet with the service account email")
    print("7. Optionally set GOOGLE_SPREADSHEET_ID env var or pass --spreadsheet-id to the test command")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Google Sheets service utilities")
    parser.add_argument("--create-sample", action="store_true", help="Create sample credentials file")
    parser.add_argument("--test", action="store_true", help="Test Google Sheets connection")
    parser.add_argument("--spreadsheet-id", help="Google Sheets spreadsheet ID for testing")

    args = parser.parse_args()

    if args.create_sample:
        create_sample_credentials_file()
    elif args.test:
        logging.basicConfig(level=logging.INFO)
        service = GoogleSheetsService(spreadsheet_id=args.spreadsheet_id)
        if service.is_configured():
            count = service.get_tender_count()
            print(f"✅ Connection successful! Current tender count: {count}")
        else:
            print("❌ Google Sheets not configured properly")
