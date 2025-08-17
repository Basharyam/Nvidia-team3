"""
FastAPI layer that exposes the /start/container endpoint and delegates
all Docker logic to ContainerManager.
"""

from typing import Dict, Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# note the filename: containermanger.py (kept exactly as requested)
from container_manger import ContainerManager


# ------------------ Pydantic schemas ------------------

class Resources(BaseModel):
    # CPU quota (Docker's --cpus). "0.5" = half a CPU.
    cpu: str
    # Docker memory string, e.g., "512m", "1g"
    memory: str
    # Desired state for the ensured single container
    status: Literal["running", "stopped"] = "running"


class StartContainerRequest(BaseModel):
    image: str                                 # e.g., "hashicorp/http-echo"
    min_replicas: int = Field(ge=0, default=1) # stored for future /scale use
    max_replicas: int = Field(ge=1, default=5)
    env: Dict[str, str] = Field(default_factory=dict)
    # containerPort/proto -> hostPort (0/None => auto host port)
    ports: Dict[str, int] = Field(default_factory=dict)
    resources: Resources


# ------------------ FastAPI app & routes ------------------

app = FastAPI(title="Orchestrator", version="0.1")
manager = ContainerManager()


@app.post("/start/container")
def start_container(body: StartContainerRequest):
    """
    Ensure exactly one managed container exists for the given image.
    - Saves desired state (min/max/env/ports/resources).
    - If a container exists: starts it if requested, returns its info.
    - If not: creates it with resource limits & port mapping, returns info.
    """
    # 1) record desired state (handy for /scale and UI later)
    manager.register_desired_state(
        body.image,
        min_replicas=body.min_replicas,
        max_replicas=body.max_replicas,
        env=body.env,
        ports=body.ports,
        resources=body.resources.model_dump()
    )

    # 2) ensure one container exists for this image
    result = manager.ensure_singleton_for_image(
        body.image,
        env=body.env,
        ports=body.ports,
        resources=body.resources.model_dump()
    )

    if not result.get("ok"):
        # Translate manager errors into HTTP errors
        raise HTTPException(status_code=500, detail=result.get("error", "Unknown error"))

    return result


@app.get("/containers")
def list_containers():
    """
    Optional helper: list all containers managed by this orchestrator (by label).
    Good for debugging and for the UI/load-balancer teams.
    """
    return {"containers": manager.list_managed_containers()}


# For quick local dev you can run: uvicorn fastapi:app --reload --port 8000
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("fastapi:app", host="0.0.0.0", port=8000, reload=True)
