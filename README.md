Marketing Analytics Agent on BigQuery MCP, with Measured security

1. Scenario:
   - a marketing team asks natural-language questions about channel performance, conversion funnels, and cohort retention
2. Data: 
   - The public GA4 obfuscated e-commedce sample in BigQuery
3. Hypothesis: 
   - an ADK agent that uses the managed BigQuery MCP server plus a small semantic layer (documented metric definitions and 15-20 verified example queries) reaches higher execution accuracy than a baseline agent with only raw schema access, and the AS101-AS103 controls cut injection attack success without a meaningful accuracy loss
4. Baseline:
   - the same model and MCP tools, with schema only and no controls
5. Variables:
   - semantic layer on or off
   - security controls on or off (read-only service account, byte cap, tool allow-list, a tool0output injection filter such as Model Armor)
6. Metrics:
   - execution accuracy on 60 gold questions written from a mock stakeholder interview;
   - cost per answered question (tokens plus bytes scanned)
   - p95 latency
   - attack success rate on 30 injection cases (for example, malicious text planted in a table the agent reads)
7. Stage mapping:
   - Discover (week 1: interview script, question bank) -> scope (acceptance threshold e.g., at least 85% accuracy and at most 5% attack success)
   -> integrate (ADK plus MCP)
   -> Deploy (Cloud Run)
   -> Measure and hand off (Langfuse or Phoenix dashboard, runbook, a one-page "deployment memo").

(3 weeks)

