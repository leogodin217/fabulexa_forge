# Kafka dev / integration rig

Pre-work for the **streaming export** Kafka sink (feature `streaming-export`,
pre-work scope `streaming-export-kafka-debezium-pre-work-scope`). It de-risks the
three Kafka mechanics the Debezium envelope leans on — **before the sink adapter
exists** — black-box and decoupled from the `writers/` sink seam that S1 is still
defining.

## What it proves

Three guarantees from `streaming-debezium-envelope-contract`, asserted against a
real broker:

1. **Key = `record_id`** — every message key is `{ "record_id": <id> }`; per-entity
   ordering / partitioning.
2. **Single-partition global order** — the derived total order (`source.lsn` = the
   coalesced `seq`) survives end-to-end, strictly monotonic in consume order.
3. **Message timestamp = rebased event time** — the Kafka record timestamp equals
   the payload `ts_ms` (= `source.ts_ms`), never broker append time.

Plus the **upsert-log** shape (first message per `record_id` is `op:c`, the rest
`op:u`; `before` always null) and the `schemas.enable` wrapper toggle.

## What it is not

- **No Schema Registry.** The format is JSON with a `schemas.enable` toggle; Avro +
  Confluent Schema Registry is deferred (see the decision note). Broker only.
- **Not the sink adapter.** The real Kafka sink + Debezium renderer are deferred to
  streaming S2/S3 — they plug into the `writers/` seam S1 defines, so building them
  now would code against a guessed interface. The rig feeds Kafka from **hand-authored
  canned envelopes** (`tests/integration/kafka/_envelopes.py`) that conform to the
  pinned contract; those fixtures become the renderer's golden outputs when S2 lands.

## Prerequisites

- Docker (with `docker compose` v2).
- `confluent-kafka` in the dev venv: `uv sync` installs it.

## Run

```bash
make kafka-up      # start the broker, wait for healthy
make kafka-it      # run the black-box validator (uv run pytest -m kafka tests/integration)
make kafka-down    # stop + remove the broker and its volumes
```

The validator is gated by the `kafka` pytest marker and **skips itself** when the
broker is unreachable, so `make check` stays docker-free and green. Override the
bootstrap address with `FABEXPORT_KAFKA_BOOTSTRAP` (default `localhost:9092`).

## Watching the data (optional web UI)

Two ways to see messages flow:

- **No install — console consumer** (shipped in the broker image). Shows the three
  guarantees above directly — key, consume order, record timestamp:

  ```bash
  docker exec fabexport-kafka /opt/kafka/bin/kafka-console-consumer.sh \
    --bootstrap-server localhost:9092 --topic <topic> --from-beginning \
    --property print.key=true --property print.timestamp=true
  ```

- **Web UI — kafka-ui**, opt-in behind the `ui` compose profile so the default
  broker-only rig is untouched:

  ```bash
  make kafka-ui      # broker + kafka-ui, browse http://localhost:8080
  make kafka-down    # tears down both
  ```

  The UI runs inside the compose network and reaches the broker on its second,
  in-network advertised listener (`kafka:29092`); the host-facing
  `localhost:9092` listener the validator uses is unchanged. The UI is read-only
  for observation — it makes no ordering or partitioning claims of its own.
