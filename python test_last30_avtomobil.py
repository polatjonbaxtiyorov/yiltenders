from datetime import datetime, timedelta
from tender_service import TenderService

def filter_last_30_days(items):
    cutoff = datetime.utcnow() - timedelta(days=60)
    result = []

    for item in items:
        confirmed = item.get("confirmed_date")
        if not confirmed:
            continue

        try:
            confirmed_dt = datetime.fromisoformat(
                confirmed.replace("Z", "+00:00")
            )
        except Exception:
            continue

        if confirmed_dt >= cutoff:
            result.append(item)

    return result


def main():
    service = TenderService()

    # Fetch normally (region 10 example)
    payload = service.fetch_raw(region_id=10)

    items = payload.get("result", {}).get("data", [])

    # Filter last 30 days
    last30 = filter_last_30_days(items)

    print(f"\nTotal items from API: {len(items)}")
    print(f"Items from last 30 days: {len(last30)}")

    # Apply AVTOMOBIL filter manually
    avtomobil_items = [
        item for item in last30
        if service._customer_contains_avtomobil(item)
    ]

    print(f"Items with AVTOMOBIL (last 30 days): {len(avtomobil_items)}\n")

    # Convert to summaries for nice output
    summaries = [service._into_summary(item) for item in avtomobil_items]

    for s in summaries:
        print("------------------------------------------------")
        print("ID:", s.tender_id)
        print("Customer:", s.customer_name)
        print("Date:", s.placement_term)
        print("Name:", s.name)


if __name__ == "__main__":
    main()
