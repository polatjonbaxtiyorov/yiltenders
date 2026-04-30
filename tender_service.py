"""Tender fetching and filtering utilities for Telegram bot integration.

This module calls the apisitender API, applies the business filtering rules
requested by the customer, and shapes the response into a telegram-friendly
payload.

Behavior notes:
- Keeps original region-based and nationwide filters.
- Adds an OPTIONAL AVTOMOBIL keyword filter (require_avtomobil flag).
- fetch_required_batches() expands scope by collecting both:
    - 'normal' results (existing rules), and
    - 'avtomobil' results (AVTOMOBIL keyword)
  then merging them and deduplicating by the existing summary key logic.
"""

from __future__ import annotations

import argparse
import codecs
import json
import logging
import unicodedata
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

API_URL = "https://apisitender.mc.uz/api/tenders"
REGION_ALLOWLIST = {10, 14}
TARGET_CUSTOMER_ID = 374
REGION_CUSTOMER_KEYWORD = "YO`LLAR"
ADDITIONAL_CUSTOMER_KEYWORD = "AVTOMOBIL"
DEFAULT_PARAMS = {
    "per_page": 100,
    "page": 1,
    "lot_type": 2,
    "sort_by": "desc",
    "order_by": "confirmed_date",
    "status": 2,
}

logger = logging.getLogger(__name__)


def _normalize_text(value: Optional[Any]) -> Optional[str]:
    """Normalize string: decode escapes and normalize unicode NFC."""
    if value is None:
        return None

    text = str(value)
    if "\\" in text:
        try:
            text = codecs.decode(text, "unicode_escape")
        except Exception:
            pass

    try:
        return unicodedata.normalize("NFC", text)
    except Exception:
        return text


@dataclass
class TenderSummary:
    tender_id: Optional[int]
    name: Optional[str]
    unique_name: Optional[str]
    start_price: Optional[str]
    required_percent: Optional[str]
    placement_term: Optional[str]
    complexity_category_id: Optional[int]
    end_term_work_days: Optional[int]
    customer_name: Optional[str]
    address: Optional[str]

    def to_dict(self) -> Dict[str, Optional[Any]]:
        return asdict(self)


