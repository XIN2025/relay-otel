from __future__ import annotations

EXPECTED_STACK: dict[str, dict[str, str]] = {
    "ingester": {
        "container": "relay-otel-signoz-ingester-1",
        "image": "signoz/signoz-otel-collector",
        "lifecycle": "running_healthy",
    },
    "relay-otel-signoz-signoz-0": {
        "container": "relay-otel-signoz-signoz-0",
        "image": "signoz/signoz",
        "lifecycle": "running_healthy",
    },
    "relay-otel-signoz-telemetrykeeper-clickhousekeeper-0": {
        "container": "relay-otel-signoz-telemetrykeeper-clickhousekeeper-0",
        "image": "clickhouse/clickhouse-keeper",
        "lifecycle": "running_healthy",
    },
    "relay-otel-signoz-telemetrystore-clickhouse-0-0": {
        "container": "relay-otel-signoz-telemetrystore-clickhouse-0-0",
        "image": "clickhouse/clickhouse-server",
        "lifecycle": "running_healthy",
    },
    "relay-otel-signoz-telemetrystore-clickhouse-user-scripts": {
        "container": "relay-otel-signoz-telemetrystore-clickhouse-user-scripts",
        "image": "clickhouse/clickhouse-server",
        "lifecycle": "completed_successfully",
    },
    "relay-otel-signoz-telemetrystore-migrator": {
        "container": "relay-otel-signoz-telemetrystore-migrator",
        "image": "signoz/signoz-otel-collector",
        "lifecycle": "completed_successfully",
    },
}
