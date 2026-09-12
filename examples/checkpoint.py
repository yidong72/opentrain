"""Run twice: a framework-independent example of persisted run identity.

Replace JSON with your framework's checkpoint containing model and optimizer state.
"""

import json
import tempfile
from pathlib import Path

import wandb

checkpoint_path = Path("checkpoint-demo.json")
checkpoint = (
    json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else None
)
with tempfile.TemporaryDirectory(prefix="open-train-attempt-") as attempt:
    with wandb.init(
        project="checkpoint-demo",
        id=checkpoint["wandb_run_id"] if checkpoint else None,
        resume="must" if checkpoint else "never",
        dir=attempt,
    ) as run:
        run.define_metric("global_step")
        run.define_metric("loss", step_metric="global_step")
        start = checkpoint["global_step"] + 1 if checkpoint else 0
        for step in range(start, start + 10):
            run.log({"global_step": step, "loss": 1 / (step + 1)})
        checkpoint_path.write_text(
            json.dumps({"wandb_run_id": run.id, "global_step": step})
        )
        run.save(str(checkpoint_path), policy="now")
