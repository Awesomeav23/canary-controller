"""Prometheus metrics about the controller itself, served at GET /metrics.

Useful queries:
  Rollback rate (7d):  sum(increase(canary_rollouts_finished_total{outcome="rolled_back"}[7d]))
                         / sum(increase(canary_rollouts_finished_total[7d]))
  p50 time to rollback: histogram_quantile(0.5, sum by (le) (
                          rate(canary_rollout_duration_seconds_bucket{outcome="rolled_back"}[7d])))
  Stuck controller:    rate(canary_advance_errors_total[5m]) > 0
"""

from prometheus_client import Counter, Gauge, Histogram

ROLLOUTS_FINISHED = Counter(
    "canary_rollouts_finished_total",
    "Rollouts that reached a final state",
    ["app", "outcome"],  # outcome: succeeded | rolled_back | aborted
)
ROLLOUT_DURATION = Histogram(
    "canary_rollout_duration_seconds",
    "From the canary's first traffic to its final state; outcome=rolled_back is time to rollback",
    ["outcome"],
    buckets=(30, 60, 90, 120, 180, 240, 300, 600, 900, 1800, 3600),
)
ANALYSES = Counter(
    "canary_analyses_total",
    "Step analyses by result",
    ["app", "result"],  # result: healthy | unhealthy | waiting_for_traffic
)
ACTIVE_ROLLOUTS = Gauge("canary_active_rollouts", "Rollouts pending or progressing")
TICK_DURATION = Histogram(
    "canary_tick_duration_seconds",
    "Time to process one controller tick",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
ADVANCE_ERRORS = Counter(
    "canary_advance_errors_total", "Unexpected exceptions while advancing a rollout"
)
NOTIFICATIONS = Counter(
    "canary_notifications_total",
    "Notification delivery attempts",
    ["sink", "result"],  # result: sent | retry | failed
)
