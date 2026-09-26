"""Create sandbox datasets with fake marketing data for integration tests.

Usage: python scripts/seed_sandbox.py <gcp-project>

Creates (or replaces the tables in) two datasets:
  marketing: campaigns, ad_spend_daily, conversions
  customers: customers  (fake PII, to exercise the PII guardrail)

Data is generated from a fixed seed, so generate_data() returns the same rows
every time and tests can compute expected answers from it. Uses load jobs,
which are free and work in the BigQuery sandbox (no billing needed).
"""
import datetime
import random
import sys
from decimal import Decimal

from google.cloud import bigquery

LOCATION = "US"
SEED = 20260926

CHANNELS = ["search", "social", "email", "display", "video"]
FIRST_NAMES = ["Ava", "Ben", "Chen", "Dana", "Eli", "Fatima", "Goro", "Hana", "Ivan", "Jo"]
LAST_NAMES = ["Kim", "Lopez", "Nguyen", "Okafor", "Patel", "Rossi", "Smith", "Tanaka"]
COUNTRIES = ["US", "CA", "GB", "DE", "JP", "TW"]

SCHEMAS = {
    "marketing.campaigns": (
        "One row per marketing campaign.",
        [
            bigquery.SchemaField("campaign_id", "INT64", "REQUIRED"),
            bigquery.SchemaField("name", "STRING", description="Campaign name"),
            bigquery.SchemaField("channel", "STRING", description=", ".join(CHANNELS)),
            bigquery.SchemaField("start_date", "DATE"),
            bigquery.SchemaField("end_date", "DATE"),
            bigquery.SchemaField("budget", "NUMERIC", description="Total budget in USD"),
        ],
    ),
    "marketing.ad_spend_daily": (
        "Daily delivery and spend per campaign.",
        [
            bigquery.SchemaField("date", "DATE", "REQUIRED"),
            bigquery.SchemaField("campaign_id", "INT64", "REQUIRED"),
            bigquery.SchemaField("impressions", "INT64"),
            bigquery.SchemaField("clicks", "INT64"),
            bigquery.SchemaField("spend", "NUMERIC", description="Spend in USD"),
        ],
    ),
    "marketing.conversions": (
        "One row per attributed conversion.",
        [
            bigquery.SchemaField("conversion_id", "INT64", "REQUIRED"),
            bigquery.SchemaField("campaign_id", "INT64", "REQUIRED"),
            bigquery.SchemaField("customer_id", "INT64"),
            bigquery.SchemaField("conversion_date", "DATE"),
            bigquery.SchemaField("revenue", "NUMERIC", description="Revenue in USD"),
        ],
    ),
    "customers.customers": (
        "Customer records. email and phone are PII.",
        [
            bigquery.SchemaField("customer_id", "INT64", "REQUIRED"),
            bigquery.SchemaField("name", "STRING"),
            bigquery.SchemaField("email", "STRING", description="PII"),
            bigquery.SchemaField("phone", "STRING", description="PII"),
            bigquery.SchemaField("country", "STRING"),
            bigquery.SchemaField("signup_date", "DATE"),
        ],
    ),
}


def generate_data() -> dict[str, list[dict]]:
    rng = random.Random(SEED)
    base = datetime.date(2026, 4, 1)

    campaigns = []
    for cid in range(1, 21):
        start = base + datetime.timedelta(days=rng.randint(0, 120))
        channel = CHANNELS[cid % len(CHANNELS)]
        campaigns.append(
            {
                "campaign_id": cid,
                "name": f"{channel.title()} Campaign {cid}",
                "channel": channel,
                "start_date": start,
                "end_date": start + datetime.timedelta(days=rng.randint(14, 60)),
                "budget": Decimal(rng.randint(5, 50) * 1000),
            }
        )

    spend = []
    for c in campaigns:
        day = c["start_date"]
        while day <= c["end_date"]:
            impressions = rng.randint(1_000, 50_000)
            clicks = int(impressions * rng.uniform(0.005, 0.05))
            spend.append(
                {
                    "date": day,
                    "campaign_id": c["campaign_id"],
                    "impressions": impressions,
                    "clicks": clicks,
                    "spend": Decimal(str(round(clicks * rng.uniform(0.3, 2.5), 2))),
                }
            )
            day += datetime.timedelta(days=1)

    customers = []
    for cust_id in range(1, 501):
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        customers.append(
            {
                "customer_id": cust_id,
                "name": f"{first} {last}",
                "email": f"{first.lower()}.{last.lower()}{cust_id}@example.com",
                "phone": f"+1-555-{cust_id:04d}",
                "country": rng.choice(COUNTRIES),
                "signup_date": base - datetime.timedelta(days=rng.randint(0, 700)),
            }
        )

    conversions = []
    for conv_id in range(1, 2001):
        c = rng.choice(campaigns)
        span = (c["end_date"] - c["start_date"]).days
        conversions.append(
            {
                "conversion_id": conv_id,
                "campaign_id": c["campaign_id"],
                "customer_id": rng.randint(1, 500),
                "conversion_date": c["start_date"] + datetime.timedelta(days=rng.randint(0, span)),
                "revenue": Decimal(str(round(rng.uniform(10, 400), 2))),
            }
        )

    return {
        "marketing.campaigns": campaigns,
        "marketing.ad_spend_daily": spend,
        "marketing.conversions": conversions,
        "customers.customers": customers,
    }


def _to_json(row: dict) -> dict:
    return {
        k: str(v) if isinstance(v, (Decimal, datetime.date)) else v for k, v in row.items()
    }


def seed(project: str) -> None:
    client = bigquery.Client(project=project)
    data = generate_data()

    for dataset in sorted({name.split(".")[0] for name in SCHEMAS}):
        ds = bigquery.Dataset(f"{project}.{dataset}")
        ds.location = LOCATION
        client.create_dataset(ds, exists_ok=True)

    for name, (description, schema) in SCHEMAS.items():
        table_id = f"{project}.{name}"
        config = bigquery.LoadJobConfig(
            schema=schema,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        rows = [_to_json(r) for r in data[name]]
        client.load_table_from_json(rows, table_id, job_config=config).result()

        table = client.get_table(table_id)
        table.description = description
        client.update_table(table, ["description"])
        print(f"{name}: {table.num_rows} rows")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/seed_sandbox.py <gcp-project>")
    seed(sys.argv[1])
