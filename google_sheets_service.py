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
from typing import List, Optional, Dict, Any
from tender_service import _discount_percent
import gspread
from google.auth.exceptions import GoogleAuthError
from google.oauth2.service_account import Credentials

from tender_service import TenderSummary

logger = logging.getLogger(__name__)

# Default scopes for Google Sheets API
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.file'
]

# Column headers for the spreadsheet
HEADERS = [
    'Лот рақами',
    'Лойиҳа номи',
    'Лойиҳа манзили',
    'Бошланғич нарх',
    'Чегирма',
    'Таклиф нархи',
    'Мураккаблик тоифаси',
    'Таклифларни топширишнинг охирги муддати',
    'Ишларни бажариш муддати',
    'Буюртмачи'
]


class GoogleSheetsService:
    """Service for managing tender data in Google Sheets."""
    
    def __init__(
        self,
        credentials_path: Optional[Path] = None,
        spreadsheet_id: Optional[str] = None,
        worksheet_name: str = "Tenders"
    ) -> None:
        """Initialize Google Sheets service.
        
        Args:
            credentials_path: Path to service account JSON credentials file
            spreadsheet_id: Google Sheets spreadsheet ID
            worksheet_name: Name of the worksheet to use
        """
        self.credentials_path = credentials_path or Path("google_credentials.json")
        self.spreadsheet_id = "1H2UE2v3ftFTvffcu12p1XEpyCceK6_u5dlXSsvVF0C8"
        self.worksheet_name = worksheet_name
        self._client: Optional[gspread.Client] = None
        self._worksheet: Optional[gspread.Worksheet] = None
        
    def _authenticate(self) -> bool:
        """Authenticate with Google Sheets API.
        
        Returns:
            True if authentication successful, False otherwise
        """
        try:
            # Try to use environment variable first (for Railway/cloud deployment)
            google_creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
            
            if google_creds_json:
                logger.info("Using Google credentials from environment variable")
                try:
                    credentials_info = json.loads(google_creds_json)
                    credentials = Credentials.from_service_account_info(
                        credentials_info,
                        scopes=SCOPES
                    )
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse GOOGLE_CREDENTIALS_JSON: {e}")
                    return False
            # Fall back to file-based credentials (for local development)
            elif self.credentials_path.exists():
                logger.info(f"Using Google credentials from file: {self.credentials_path}")
                credentials = Credentials.from_service_account_file(
                    str(self.credentials_path),
                    scopes=SCOPES
                )
            else:
                logger.error(
                    "No Google credentials found. Set GOOGLE_CREDENTIALS_JSON environment variable "
                    f"or provide credentials file at: {self.credentials_path}"
                )
                return False
            
            self._client = gspread.authorize(credentials)
            logger.info("Successfully authenticated with Google Sheets API")
            return True
            
        except GoogleAuthError as e:
            logger.error(f"Google authentication failed: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during authentication: {e}")
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
                logger.info(f"Found existing worksheet: {self.worksheet_name}")
            except gspread.WorksheetNotFound:
                # Create new worksheet
                worksheet = spreadsheet.add_worksheet(
                    title=self.worksheet_name,
                    rows=1000,
                    cols=len(HEADERS)
                )
                logger.info(f"Created new worksheet: {self.worksheet_name}")
                
                # Add headers
                worksheet.append_row(HEADERS)
                logger.info("Added headers to new worksheet")
                
            self._worksheet = worksheet
            return worksheet
            
        except gspread.SpreadsheetNotFound:
            logger.error(f"Spreadsheet not found with ID: {self.spreadsheet_id}")
            return None
        except Exception as e:
            error_msg = str(e)
            error_type = type(e).__name__
            logger.error(f"Error accessing worksheet: {error_type}: {error_msg}")
            
            # Provide more helpful error messages
            if "403" in error_msg or "Forbidden" in error_msg:
                logger.error("Permission denied. Please share the spreadsheet with the service account:")
                with open(self.credentials_path, 'r') as f:
                    creds = json.load(f)
                    service_email = creds.get('client_email', 'unknown')
                    logger.error(f"Service account email: {service_email}")
            elif "404" in error_msg:
                logger.error("Spreadsheet not found. Check the spreadsheet ID.")
            elif not error_msg:
                logger.error(f"Unknown error of type {error_type}")
            
            # Re-raise for debugging
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
            # Get existing tender IDs to avoid duplicates
            existing_ids = set()
            try:
                existing_data = worksheet.get_all_values()[1:]  # Skip header
                existing_ids = {row[0] for row in existing_data if row and row[0]}
            except Exception as e:
                logger.warning(f"Could not fetch existing data: {e}")
            
            # Prepare new rows
            new_rows = []
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            for tender in tenders:
                tender_id = str(tender.tender_id) if tender.tender_id else tender.unique_name or ""
                
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
                if start_price_decimal and tender.required_percent:
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
                    str(tender.complexity_category_id) if tender.complexity_category_id else "",
                    tender.placement_term or "",
                    str(tender.end_term_work_days) if tender.end_term_work_days else "",
                    tender.customer_name or "",
                ]
                new_rows.append(row)
            
            if not new_rows:
                logger.info("No new tenders to add (all already exist)")
                return True
                
            # Add new rows to spreadsheet
            worksheet.append_rows(new_rows)
            logger.info(f"Added {len(new_rows)} new tenders to spreadsheet")
            return True
            
        except Exception as e:
            logger.error(f"Error adding tenders to spreadsheet: {e}")
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
            return len(all_values) - 1  # Exclude header
        except Exception as e:
            logger.error(f"Error getting tender count: {e}")
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
            # Clear all data except header row
            worksheet.batch_clear(["A2:K1000"])
            logger.info("Cleared all tender data from spreadsheet")
            return True
        except Exception as e:
            logger.error(f"Error clearing spreadsheet data: {e}")
            return False
    
    def is_configured(self) -> bool:
        """Check if Google Sheets service is properly configured.
        
        Returns:
            True if credentials and spreadsheet ID are available
        """
        has_credentials = (
            os.getenv("GOOGLE_CREDENTIALS_JSON") is not None or 
            self.credentials_path.exists()
        )
        return has_credentials and self.spreadsheet_id is not None


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
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/your-service-account%40your-project.iam.gserviceaccount.com"
    }
    
    sample_path = Path("google_credentials_sample.json")
    with open(sample_path, 'w', encoding='utf-8') as f:
        json.dump(sample_creds, f, indent=2)
    
    print(f"Sample credentials file created: {sample_path}")
    print("\nTo set up Google Sheets integration:")
    print("1. Go to Google Cloud Console (https://console.cloud.google.com/)")
    print("2. Create a new project or select existing one")
    print("3. Enable Google Sheets API and Google Drive API")
    print("4. Create a Service Account and download the JSON key")
    print("5. Rename the key file to 'google_credentials.json'")
    print("6. Share your Google Sheet with the service account email")
    print("7. Set GOOGLE_SPREADSHEET_ID environment variable")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Google Sheets service utilities")
    parser.add_argument("--create-sample", action="store_true", 
                       help="Create sample credentials file")
    parser.add_argument("--test", action="store_true",
                       help="Test Google Sheets connection")
    parser.add_argument("--spreadsheet-id", 
                       help="Google Sheets spreadsheet ID for testing")
    
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