class TenderService:
    """Fetch tenders and apply custom filtering rules."""

    def __init__(self, session: Optional[requests.Session] = None, timeout: int = 30) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout

    def _customer_contains_avtomobil(self, item: Dict[str, Any]) -> bool:
        customer_name = (item.get("customer") or {}).get("name")
        if not customer_name:
            return False

        normalized = _normalize_text(customer_name)
        if not normalized:
            return False

        normalized_lower = (
            normalized
            .replace("'", "'")
            .replace("'", "'")
            .replace("`", "'")
            .replace("ʹ", "'")
            .replace("ʼ", "'")
            .replace("\u2019", "'")
            .replace("\u2018", "'")
            .strip()
            .lower()
        )
        normalized_lower = " ".join(normalized_lower.split())
        return ADDITIONAL_CUSTOMER_KEYWORD.lower() in normalized_lower

    def fetch_raw(self, region_id: Optional[int] = None) -> Dict[str, Any]:
        params = DEFAULT_PARAMS.copy()
        if region_id is not None:
            self._validate_region(region_id)
            params["region_id"] = region_id

        logger.info("Requesting %s with params=%s", API_URL, params)
        response = self._session.get(API_URL, params=params, timeout=self._timeout)
        response.raise_for_status()
        logger.info(
            "Server responded with status=%s content_length=%s",
            response.status_code,
            response.headers.get("content-length"),
        )

        payload = response.json()
        data_len = len(payload.get("result", {}).get("data", []) or [])
        logger.info("Server payload contains %s tender items", data_len)
        return payload

    def fetch_and_filter(self, region_id: Optional[int] = None) -> List[TenderSummary]:
        payload = self.fetch_raw(region_id=region_id)
        return self.filter_payload(payload, region_id=region_id)

    def filter_payload(
        self,
        payload: Dict[str, Any],
        region_id: Optional[int] = None,
        require_avtomobil: bool = False,
    ) -> List[TenderSummary]:
        items = payload.get("result", {}).get("data", [])
        total_items = len(items)
        logger.debug("Filtering %s items for region_id=%s (require_avtomobil=%s)", total_items, region_id, require_avtomobil)

        if require_avtomobil:
            filtered = [item for item in items if self._customer_contains_avtomobil(item)]
            logger.info("AVTOMOBIL mode: kept %s of %s items (region ignored)", len(filtered), total_items)
            return [self._into_summary(item) for item in filtered]

        if region_id is not None and region_id not in REGION_ALLOWLIST:
            raise ValueError(
                f"Unsupported region_id={region_id}. Only {sorted(REGION_ALLOWLIST)} are allowed."
            )

        if region_id in REGION_ALLOWLIST:
            filtered = [item for item in items if self._region_customer_matches(item)]
        else:
            filtered = [item for item in items if self._is_target_customer(item)]

        logger.info("After filtering (normal mode), %s items remain", len(filtered))
        return [self._into_summary(item) for item in filtered]

    def fetch_required_batches(self) -> Tuple[List[TenderSummary], List[Dict[str, Any]]]:
        combined: Dict[str, TenderSummary] = {}
        payloads_with_meta: List[Dict[str, Any]] = []

        for region_id in sorted(REGION_ALLOWLIST):
            payload = self.fetch_raw(region_id=region_id)
            payloads_with_meta.append({"region_id": region_id, "payload": payload})

            normal = self.filter_payload(payload, region_id=region_id, require_avtomobil=False)
            avtomobil = self.filter_payload(payload, region_id=region_id, require_avtomobil=True)

            self._merge_summaries(combined, normal)
            self._merge_summaries(combined, avtomobil)

        nationwide_payload = self.fetch_raw(region_id=None)
        payloads_with_meta.append({"region_id": None, "payload": nationwide_payload})

        normal = self.filter_payload(nationwide_payload, require_avtomobil=False)
        avtomobil = self.filter_payload(nationwide_payload, require_avtomobil=True)

        self._merge_summaries(combined, normal)
        self._merge_summaries(combined, avtomobil)

        logger.info("Combined total summaries (expanded scope): %s", len(combined))
        return list(combined.values()), payloads_with_meta

    def _is_target_customer(self, item: Dict[str, Any]) -> bool:
        return item.get("customer", {}).get("id") == TARGET_CUSTOMER_ID

    def _validate_region(self, region_id: int) -> None:
        if region_id not in REGION_ALLOWLIST:
            raise ValueError(
                f"Unsupported region_id={region_id}. Only {sorted(REGION_ALLOWLIST)} are allowed."
            )

    def _into_summary(self, item: Dict[str, Any]) -> TenderSummary:
        customer_name = (item.get("customer") or {}).get("name")
        return TenderSummary(
            tender_id=item.get("id"),
            name=_normalize_text(item.get("name")),
            unique_name=_normalize_text(item.get("unique_name")),
            start_price=_normalize_text(item.get("start_price")),
            required_percent=_normalize_text(item.get("required_percent")),
            placement_term=_normalize_text(item.get("placement_term")),
            complexity_category_id=item.get("complexity_category_id"),
            end_term_work_days=item.get("end_term_work_days"),
            customer_name=_normalize_text(customer_name),
            address=_normalize_text(item.get("address")),
        )

    def _merge_summaries(
        self, merged: Dict[str, TenderSummary], summaries: Iterable[TenderSummary]
    ) -> None:
        for summary in summaries:
            key = self._summary_key(summary)
            if key not in merged:
                merged[key] = summary

    def _summary_key(self, summary: TenderSummary) -> str:
        if summary.unique_name:
            return summary.unique_name
        return f"{summary.name}|{summary.customer_name}|{summary.address}"

    def _region_customer_matches(self, item: Dict[str, Any]) -> bool:
        customer_name = (item.get("customer") or {}).get("name")
        if not customer_name:
            return False
        normalized = _normalize_text(customer_name)
        if not normalized:
            return False
        return REGION_CUSTOMER_KEYWORD.lower() in normalized.lower()


