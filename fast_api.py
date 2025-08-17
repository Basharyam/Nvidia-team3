"""
FastAPI layer that exposes container orchestration endpoints and delegates
all Docker logic to ContainerManager.
"""

from typing import Dict, Literal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# note the filename: container_manger.py (kept exactly as requested)
from container_manger import ContainerManager


# ------------------ Pydantic schemas ------------------

class Resources(BaseModel):
    cpu: str                  # CPU quota, e.g., "0.5" = half a CPU
    memory: str               # Docker memory string, e.g., "512m", "1g"
    status: Literal["running", "stopped"] = "running"


class StartContainerRequest(BaseModel):
    image: str
    min_replicas: int = Field(ge=0, default=1)
    max_replicas: int = Field(ge=1, default=5)
    env: Dict[str, str] = Field(default_factory=dict)
    ports: Dict[str, int] = Field(default_factory=dict)   # containerPort/proto -> hostPort
    resources: Resources


# ------------------ FastAPI app & routes ------------------

app = FastAPI(title="Orchestrator", version="0.1")
manager = ContainerManager()


@app.post("/start/container")
def start_container(body: StartContainerRequest):
    """
    Ensure exactly one managed container exists for the given image.
    """
    manager.register_desired_state(
        body.image,
        min_replicas=body.min_replicas,
        max_replicas=body.max_replicas,
        env=body.env,
        ports=body.ports,
        resources=body.resources.model_dump()
    )

    result = manager.ensure_singleton_for_image(
        body.image,
        env=body.env,
        ports=body.ports,
        resources=body.resources.model_dump()
    )

    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "Unknown error"))
    return result


@app.get("/containers")
def list_containers():
    """List all containers managed by this orchestrator (by label)."""
    return {"containers": manager.list_managed_containers()}


@app.delete("/containers/{name_or_id}")
def delete_container(name_or_id: str, force: bool = False):
    """
    Delete a container by name or ID.
    Example:
        DELETE /containers/
    """
    result = manager.delete_container(name_or_id, force=force)
    if not result.get("deleted"):
        raise HTTPException(status_code=404, detail=f"Container '{name_or_id}' not found")
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("fast_api:app", host="0.0.0.0", port=8000, reload=True)
