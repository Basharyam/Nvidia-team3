# tests/test_integration_start_container.py
import sys
import time
import pathlib
import importlib.util
import os
import uuid
import pytest
import docker
from fastapi.testclient import TestClient
from container_manger import ContainerManager


IMAGE = "nginx:alpine"          # runs with no args
C_PORT = "80/tcp"               # container port to expose (nginx)
MEM = "128m"                    # small mem limit for tests
CPU = "0.25"                    # quarter CPU


def _docker_or_skip():
    """Return a docker client or skip tests if Docker is unavailable."""
    try:
        client = docker.from_env()
        client.ping()
        return client
    except Exception as e:
        pytest.skip(f"Docker not available: {e}")


def _load_app_module():
    """
    Load the API module dynamically without clashing with the 'fastapi' package.
    1) If ORCH_APP_FILE env var is set -> use that path.
    2) Otherwise, search common filenames/locations.
    """
    project_root = pathlib.Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # 1) env override (absolute or relative to project root)
    env_path = os.getenv("ORCH_APP_FILE")
    if env_path:
        target = (project_root / env_path).resolve() if not os.path.isabs(env_path) else pathlib.Path(env_path)
        if not target.exists():
            raise FileNotFoundError(f"ORCH_APP_FILE points to non-existing file: {target}")
    else:
        # 2) search common candidates
        candidates = [
            project_root / "fast_api.py",                 # what we used in examples
            project_root / "api.py",
            project_root / "app.py",
            project_root / "main.py",
            project_root / "server.py",
            project_root / "src" / "fastapi.py",
            project_root / "src" / "api.py",
            project_root / "orchestrator" / "fastapi.py",
            project_root / "orchestrator" / "api.py",
        ]
        target = next((p for p in candidates if p.exists()), None)
        if target is None:
            looked = "\n  - ".join(str(p) for p in candidates)
            raise FileNotFoundError(
                "Could not find your API module.\n"
                "Set ORCH_APP_FILE to its path or place it at one of:\n  - " + looked
            )

    modname = f"orchestrator_api_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(modname, target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore

    # sanity: module should expose 'app' and 'manager'
    if not hasattr(module, "app"):
        raise AttributeError(f"{target} must define 'app = FastAPI(...)'")
    if not hasattr(module, "manager"):
        raise AttributeError(f"{target} must define 'manager = ContainerManager()'")

    return module


@pytest.fixture(scope="module")
def dclient():
    return _docker_or_skip()


@pytest.fixture(scope="module")
def app_module():
    return _load_app_module()


@pytest.fixture(autouse=True)
def cleanup_managed_containers(dclient, app_module):
    """
    Before & after each test, remove any containers we manage for IMAGE (by label).
    """
    label_key = app_module.manager.LABEL_KEY
    def _prune():
        for c in dclient.containers.list(all=True, filters={"label": f"{label_key}={IMAGE}"}):
            try:
                c.remove(force=True)
            except Exception:
                pass
    _prune()
    yield
    _prune()


def test_create_container_real_docker(app_module, dclient):
    client = TestClient(app_module.app)

    payload = {
        "image": IMAGE,
        "min_replicas": 1,
        "max_replicas": 3,
        "env": {},
        "ports": {C_PORT: 0},  # 0 => auto-assign host port
        "resources": {"cpu": CPU, "memory": MEM, "status": "running"}
    }

    # First call should create a new container
    r = client.post("/start/container", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["action"] == "created"
    assert data["image"] == IMAGE
    assert "container_id" in data
    assert C_PORT in data["ports"]
    assert data["ports"][C_PORT][0]["HostPort"].isdigit()

    # Verify with Docker that the container exists and is running
    cid = data["container_id"]
    c = dclient.containers.get(cid)
    c.reload()
    assert c.status == "running"


def test_idempotent_keeps_existing(app_module, dclient):
    client = TestClient(app_module.app)

    payload = {
        "image": IMAGE,
        "min_replicas": 1,
        "max_replicas": 3,
        "env": {},
        "ports": {C_PORT: 0},
        "resources": {"cpu": CPU, "memory": MEM, "status": "running"}
    }

    # Create once
    r1 = client.post("/start/container", json=payload)
    cid1 = r1.json()["container_id"]

    # Call again -> should NOT create a duplicate
    r2 = client.post("/start/container", json=payload)
    assert r2.status_code == 200, r2.text
    data2 = r2.json()
    assert data2["action"] == "kept-existing"
    assert data2["container_id"] == cid1


def test_status_stopped_creates_then_stops(app_module, dclient):
    client = TestClient(app_module.app)

    payload = {
        "image": IMAGE,
        "min_replicas": 1,
        "max_replicas": 3,
        "env": {},
        "ports": {C_PORT: 0},
        "resources": {"cpu": CPU, "memory": MEM, "status": "stopped"}
    }

    r = client.post("/start/container", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    cid = data["container_id"]

    # Give Docker a moment to reflect the stopped state
    time.sleep(0.5)
    c = dclient.containers.get(cid)
    c.reload()
    # stopped containers report as 'exited'
    assert c.status in ("exited", "created")



# ---------- test the delete container ----------


def _create_nginx(dclient: docker.DockerClient, *, name=None, labels=None, expose_port=False):
    """
    Small helper to create an nginx container for testing.
    If expose_port=True, requests a random host port mapping for 80/tcp
    to ensure the container is RUNNING.
    """
    ports = {"80/tcp": None} if expose_port else None
    return dclient.containers.run(
        "nginx:alpine",
        detach=True,
        name=name,
        labels=labels or {},
        ports=ports
    )


def test_delete_container_exited(app_module, dclient):
    """
    Create a container, stop it, and then delete it with force=false.
    Expect 200 and that the container no longer exists.
    """
    client = TestClient(app_module.app)

    labels = {ContainerManager.LABEL_KEY: "nginx:alpine"}
    c = _create_nginx(dclient, labels=labels)
    cname = c.name
    try:
        c.stop(timeout=5)

        resp = client.delete(f"/containers/{cname}?force=false")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("deleted") is True

        with pytest.raises(docker.errors.NotFound):
            dclient.containers.get(cname)
    finally:
        # Cleanup in case of intermediate failure
        try:
            dclient.containers.get(cname).remove(force=True)
        except docker.errors.NotFound:
            pass


def test_delete_container_running_force(app_module, dclient):
    """
    Create a RUNNING container and delete it with force=true.
    Expect 200 and that the container is gone.
    """
    client = TestClient(app_module.app)

    labels = {ContainerManager.LABEL_KEY: "nginx:alpine"}
    c = _create_nginx(dclient, labels=labels, expose_port=True)
    cname = c.name
    try:
        c.reload()
        assert c.status in ("created", "restarting", "running")

        resp = client.delete(f"/containers/{cname}?force=true")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data.get("deleted") is True

        with pytest.raises(docker.errors.NotFound):
            dclient.containers.get(cname)
    finally:
        try:
            dclient.containers.get(cname).remove(force=True)
        except docker.errors.NotFound:
            pass


def test_delete_container_not_found(app_module):
    """
    Deleting a non-existent container should return 404.
    """
    client = TestClient(app_module.app)
    resp = client.delete("/containers/this-name-should-not-exist-xyz?force=true")
    assert resp.status_code == 404
    assert "not found" in resp.json().get("detail", "").lower()