def _parse_price(value: Optional[str]) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).replace(" ", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _format_currency(amount: Optional[Decimal]) -> str:
    if amount is None:
        return "–"
    try:
        as_int = int(amount.quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return "–"
    return f"{as_int:,}".replace(",", " ") + " UZS"


def _discount_percent(price: Optional[Decimal]) -> Optional[Decimal]:
    if price is None:
        return None
    thresholds = [
        (Decimal("102000000"), Decimal("0.05")),
        (Decimal("510000000"), Decimal("0.10")),
        (Decimal("1020000000"), Decimal("0.15")),
    ]
    for limit, percent in thresholds:
        if price < limit:
            return percent
    return Decimal("0.20")


def _format_percent(value: Optional[Decimal]) -> str:
    if value is None:
        return "–"
    percent = (value * 100).quantize(Decimal("1"))
    return f"{int(percent)}%"


def format_for_telegram(tenders: Iterable[TenderSummary]) -> List[str]:
    """Return human-readable snippets ready to send to Telegram."""
    lines: List[str] = []
    for tender in tenders:
        price = _parse_price(tender.start_price)
        discount = _discount_percent(price)
        offer_price = price - (price * discount) if price is not None and discount is not None else None

        if tender.tender_id is not None:
            tender_id_text = str(tender.tender_id)
            link = f"https://tender.mc.uz/tender-list/tender/{tender_id_text[-6:]}/view"
            id_line = f'<b>Лот рақами:</b> <a href="{link}">{tender.unique_name}</a>'
        else:
            id_line = f"<b>Лот рақами:</b> {tender.unique_name or '–'}"

        # FIX: use 'is not None' for integer fields so that 0 is shown correctly,
        # not swallowed by Python's falsy evaluation.
        complexity_str = str(tender.complexity_category_id) if tender.complexity_category_id is not None else "–"
        end_term_str = str(tender.end_term_work_days) if tender.end_term_work_days is not None else "–"

        block = (
            f"{id_line}\n"
            f"<b>Лойиҳа номи:</b> {tender.name or '–'}\n"
            f"<b>Лойиҳа манзили:</b> {tender.address or '–'}\n"
            f"<b>Бошланғич нарх:</b> {_format_currency(price)}\n"
            f"<b>Чегирма:</b> {_format_percent(discount)}\n"
            f"<b>Таклиф нархи:</b> {_format_currency(offer_price)}\n"
            f"<b>Мураккаблик тоифаси:</b> {complexity_str}\n"
            f"<b>Таклифларни топширишнинг охирги муддати:</b> {tender.placement_term or '–'}\n"
            f"<b>Ишларни бажариш муддати:</b> {end_term_str} кун\n"
            f"<b>Буюртмачи:</b> {tender.customer_name or '–'}"
        )
        lines.append(block)
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch filtered tenders for Telegram bot")
    parser.add_argument(
        "--region-id",
        type=int,
        choices=sorted(REGION_ALLOWLIST),
        help="Optional region to fetch (only 10 or 14 are supported)",
    )
    parser.add_argument(
        "--dump-json",
        action="store_true",
        help="Print the structured JSON payload instead of Telegram text",
    )
    parser.add_argument(
        "--show-response",
        action="store_true",
        help="Print the raw server JSON before filtering",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose logging to inspect API interactions",
    )
    parser.add_argument(
        "--single-request",
        action="store_true",
        help="Send only one request (default is three: regions 10 & 14 plus nationwide)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if not args.debug:
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    service = TenderService()
    if args.single_request:
        payload = service.fetch_raw(region_id=args.region_id)
        payloads_with_meta = [{"region_id": args.region_id, "payload": payload}]
        tenders = service.filter_payload(payload, region_id=args.region_id)
    else:
        if args.region_id is not None:
            parser.error("--region-id can only be used together with --single-request")
        tenders, payloads_with_meta = service.fetch_required_batches()

    if args.show_response:
        for meta in payloads_with_meta:
            header = f"=== Server response for region_id={meta['region_id']} ==="
            print(header)
            print(json.dumps(meta["payload"], ensure_ascii=False, indent=2))
            print()

    if args.dump_json:
        print(json.dumps([t.to_dict() for t in tenders], ensure_ascii=False, indent=2))
    else:
        formatted = format_for_telegram(tenders)
        if not formatted:
            print("No tenders matched the current filtering rules.")
            return
        for block in formatted:
            print(block)
            print("-" * 40)


if __name__ == "__main__":
    main()
