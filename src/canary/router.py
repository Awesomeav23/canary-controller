"""Shifts traffic between the stable and canary versions.

Two strategies, both using Deployments named {app}-stable and {app}-canary:

ReplicaRouter  One Service in front of both, so traffic follows the pod ratio. Needs
               nothing extra, but coarse: with 4 pods the smallest step is 25%.
GatewayRouter  A Gateway API HTTPRoute splits traffic by weight between {app}-stable
               and {app}-canary Services. Any percentage, and stable stays full size
               throughout, so rollback is an instant route change.

Every method sets an absolute desired state, so calling it again is harmless
(idempotent). set_weight and promote return False when the change can't finish yet
(pods still starting); the engine just calls them again on the next tick.
"""

import logging
import math
from typing import Any, Protocol

log = logging.getLogger(__name__)

CONTAINER_NAME = "app"


def split_replicas(weight: int, total: int) -> tuple[int, int]:
    """ReplicaRouter: (stable, canary) replica counts for a canary weight in percent."""
    if weight <= 0:
        return total, 0
    if weight >= 100:
        return 0, total
    canary = min(total - 1, max(1, round(total * weight / 100)))
    return total - canary, canary


def canary_replicas(weight: int, total: int) -> int:
    """GatewayRouter: enough canary pods to carry `weight`% of the load stable carries."""
    if weight <= 0:
        return 0
    return min(total, max(1, math.ceil(total * weight / 100)))


class TrafficRouter(Protocol):
    def set_weight(self, app: str, image: str, weight: int, total: int) -> bool:
        """Send `weight`% of traffic to the canary. False = not yet, call again."""
        ...

    def promote(self, app: str, image: str, total: int) -> bool:
        """Make `image` the stable version. False = not yet, call again."""
        ...

    def rollback(self, app: str, total: int) -> None:
        """Send all traffic back to stable and remove the canary. Always immediate."""
        ...


class LogRouter:
    """Logs the actions only. Lets you run the API without a cluster."""

    def set_weight(self, app: str, image: str, weight: int, total: int) -> bool:
        log.info("[%s] canary %s at %d%%", app, image, weight)
        return True

    def promote(self, app: str, image: str, total: int) -> bool:
        log.info("[%s] promote %s to stable (%d replicas)", app, image, total)
        return True

    def rollback(self, app: str, total: int) -> None:
        log.info("[%s] rollback: all traffic to stable", app)


class _Kubernetes:
    """Shared Deployment helpers. Pass API clients in tests; otherwise load kubeconfig."""

    def __init__(self, namespace: str, apps_api: Any = None, custom_api: Any = None):
        if apps_api is None:
            from kubernetes import client, config

            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            apps_api, custom_api = client.AppsV1Api(), client.CustomObjectsApi()
        self._apps = apps_api
        self._custom = custom_api
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

    def _ready(self, name: str) -> bool:
        """All desired pods are on the latest spec and passing readiness."""
        d = self._apps.read_namespaced_deployment_status(name, self._ns)
        want = d.spec.replicas or 0
        st = d.status
        return (
            (st.observed_generation or 0) >= (d.metadata.generation or 0)
            and (st.updated_replicas or 0) >= want
            and (st.ready_replicas or 0) >= want
            and (st.replicas or 0) == want  # old pods from a previous spec are gone
        )


class ReplicaRouter(_Kubernetes):
    def set_weight(self, app: str, image: str, weight: int, total: int) -> bool:
        stable, canary = split_replicas(weight, total)
        # Scale up the canary before scaling down stable, so capacity never dips.
        self._patch(f"{app}-canary", canary, image)
        self._patch(f"{app}-stable", stable)
        return True  # the Service only sends traffic to ready pods anyway

    def promote(self, app: str, image: str, total: int) -> bool:
        self._patch(f"{app}-stable", total, image)
        self._patch(f"{app}-canary", 0)
        return True

    def rollback(self, app: str, total: int) -> None:
        self._patch(f"{app}-stable", total)
        self._patch(f"{app}-canary", 0)


class GatewayRouter(_Kubernetes):
    """Weighted routing through an HTTPRoute named {app}."""

    def _route(self, app: str, canary_weight: int) -> None:
        backend = {"kind": "Service", "port": 80}
        self._custom.patch_namespaced_custom_object(
            "gateway.networking.k8s.io",
            "v1",
            self._ns,
            "httproutes",
            app,
            {
                "spec": {
                    "rules": [
                        {
                            "backendRefs": [
                                {**backend, "name": f"{app}-stable", "weight": 100 - canary_weight},
                                {**backend, "name": f"{app}-canary", "weight": canary_weight},
                            ]
                        }
                    ]
                }
            },
        )

    def set_weight(self, app: str, image: str, weight: int, total: int) -> bool:
        self._patch(f"{app}-canary", canary_replicas(weight, total), image)
        self._patch(f"{app}-stable", total)  # stable stays full size: instant rollback
        if not self._ready(f"{app}-canary"):
            return False  # never route users to pods that aren't serving yet
        self._route(app, weight)
        return True

    def promote(self, app: str, image: str, total: int) -> bool:
        # The canary keeps 100% of traffic while stable rolls to the new image.
        self._patch(f"{app}-stable", total, image)
        if not self._ready(f"{app}-stable"):
            return False
        self._route(app, 0)
        self._patch(f"{app}-canary", 0)
        return True

    def rollback(self, app: str, total: int) -> None:
        self._route(app, 0)  # first: users are off the canary immediately
        self._patch(f"{app}-canary", 0)
        self._patch(f"{app}-stable", total)
