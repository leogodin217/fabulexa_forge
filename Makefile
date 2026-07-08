# Run all targets through `uv run` / `make` so tooling resolves this project's own
# standalone venv (only this project's own dependencies are installed there).

.PHONY: check lint typecheck test kafka-up kafka-down kafka-it kafka-ui \
        demo demo-emit mixer-demo board

KAFKA_COMPOSE = dev/kafka/docker-compose.yml

check: lint typecheck test

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy src

test:
	uv run pytest

# Kafka dev/integration rig (dev/kafka/README.md). Not part of `check` — the rig
# needs a broker; without one, its tests skip.
kafka-up:
	docker compose -f $(KAFKA_COMPOSE) up -d --wait

# Activate the `ui` profile so the profiled kafka-ui container is torn down too;
# without it `down` leaves kafka-ui orphaned, and its stale network reference
# breaks the next `kafka-ui` start. --remove-orphans sweeps any stragglers.
kafka-down:
	docker compose -f $(KAFKA_COMPOSE) --profile ui down -v --remove-orphans

kafka-it:
	uv run pytest -m kafka tests/integration

# Broker + read-only web UI (kafka-ui, compose profile `ui`). Opt-in — the
# default `kafka-up`/`kafka-it` rig never starts it. Browse at localhost:8080.
kafka-ui:
	docker compose -f $(KAFKA_COMPOSE) --profile ui up -d --wait
	@echo "kafka-ui: http://localhost:8080"

# --- FabulMixer live-perform demo --------------------------------------------
# Three long-running pieces, each in its own terminal: the Kafka broker
# (background docker), the mixer driver (foreground server), and the board
# (foreground vite). `make demo` prints the run order; the rest each own one
# piece. Overridable: BOOTSTRAP, MIXER_FLAGS.
DEMO_EMIT      = dev/demo/emit
DEMO_CONFIG    = dev/demo/config.yaml
BOOTSTRAP      = localhost:9092
# Launch the consumer instrument: 12-hour event-time windows + an admission(fact)
# -> patient(dim) enrichment join. Launch is paused; drive play/speed/lag/mute/
# ingest-rate live from the board.
MIXER_FLAGS    = --consumer --window 43200000 --join admission:patient

demo:
	@echo "FabulMixer live-perform demo — run these in three terminals:"
	@echo "  1) make kafka-up      # start the broker (background, docker)"
	@echo "  2) make mixer-demo    # the mixer driver, serving the control API on :8765"
	@echo "  3) make board         # the live-perform board on http://localhost:5173"
	@echo "Then open http://localhost:5173, press play, and drive the sliders."
	@echo "Tear down with: make kafka-down"
	@echo ""
	@echo "Or run a bundled example: make mixer-demo EXAMPLE=<name>"
	@echo "  Available: ride-sharing-marketplace, ride-sharing, retail, nhs"

# Materialize a real emit (run.duckdb + base.json) into dev/demo/emit (gitignored).
demo-emit:
	uv run python dev/demo/build_emit.py $(DEMO_EMIT)

# The mixer driver: replay the demo emit as a live, operator-mixable Kafka feed.
# Foreground; assumes `make kafka-up` has run. Serves the control API on :8765
# (the port the board proxies to). Ctrl-C to stop.
# Set EXAMPLE=<name> to use a bundled preset instead of the fixture path.
ifdef EXAMPLE
mixer-demo:
	dev/demo/run.sh $(EXAMPLE)
else
mixer-demo: demo-emit
	uv run fabexport mixer $(DEMO_EMIT) $(DEMO_CONFIG) \
	  --fmt jsonl --bootstrap-servers $(BOOTSTRAP) $(MIXER_FLAGS)
endif

# The live-perform board against the real backend (proxies /api -> :8765).
# Foreground vite dev server on http://localhost:5173. Ctrl-C to stop.
board:
	cd frontend && npm install && VITE_API_MODE=http npm run dev
