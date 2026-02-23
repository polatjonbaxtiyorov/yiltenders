"""Google Sheets integration for tender data management.

This module provides functionality to automatically update Google Sheets
with new tender data at regular intervals.
"""

from __future__ import annotations

import logging
import os
import json
from datetime import datetime
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

# Column headers for the spreadsheet
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

# Column range helper (A..J for 10 headers)
_CLEAR_RANGE = "A2:J1000"


class GoogleSheetsService:
    """Service for managing tender data in Google Sheets."""

    def __init__(
        self,
        credentials_path: Optional[Path] = None,
        spreadsheet_id: Optional[str] = None,
        worksheet_name: str = "Tenders",
    ) -> None:
        """Initialize Google Sheets service.

        Args:
            credentials_path: Path to service account JSON credentials file
            spreadsheet_id: Google Sheets spreadsheet ID (optional; falls back to env var)
            worksheet_name: Name of the worksheet to use
        """
        self.credentials_path = Path(credentials_path) if credentials_path else Path("google_credentials.json")
        # Priority: provided spreadsheet_id -> env var -> built-in default (if any)
        self.spreadsheet_id = (
            spreadsheet_id
            or os.getenv("GOOGLE_SPREADSHEET_ID")
            or "1H2UE2v3ftFTvffcu12p1XEpyCceK6_u5dlXSsvVF0C8"
        )
        self.worksheet_name = worksheet_name
        self._client: Optional[gspread.Client] = None
        self._worksheet: Optional[gspread.Worksheet] = None

    def _authenticate(self) -> bool:
        """Authenticate with Google Sheets API.

        Returns:
            True if authentication successful, False otherwise
        """
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

    def replace_all_tenders(self, tenders: List[TenderSummary]) -> bool:
        """Completely replace all data in sheet with fresh tenders.

        This clears existing data (keeping header row) and appends all provided tenders.
        """
        worksheet = self._get_worksheet()
        if not worksheet:
            return False

        try:
            # Clear all data (except header) and re-write header to be safe
            try:
                worksheet.batch_clear([_CLEAR_RANGE])
            except Exception:
                # Some accounts / worksheet states may not allow batch_clear; fall back to clear()
                try:
                    worksheet.clear()
                except Exception:
                    logger.warning("Failed to clear worksheet cleanly; continuing to attempt to write anyway")

            # Ensure header exists (overwrite first row)
            try:
                worksheet.update("A1", [HEADERS], value_input_option="USER_ENTERED")
            except Exception:
                # Fallback append if update fails
                try:
                    worksheet.append_row(HEADERS, value_input_option="USER_ENTERED")
                except Exception:
                    logger.warning("Failed to write header row; continuing")

            new_rows = []

            for tender in tenders:
                # Convert start_price to Decimal when possible
                start_price_decimal = None
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
                    tender.unique_name or "",
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
                # append_rows expects list of rows
                worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")
            logger.info("Replaced sheet with %d tenders", len(new_rows))
            return True

        except Exception as e:
            logger.exception("Error replacing sheet data: %s", e)
            return False

    def _get_worksheet(self) -> Optional[gspread.Worksheet]:
        """Get or create the worksheet.

        Returns:
            Worksheet object or None if failed
        """
        if not self._client:
            if not self._authenticate():
                return None

        try:
            if not self.spreadsheet_id:
                logger.error("No spreadsheet ID provided")
                return None

            spreadsheet = self._client.open_by_key(self.spreadsheet_id)

            # Try to get existing worksheet
            try:
                worksheet = spreadsheet.worksheet(self.worksheet_name)
                logger.info("Found existing worksheet: %s", self.worksheet_name)
            except gspread.WorksheetNotFound:
                # Create new worksheet
                worksheet = spreadsheet.add_worksheet(title=self.worksheet_name, rows=1000, cols=len(HEADERS))
                logger.info("Created new worksheet: %s", self.worksheet_name)

                # Add headers
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
            error_type = type(e).__name__
            logger.error("Error accessing worksheet: %s: %s", error_type, error_msg)

            # Provide more helpful error messages when possible
            if "403" in error_msg or "Forbidden" in error_msg:
                logger.error("Permission denied. Please share the spreadsheet with the service account.")
                try:
                    if self.credentials_path.exists():
                        with open(self.credentials_path, "r", encoding="utf-8") as f:
                            creds = json.load(f)
                            service_email = creds.get("client_email", "unknown")
                            logger.error("Service account email: %s", service_email)
                except Exception:
                    logger.debug("Could not read credentials file to show service account email")
            elif "404" in error_msg:
                logger.error("Spreadsheet not found. Check the spreadsheet ID.")
            else:
                logger.debug("Unhandled error while accessing spreadsheet")

            logger.exception("Full exception details:")
            return None

    def add_tenders(self, tenders: List[TenderSummary]) -> bool:
        """Add new tender data to the spreadsheet.

        Args:
            tenders: List of TenderSummary objects to add

        Returns:
            True if successful, False otherwise
        """
        if not tenders:
            logger.info("No tenders to add to spreadsheet")
            return True

        worksheet = self._get_worksheet()
        if not worksheet:
            return False

        try:
            # Get existing tender IDs to avoid duplicates (skip header)
            existing_ids = set()
            try:
                existing_data = worksheet.get_all_values()[1:]  # Skip header
                existing_ids = {row[0] for row in existing_data if row and row[0]}
            except Exception as e:
                logger.warning("Could not fetch existing data: %s", e)

            # Prepare new rows
            new_rows = []
            logger.info("Preparing to add %d tenders", len(tenders))
            for tender in tenders:
                tender_id = str(getattr(tender, "tender_id", "")) if getattr(tender, "tender_id", None) else (tender.unique_name or "")
                logger.debug("Tender: ID=%s, Name=%s", tender_id, tender.name)

                # Skip if already exists
                if tender_id and tender_id in existing_ids:
                    continue

                # Convert start_price to Decimal for calculations
                start_price_decimal = None
                if tender.start_price:
                    try:
                        start_price_decimal = Decimal(str(tender.start_price))
                    except (ValueError, TypeError):
                        start_price_decimal = None

                # Calculate discount and final price
                discount_percent = None
                final_price = None
                if start_price_decimal and getattr(tender, "required_percent", None):
                    discount_percent = _discount_percent(start_price_decimal)
                    if discount_percent:
                        final_price = start_price_decimal - (start_price_decimal * discount_percent)

                row = [
                    tender.unique_name or "",
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

            # Add new rows to spreadsheet
            try:
                worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")
                logger.info("Added %d new tenders to spreadsheet", len(new_rows))
                return True
            except Exception as e:
                logger.exception("FAILED to append rows: %s", e)
                return False

        except Exception as e:
            logger.exception("Error adding tenders to spreadsheet: %s", e)
            return False

    def get_tender_count(self) -> int:
        """Get the total number of tenders in the spreadsheet.

        Returns:
            Number of tender records (excluding header)
        """
        worksheet = self._get_worksheet()
        if not worksheet:
            return 0

        try:
            all_values = worksheet.get_all_values()
            return max(0, len(all_values) - 1)  # Exclude header
        except Exception as e:
            logger.exception("Error getting tender count: %s", e)
            return 0

    def clear_all_data(self) -> bool:
        """Clear all tender data (keeping headers).

        Returns:
            True if successful, False otherwise
        """
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
        """Check if Google Sheets service is properly configured.

        Returns:
            True if credentials and spreadsheet ID are available
        """
        has_credentials = os.getenv("GOOGLE_CREDENTIALS_JSON") is not None or (
            self.credentials_path is not None and self.credentials_path.exists()
        )
        return bool(has_credentials and self.spreadsheet_id)

# Utility to help users create a sample credentials file when running module directly
def create_sample_credentials_file() -> None:
    """Create a sample Google credentials file for user reference."""
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
