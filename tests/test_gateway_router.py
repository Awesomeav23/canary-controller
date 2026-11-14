"""GatewayRouter against a fake Kubernetes API: what it patches, and in what order."""

from types import SimpleNamespace

import pytest

from canary.router import GatewayRouter, canary_replicas


class FakeKube:
    """Stands in for AppsV1Api and CustomObjectsApi. Records every write in order."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.ready: dict[str, bool] = {"demo-stable": True, "demo-canary": True}
        self.replicas: dict[str, int] = {}

    # AppsV1Api
    def patch_namespaced_deployment(self, name, namespace, body, **kwargs):
        spec = body["spec"]
        self.replicas[name] = spec["replicas"]
        image = spec.get("template", {}).get("spec", {}).get("containers", [{}])[0].get("image")
        self.calls.append(("scale", name, spec["replicas"], image))

    def read_namespaced_deployment_status(self, name, namespace):
        want = self.replicas.get(name, 0)
        have = want if self.ready[name] else 0
        return SimpleNamespace(
            spec=SimpleNamespace(replicas=want),
            metadata=SimpleNamespace(generation=2),
            status=SimpleNamespace(
                observed_generation=2, updated_replicas=have, ready_replicas=have, replicas=want
            ),
        )

    # CustomObjectsApi
    def patch_namespaced_custom_object(self, group, version, namespace, plural, name, body):
        refs = body["spec"]["rules"][0]["backendRefs"]
        self.calls.append(("route", name, {r["name"]: r["weight"] for r in refs}))


@pytest.fixture
def kube():
    return FakeKube()


@pytest.fixture
def router(kube):
    return GatewayRouter("default", apps_api=kube, custom_api=kube)


def routes(kube):
    return [c for c in kube.calls if c[0] == "route"]


def test_shifts_exact_weight_once_canary_is_ready(router, kube):
    assert router.set_weight("demo", "demo:v2", 5, 4) is True
    assert kube.calls == [
        ("scale", "demo-canary", 1, "demo:v2"),
        ("scale", "demo-stable", 4, None),  # stable stays full size
        ("route", "demo", {"demo-stable": 95, "demo-canary": 5}),
    ]


def test_does_not_route_to_canary_pods_that_are_not_ready(router, kube):
    kube.ready["demo-canary"] = False
    assert router.set_weight("demo", "demo:v2", 25, 4) is False
    assert routes(kube) == []  # users never sent to a canary that can't serve


def test_rollback_moves_traffic_before_touching_pods(router, kube):
    router.rollback("demo", 4)
    assert kube.calls[0] == ("route", "demo", {"demo-stable": 100, "demo-canary": 0})
    assert kube.calls[1:] == [("scale", "demo-canary", 0, None), ("scale", "demo-stable", 4, None)]


def test_promote_waits_for_stable_then_moves_traffic(router, kube):
    kube.ready["demo-stable"] = False
    assert router.promote("demo", "demo:v2", 4) is False
    assert kube.calls == [("scale", "demo-stable", 4, "demo:v2")]  # canary keeps traffic

    kube.ready["demo-stable"] = True
    assert router.promote("demo", "demo:v2", 4) is True
    assert kube.calls[-2:] == [
        ("route", "demo", {"demo-stable": 100, "demo-canary": 0}),
        ("scale", "demo-canary", 0, None),
    ]


@pytest.mark.parametrize(
    "weight,total,expected",
    [(0, 4, 0), (1, 4, 1), (5, 4, 1), (25, 4, 1), (26, 4, 2), (50, 4, 2), (100, 4, 4)],
)
def test_canary_replicas(weight, total, expected):
    assert canary_replicas(weight, total) == expected
