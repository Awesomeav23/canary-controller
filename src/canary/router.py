"""Shifts traffic between the stable and canary versions.

v1 strategy: two Deployments ({app}-stable, {app}-canary) behind one Service, so the
traffic split follows the replica ratio. Coarse but needs no service mesh. Every
method sets an absolute desired state, so calling it twice is harmless (idempotent).
"""

import logging
from typing import Protocol

log = logging.getLogger(__name__)

CONTAINER_NAME = "app"


def split_replicas(weight: int, total: int) -> tuple[int, int]:
    """Return (stable, canary) replica counts for a canary weight in percent."""
    if weight <= 0:
        return total, 0
    if weight >= 100:
        return 0, total
    canary = min(total - 1, max(1, round(total * weight / 100)))
    return total - canary, canary


class TrafficRouter(Protocol):
    def set_weight(self, app: str, image: str, weight: int, total: int) -> None: ...
    def promote(self, app: str, image: str, total: int) -> None: ...
    def rollback(self, app: str, total: int) -> None: ...


class LogRouter:
    """Logs the actions only. Lets you run the API without a cluster."""

    def set_weight(self, app: str, image: str, weight: int, total: int) -> None:
        stable, canary = split_replicas(weight, total)
        log.info("[%s] canary %s at %d%% (stable=%d canary=%d)", app, image, weight, stable, canary)

    def promote(self, app: str, image: str, total: int) -> None:
        log.info("[%s] promote %s to stable (%d replicas)", app, image, total)

    def rollback(self, app: str, total: int) -> None:
        log.info("[%s] rollback: canary -> 0, stable -> %d", app, total)


class KubernetesRouter:
    def __init__(self, namespace: str):
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self._apps = client.AppsV1Api()
        self._ns = namespace

    def _patch(self, name: str, replicas: int, image: str | None = None) -> None:
        spec: dict = {"replicas": replicas}
        if image:
            spec["template"] = {"spec": {"containers": [{"name": CONTAINER_NAME, "image": image}]}}
        self._apps.patch_namespaced_deployment(
            name,
            self._ns,
            {"spec": spec},
            # Strategic merge keys containers by name, so other container fields survive.
            _content_type="application/strategic-merge-patch+json",
        )

    def set_weight(self, app: str, image: str, weight: int, total: int) -> None:
        stable, canary = split_replicas(weight, total)
        # Scale up the canary before scaling down stable, so capacity never dips.
        self._patch(f"{app}-canary", canary, image)
        self._patch(f"{app}-stable", stable)

    def promote(self, app: str, image: str, total: int) -> None:
        self._patch(f"{app}-stable", total, image)
        self._patch(f"{app}-canary", 0)

    def rollback(self, app: str, total: int) -> None:
        self._patch(f"{app}-stable", total)
        self._patch(f"{app}-canary", 0)
