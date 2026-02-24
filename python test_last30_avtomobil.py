from datetime import datetime, timedelta
from tender_service import TenderService


def filter_last_120_days(items):
    # Use date-only comparison (no strict ISO parsing)
    cutoff_date = (datetime.utcnow() - timedelta(days=120)).date()
    result = []

    for item in items:
        confirmed = item.get("confirmed_date")
        if not confirmed:
            continue

        # Extract only YYYY-MM-DD part safely
        date_part = str(confirmed)[:10]

        try:
            confirmed_date = datetime.strptime(date_part, "%Y-%m-%d").date()
        except Exception:
            continue

        if confirmed_date >= cutoff_date:
            result.append(item)

    return result


def main():
    service = TenderService()

    # Fetch region 10 example
    payload = service.fetch_raw(region_id=10)

    items = payload.get("result", {}).get("data", [])

    # Filter last 120 days
    last120 = filter_last_120_days(items)

    print(f"\nTotal items from API: {len(items)}")
    print(f"Items from last 120 days: {len(last120)}")

    # Apply AVTOMOBIL filter manually
    avtomobil_items = [
        item for item in last120
        if service._customer_contains_avtomobil(item)
    ]

    print(f"Items with AVTOMOBIL (last 120 days): {len(avtomobil_items)}\n")

    # Convert to summaries for nice output
    summaries = [service._into_summary(item) for item in avtomobil_items]

    for s in summaries:
        print("------------------------------------------------")
        print("ID:", s.tender_id)
        print("Customer:", s.customer_name)
        print("Name:", s.name)


if __name__ == "__main__":
    main()
