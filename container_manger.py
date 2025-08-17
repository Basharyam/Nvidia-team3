"""
ContainerManager: a thin orchestration wrapper around the Docker SDK.

- Keeps a minimal desired-state store per image (min/max/env/ports/resources).
- Ensures there is at least one managed container per image on demand.
- Uses Docker labels to rediscover containers even after process restarts.
"""

from typing import Dict, List, Optional, Any
import time
import docker


class ContainerManager:
    """
    Encapsulates all Docker interactions so the FastAPI layer stays thin
    and focused on HTTP/Pydantic only.
    """

    LABEL_KEY = "orchestrator.image"  # every managed container gets this label

    def __init__(
        self,
        client: Optional[docker.DockerClient] = None,
        client_factory=None,
    ) -> None:
        # Do NOT talk to Docker during import; connect only when first needed.
        self._client: Optional[docker.DockerClient] = client
        self._client_factory = client_factory or docker.from_env

        # Super-simple desired-state store (swap to DB later if needed).
        self.desired_images: Dict[str, Dict[str, Any]] = {}  # image -> config dict
        # Optional bookkeeping (we mostly rely on labels for rediscovery).
        self._tracked: Dict[str, List[str]] = {}

    # ---------- internals ----------

    def _cli(self) -> docker.DockerClient:
        """Get (or lazily create) the Docker client."""
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    # ---------- desired state ----------

    def register_desired_state(
        self,
        image: str,
        *,
        min_replicas: int,
        max_replicas: int,
        env: Dict[str, str],
        ports: Dict[str, int],
        resources: Dict[str, Any],
    ) -> None:
        """
        Save/refresh what's desired for a given image so other modules can query it
        or so you can implement /scale next.
        """
        self.desired_images[image] = {
            "image": image,
            "min_replicas": min_replicas,
            "max_replicas": max_replicas,
            "env": env or {},
            "ports": ports or {},
            "resources": resources or {},  # {"cpu":"0.5","memory":"512m","status":"running"}
        }
        self._tracked.setdefault(image, [])

    # ---------- queries & helpers ----------

    def _current_for_image(self, image: str, running_only: bool = False):
        """
        Find existing containers for this image by label.
        running_only=False -> include stopped/exited (all=True)
        running_only=True  -> only running
        """
        cli = self._cli()
        flt = {"label": f"{self.LABEL_KEY}={image}"}
        if running_only:
            return cli.containers.list(filters=flt)
        return cli.containers.list(all=True, filters=flt)

    @staticmethod
    def _normalize_ports(ports: Dict[str, int]) -> Optional[Dict[str, Optional[int]]]:
        """
        Convert {"5678/tcp": 0} -> {"5678/tcp": None} so Docker allocates a free host port.
        Return None if ports is empty (no -p).
        """
        if not ports:
            return None
        out: Dict[str, Optional[int]] = {}
        for cport, hport in ports.items():
            out[cport] = None if (hport is None or hport == 0) else hport
        return out

    @staticmethod
    def _summarize_container(c) -> Dict[str, Any]:
        """
        Produce a response-friendly dict about the container (id/name/status/port bindings).
        """
        c.reload()
        return {
            "container_id": c.id,
            "name": c.name,
            "status": c.status,
            # Host port bindings live under NetworkSettings->Ports
            "ports": c.attrs.get("NetworkSettings", {}).get("Ports", {}),
        }

    @staticmethod
    def _cpu_to_nano_cpus(cpu_value: Any) -> Optional[int]:
        """
        Translate a user CPU value to Docker's nano_cpus (int).
        Accepts strings like "0.5", "1", "0", or Kubernetes-like "500m".
        """
        if cpu_value is None:
            return None
        try:
            s = str(cpu_value).strip().lower()
            if s.endswith("m"):              # e.g., "500m" => 0.5
                milli = float(s[:-1])
                v = milli / 1000.0
            else:
                v = float(s)
            return int(v * 1_000_000_000) if v > 0 else None
        except Exception:
            return None

    @staticmethod
    def _ports_ready(c, expect_keys: Optional[List[str]] = None) -> bool:
        ports = (c.attrs.get("NetworkSettings", {}) or {}).get("Ports") or {}
        if not ports:
            return False
        if expect_keys:
            for k in expect_keys:
                v = ports.get(k)
                if not isinstance(v, list) or len(v) == 0:
                    return False
            return True
        # no explicit expectation -> at least one non-empty binding
        return any(isinstance(v, list) and len(v) > 0 for v in ports.values())

    def _wait_for_ports(self, c, expect_keys: Optional[List[str]], timeout: float = 5.0, interval: float = 0.1):
        """
        Reload until host-port bindings appear (needed on Desktop/macOS right after run/start).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            c.reload()
            if self._ports_ready(c, expect_keys):
                return
            time.sleep(interval)
        # best effort; return regardless

    # ---------- core action ----------

    def ensure_singleton_for_image(
        self,
        image: str,
        *,
        env: Dict[str, str],
        ports: Dict[str, int],
        resources: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Ensure there is at least one managed container for `image`.
        """
        cli = self._cli()
        existing = self._current_for_image(image, running_only=False)
        desired_status = (resources or {}).get("status", "running")
        expect_keys = list((ports or {}).keys()) or None

        if existing:
            c = existing[0]
            if desired_status == "running" and c.status != "running":
                c.start()
                if expect_keys:
                    self._wait_for_ports(c, expect_keys)
            return {
                "ok": True,
                "action": "kept-existing",
                "image": image,
                **self._summarize_container(c),
            }

        # No container exists yet -> create one
        try:
            cli.images.pull(image)
        except Exception:
            pass  # fine if already local/offline

        mem_limit = (resources or {}).get("memory")  # e.g., "512m"
        nano_cpus = self._cpu_to_nano_cpus((resources or {}).get("cpu"))
        port_map = self._normalize_ports(ports)

        try:
            c = cli.containers.run(
                image,
                detach=True,
                environment=env or None,
                ports=port_map,                         # {"80/tcp": None} => random free host port
                labels={self.LABEL_KEY: image},         # mark as "managed"
                mem_limit=mem_limit,                    # e.g., "512m"
                nano_cpus=nano_cpus,                    # SDK expects nano_cpus
                restart_policy={"Name": "unless-stopped"},
            )
            self._tracked.setdefault(image, []).append(c.id)

            if expect_keys:
                self._wait_for_ports(c, expect_keys)

            if desired_status == "stopped":
                c.stop()

            return {
                "ok": True,
                "action": "created",
                "image": image,
                **self._summarize_container(c),
            }

        except Exception as e:
            return {"ok": False, "error": f"Failed to run container: {e}"}

    # ---------- optional utility endpoints ----------

    def list_managed_containers(self) -> List[Dict[str, Any]]:
        """
        Return a summary of all containers bearing the orchestrator label (any image).
        """
        cli = self._cli()
        result: List[Dict[str, Any]] = []
        all_containers = cli.containers.list(all=True, filters={"label": self.LABEL_KEY})
        for c in all_containers:
            result.append(self._summarize_container(c))
        return result

    def delete_container(self, name_or_id: str, force: bool = False) -> Dict[str, Any]:
        """
        Stop and remove a container by name or ID.
        If force=True, remove even if running.
        """
        cli = self._cli()
        try:
            c = cli.containers.get(name_or_id)
        except docker.errors.NotFound:
            return {"deleted": False, "reason": "not_found", "name_or_id": name_or_id}

        info = {
            "id": c.id[:12],
            "name": c.name,
            "image": c.image.tags[0] if c.image.tags else c.image.short_id,
            "status_before": c.status,
        }

        try:
            c.remove(force=force)
            info["deleted"] = True
        except Exception as e:
            info["deleted"] = False
            info["error"] = str(e)

        return info