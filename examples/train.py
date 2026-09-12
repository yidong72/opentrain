"""Seed the local dashboard with explicitly labeled synthetic training runs."""

import math
import os
import random
import tempfile
import uuid

import wandb

os.environ.setdefault("WANDB_BASE_URL", "http://127.0.0.1:8080")
os.environ.setdefault("WANDB_API_KEY", "local" + "0" * 35)
os.environ.setdefault("WANDB_ENTITY", "local")


def train(run, start, stop, seed, rate):
    rng = random.Random(seed + start)
    for step in range(start, stop):
        loss = 2.5 * math.exp(-step / rate) + 0.14 + rng.uniform(-0.025, 0.025)
        run.log(
            {
                "train/loss": loss,
                "eval/accuracy": min(0.98, 1 - loss / 4 + rng.uniform(-0.01, 0.01)),
                "train/learning_rate": 0.001 * (1 + math.cos(step / 200 * math.pi)) / 2,
                "train/throughput": 2100 + seed * 200 + rng.uniform(-80, 80),
                "global_step": step,
            }
        )


if __name__ == "__main__":
    for index, (name, rate) in enumerate(
        [
            ("transformer-base", 44),
            ("transformer-wide", 57),
            ("transformer-resumed", 35),
        ]
    ):
        identifier = uuid.uuid4().hex[:8]
        with wandb.init(
            project="demo-training",
            id=identifier,
            name=name,
            group="synthetic-comparison",
            tags=["demo", "synthetic"],
            config={
                "learning_rate": 0.001,
                "batch_size": 32 * (index + 1),
                "model": name,
            },
            settings=wandb.Settings(x_disable_stats=True),
        ) as run:
            train(run, 0, 100 if index == 2 else 200, index, rate)
        if index == 2:
            with tempfile.TemporaryDirectory(prefix="open-train-resume-") as attempt:
                with wandb.init(
                    project="demo-training",
                    id=identifier,
                    resume="must",
                    dir=attempt,
                    settings=wandb.Settings(x_disable_stats=True),
                ) as run:
                    train(run, 100, 200, index, rate)
    print(f"Dashboard: {os.environ['WANDB_BASE_URL']}")
