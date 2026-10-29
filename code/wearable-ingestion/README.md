# Study-agnostic wearable ingestion prototype

A small reference implementation for the part of a digital-health data system that is easy to underestimate: accepting repeated provider events, keeping an immutable copy of what arrived, and making each downstream processing job traceable to a study, participant, session, stream, and source hash.

## Run the synthetic demo

```bash
python pipeline.py ingest --root ./demo-lake --event sample-event.json --data sample-stream.csv
python pipeline.py ingest --root ./demo-lake --event sample-event.json --data sample-stream.csv
python pipeline.py work --root ./demo-lake
python pipeline.py status --root ./demo-lake
python pipeline.py retry --root ./demo-lake
```

The second ingest is intentionally a duplicate: the `(tenant, provider, event_id)` key makes it idempotent. Raw payloads are stored under tenant/study/participant/session/stream partitions by content hash. The local worker validates time-series rows, records malformed rows, and writes normalized long-form data plus a job summary. Failed jobs can be requeued after the cause is addressed. The database is only a local queue/catalog stand-in.

## System boundary

```text
Wearable / provider adapter
        ↓ event + payload
Authenticated ingress → durable queue → immutable object storage
                                  ↓
                           cloud workers
                                  ↓
                   normalized study partitions
```

In a cloud deployment, ingress would verify webhook authentication, map a provider identity to an internal pseudonymous participant, persist the event and raw payload, then return quickly. A managed queue would trigger retryable workers; workers would write derived partitions and QC summaries without moving the raw object. The prototype runs these steps locally so the contract and idempotency behavior are inspectable. It does not deploy cloud resources.

## Connector model

Provider adapters should translate source-specific events into the small envelope in `sample-event.json`, then pass the fetched payload to the same ingest path. Terra is one possible source: its documented Destinations include webhooks and other delivery targets, and its event references can support request/event reconciliation. This repository does not authenticate to Terra, receive webhooks, fetch `payload_url` data, or implement a production Terra adapter. Those are explicit next steps, not claimed capabilities.

## Why this can span studies

Study and participant identity are part of the storage and job keys instead of being encoded in one study-specific script. That lets the same ingress and worker contract handle many studies while keeping each record traceable to its source and processing version. In a real system, tenant isolation, access policy, consent/revocation handling, encryption, retention, audit, and validated provider mappings are additional requirements; this demo is not a compliant clinical platform and must use synthetic data only.

## Current limits

The local prototype handles timestamped CSV time-series payloads with numeric channels. It uses SQLite and the local filesystem, and has a single worker process. It does not implement cloud queues/object stores, webhook signatures, encryption, PHI handling, study-specific clinical transformations, BIDS export, Terra API credentials, or production-grade retry/dead-letter policy. The cloud architecture is a design target; the CLI is the runnable part.

## Tests

```bash
python -m unittest discover -s tests -v
```
