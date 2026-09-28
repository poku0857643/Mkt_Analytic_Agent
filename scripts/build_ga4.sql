-- Build the `ga4` dataset: analysis-ready tables from Google's public GA4 sample
-- (bigquery-public-data.ga4_obfuscated_sample_ecommerce, Google Merchandise Store,
-- 2020-11-01 to 2021-01-31).
--
-- The agent's query checks only allow datasets in our own project, so instead of
-- reading the public nested event tables directly, we flatten them once into four
-- tables whose descriptions define the metrics (a small semantic layer).
--
-- Run as a user who can create datasets (the app's service account cannot):
--   bq query --project_id=<project> --location=US --use_legacy_sql=false < scripts/build_ga4.sql
-- Scans about 4 GB of public data (within BigQuery's free monthly 1 TB).

CREATE SCHEMA IF NOT EXISTS ga4
OPTIONS (
  location = 'US',
  description = 'Google Merchandise Store web analytics, 2020-11-01 to 2021-01-31, built from the public GA4 obfuscated sample. Tables: sessions, purchases, purchase_items, users. There is no advertising cost data, so ROAS, CPC and CPA cannot be calculated.'
);

-- One row per event, with the parameters we use pulled out of the nested arrays.
CREATE TEMP TABLE ev AS
SELECT
  PARSE_DATE('%Y%m%d', event_date) AS event_date,
  TIMESTAMP_MICROS(event_timestamp) AS event_ts,
  event_name,
  user_pseudo_id,
  CONCAT(user_pseudo_id, '.', CAST(
    (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id') AS STRING
  )) AS session_key,
  (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'source') AS source,
  (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'medium') AS medium,
  (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'campaign') AS campaign,
  (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_location') AS page_location,
  (SELECT COALESCE(value.string_value, CAST(value.int_value AS STRING))
     FROM UNNEST(event_params) WHERE key = 'session_engaged') = '1' AS session_engaged,
  (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'engagement_time_msec') AS engagement_msec,
  device.category AS device_category,
  device.operating_system AS operating_system,
  device.web_info.browser AS browser,
  geo.country AS country,
  geo.region AS region,
  geo.city AS city,
  traffic_source.source AS first_source,
  traffic_source.medium AS first_medium,
  traffic_source.name AS first_campaign,
  user_first_touch_timestamp,
  ecommerce.transaction_id AS transaction_id,
  ecommerce.purchase_revenue_in_usd AS purchase_revenue_usd,
  ecommerce.total_item_quantity AS item_quantity,
  IF(event_name = 'purchase', items, []) AS items
FROM `bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*`;

-- Session attributes: source/medium/campaign from the first event that has them.
CREATE TEMP TABLE sess AS
SELECT
  session_key,
  ANY_VALUE(user_pseudo_id) AS user_pseudo_id,
  MIN(event_ts) AS session_start,
  ARRAY_AGG(source IGNORE NULLS ORDER BY event_ts LIMIT 1)[SAFE_OFFSET(0)] AS source,
  ARRAY_AGG(medium IGNORE NULLS ORDER BY event_ts LIMIT 1)[SAFE_OFFSET(0)] AS medium,
  ARRAY_AGG(campaign IGNORE NULLS ORDER BY event_ts LIMIT 1)[SAFE_OFFSET(0)] AS campaign,
  ARRAY_AGG(STRUCT(device_category, operating_system, browser, country, region, city)
            ORDER BY event_ts LIMIT 1)[OFFSET(0)] AS first_event,
  ARRAY_AGG(IF(event_name = 'page_view', page_location, NULL) IGNORE NULLS
            ORDER BY event_ts LIMIT 1)[SAFE_OFFSET(0)] AS landing_url,
  LOGICAL_OR(event_name = 'first_visit') AS is_new_user,
  LOGICAL_OR(COALESCE(session_engaged, FALSE)) AS engaged,
  COUNTIF(event_name = 'page_view') AS page_views,
  COALESCE(SUM(engagement_msec), 0) / 1000 AS engagement_seconds,
  LOGICAL_OR(event_name = 'view_item') AS viewed_item,
  LOGICAL_OR(event_name = 'add_to_cart') AS added_to_cart,
  LOGICAL_OR(event_name = 'begin_checkout') AS began_checkout
FROM ev
WHERE session_key IS NOT NULL
GROUP BY session_key;

CREATE TEMP FUNCTION channel(source STRING, medium STRING) AS (
  CASE
    WHEN medium = 'organic' THEN 'Organic Search'
    WHEN medium IN ('cpc', 'ppc', 'paid') THEN 'Paid Search'
    WHEN source = '(direct)' OR medium = '(none)' THEN 'Direct'
    WHEN medium = 'referral' THEN 'Referral'
    WHEN medium = 'affiliate' THEN 'Affiliates'
    WHEN medium = 'email' THEN 'Email'
    ELSE 'Unknown'
  END
);

-- Purchases, de-duplicated: the sample logs some transactions more than once.
-- Purchases without a transaction ID are each kept as their own purchase.
CREATE TEMP TABLE purch AS
SELECT * EXCEPT (rn)
FROM (
  SELECT
    IF(transaction_id IS NULL OR transaction_id = '(not set)',
       CONCAT('unset-', session_key, '-', CAST(UNIX_MICROS(event_ts) AS STRING)),
       transaction_id) AS purchase_id,
    event_date, event_ts, user_pseudo_id, session_key,
    COALESCE(purchase_revenue_usd, 0) AS revenue_usd,
    item_quantity,
    items,
    ROW_NUMBER() OVER (
      PARTITION BY IF(transaction_id IS NULL OR transaction_id = '(not set)',
                      CONCAT(session_key, '-', CAST(UNIX_MICROS(event_ts) AS STRING)),
                      transaction_id)
      ORDER BY event_ts
    ) AS rn
  FROM ev
  WHERE event_name = 'purchase'
)
WHERE rn = 1;

CREATE OR REPLACE TABLE ga4.sessions (
  session_key STRING OPTIONS (description = 'Unique session ID. Sessions = COUNT(*).'),
  user_pseudo_id STRING OPTIONS (description = 'Anonymous browser ID (not personal data). Users = COUNT(DISTINCT user_pseudo_id). Joins to users.'),
  session_date DATE OPTIONS (description = 'Date the session started (UTC).'),
  session_start TIMESTAMP OPTIONS (description = 'Time of the first event in the session.'),
  channel STRING OPTIONS (description = 'Marketing channel from source/medium: Organic Search, Paid Search, Direct, Referral, Affiliates, Email, Unknown. Unknown covers obfuscated or missing sources.'),
  source STRING OPTIONS (description = 'Traffic source, e.g. google, (direct). "<Other>" and "(data deleted)" are privacy placeholders in the public sample, not real sources; "(not set)" means no source was recorded.'),
  medium STRING OPTIONS (description = 'Traffic medium, e.g. organic, cpc, referral, (none). Same placeholders as source.'),
  campaign STRING OPTIONS (description = 'Campaign name, e.g. (organic), (direct), Data Share Promo. Same placeholders as source.'),
  device_category STRING OPTIONS (description = 'desktop, mobile or tablet.'),
  operating_system STRING,
  browser STRING,
  country STRING,
  region STRING,
  city STRING,
  landing_page STRING OPTIONS (description = 'Path of the first page viewed, without query string, e.g. /home.'),
  is_new_user BOOL OPTIONS (description = 'TRUE if this was the user\'s first visit.'),
  engaged BOOL OPTIONS (description = 'Engaged session (GA4 definition). Engagement rate = COUNTIF(engaged) / COUNT(*).'),
  page_views INT64,
  engagement_seconds FLOAT64 OPTIONS (description = 'Total foreground engagement time in the session, in seconds.'),
  viewed_item BOOL OPTIONS (description = 'Funnel step 1: viewed a product page.'),
  added_to_cart BOOL OPTIONS (description = 'Funnel step 2: added to cart.'),
  began_checkout BOOL OPTIONS (description = 'Funnel step 3: started checkout.'),
  purchased BOOL OPTIONS (description = 'Funnel step 4: made at least one purchase. Conversion rate = COUNTIF(purchased) / COUNT(*).'),
  transactions INT64 OPTIONS (description = 'De-duplicated purchases in the session.'),
  revenue_usd FLOAT64 OPTIONS (description = 'Purchase revenue in USD in the session (de-duplicated). Revenue per session = SUM(revenue_usd) / COUNT(*).')
)
OPTIONS (
  description = 'One row per website session, Google Merchandise Store, 2020-11-01 to 2021-01-31 (public GA4 sample). Use for traffic, channel, device, geography, engagement and funnel questions. Conversion rate = sessions with a purchase / sessions. No ad cost data: ROAS, CPC and CPA cannot be calculated.'
)
AS
SELECT
  s.session_key,
  s.user_pseudo_id,
  DATE(s.session_start) AS session_date,
  s.session_start,
  channel(s.source, s.medium) AS channel,
  COALESCE(s.source, '(not set)') AS source,
  COALESCE(s.medium, '(not set)') AS medium,
  COALESCE(s.campaign, '(not set)') AS campaign,
  s.first_event.device_category,
  s.first_event.operating_system,
  s.first_event.browser,
  s.first_event.country,
  s.first_event.region,
  s.first_event.city,
  REGEXP_EXTRACT(s.landing_url, r'^https?://[^/]+(/[^?#]*)') AS landing_page,
  s.is_new_user,
  s.engaged,
  s.page_views,
  s.engagement_seconds,
  s.viewed_item,
  s.added_to_cart,
  s.began_checkout,
  COALESCE(p.transactions, 0) > 0 AS purchased,
  COALESCE(p.transactions, 0) AS transactions,
  COALESCE(p.revenue_usd, 0) AS revenue_usd
FROM sess AS s
LEFT JOIN (
  SELECT session_key, COUNT(*) AS transactions, SUM(revenue_usd) AS revenue_usd
  FROM purch GROUP BY session_key
) AS p USING (session_key);

CREATE OR REPLACE TABLE ga4.purchases (
  purchase_id STRING OPTIONS (description = 'Transaction ID; "unset-..." when the sample has none. Purchases (orders) = COUNT(*).'),
  purchase_date DATE,
  purchase_time TIMESTAMP,
  user_pseudo_id STRING OPTIONS (description = 'Anonymous browser ID. Joins to users and sessions.'),
  session_key STRING OPTIONS (description = 'Session the purchase happened in. Joins to sessions.'),
  revenue_usd FLOAT64 OPTIONS (description = 'Order revenue in USD. Revenue = SUM(revenue_usd). Average order value = AVG(revenue_usd).'),
  item_quantity INT64 OPTIONS (description = 'Total units in the order.'),
  channel STRING OPTIONS (description = 'Channel of the session the purchase happened in (see sessions.channel).'),
  source STRING,
  medium STRING,
  campaign STRING,
  device_category STRING,
  country STRING
)
OPTIONS (
  description = 'One row per purchase (order), de-duplicated by transaction ID, 2020-11-01 to 2021-01-31. Use for revenue, orders and average order value by date, channel, device or country.'
)
AS
SELECT
  p.purchase_id, p.event_date, p.event_ts, p.user_pseudo_id, p.session_key,
  p.revenue_usd, p.item_quantity,
  s.channel, s.source, s.medium, s.campaign, s.device_category, s.country
FROM purch AS p
LEFT JOIN ga4.sessions AS s USING (session_key);

CREATE OR REPLACE TABLE ga4.purchase_items (
  purchase_id STRING OPTIONS (description = 'Joins to purchases.'),
  purchase_date DATE,
  item_id STRING,
  item_name STRING OPTIONS (description = 'Product name, e.g. Google Land & Sea Cotton Cap.'),
  item_brand STRING,
  item_category STRING OPTIONS (description = 'Product category, e.g. Apparel, Bags, Drinkware.'),
  item_variant STRING,
  price_usd FLOAT64 OPTIONS (description = 'Unit price in USD.'),
  quantity INT64 OPTIONS (description = 'Units bought. Units sold = SUM(quantity).'),
  item_revenue_usd FLOAT64 OPTIONS (description = 'Revenue for this line in USD. Product revenue = SUM(item_revenue_usd).')
)
OPTIONS (
  description = 'One row per product line in each purchase. Use for top products, categories and units sold.'
)
AS
SELECT
  p.purchase_id,
  p.event_date,
  i.item_id, i.item_name, i.item_brand, i.item_category, i.item_variant,
  i.price_in_usd, i.quantity, i.item_revenue_in_usd
-- Items come from the one purchase event kept above, so duplicates are not double counted.
FROM purch AS p
CROSS JOIN UNNEST(p.items) AS i;

CREATE OR REPLACE TABLE ga4.users (
  user_pseudo_id STRING OPTIONS (description = 'Anonymous browser ID (not personal data). Users = COUNT(*).'),
  first_touch_date DATE OPTIONS (description = 'Date of the user\'s first ever visit; can be before 2020-11-01.'),
  first_seen_date DATE OPTIONS (description = 'Date of the user\'s first session in this data. Use as the cohort date for retention: join sessions and compare session_date.'),
  last_seen_date DATE,
  first_channel STRING OPTIONS (description = 'Channel that first acquired the user (see sessions.channel).'),
  first_source STRING OPTIONS (description = 'Source that first acquired the user. Placeholders as in sessions.source.'),
  first_medium STRING,
  first_campaign STRING,
  country STRING OPTIONS (description = 'Country of the user\'s first session.'),
  device_category STRING OPTIONS (description = 'Device of the user\'s first session.'),
  sessions INT64,
  purchases INT64,
  revenue_usd FLOAT64 OPTIONS (description = 'Revenue from the user in this period, USD.')
)
OPTIONS (
  description = 'One row per user (anonymous browser), 2020-11-01 to 2021-01-31. Use for new vs returning users, acquisition channel, cohorts and customer value.'
)
AS
WITH first_touch AS (
  SELECT
    user_pseudo_id,
    DATE(TIMESTAMP_MICROS(MIN(user_first_touch_timestamp))) AS first_touch_date,
    ARRAY_AGG(STRUCT(first_source, first_medium, first_campaign)
              ORDER BY event_ts LIMIT 1)[OFFSET(0)] AS ft
  FROM ev
  GROUP BY user_pseudo_id
),
per_user AS (
  SELECT
    user_pseudo_id,
    MIN(session_date) AS first_seen_date,
    MAX(session_date) AS last_seen_date,
    ARRAY_AGG(STRUCT(country, device_category) ORDER BY session_start LIMIT 1)[OFFSET(0)] AS first_session,
    COUNT(*) AS sessions,
    SUM(transactions) AS purchases,
    SUM(revenue_usd) AS revenue_usd
  FROM ga4.sessions
  GROUP BY user_pseudo_id
)
SELECT
  u.user_pseudo_id,
  f.first_touch_date,
  u.first_seen_date,
  u.last_seen_date,
  channel(f.ft.first_source, f.ft.first_medium),
  COALESCE(f.ft.first_source, '(not set)'),
  COALESCE(f.ft.first_medium, '(not set)'),
  COALESCE(f.ft.first_campaign, '(not set)'),
  u.first_session.country,
  u.first_session.device_category,
  u.sessions,
  u.purchases,
  u.revenue_usd
FROM per_user AS u
LEFT JOIN first_touch AS f USING (user_pseudo_id);
