"""Run with WANDB_BASE_URL and WANDB_API_KEY set for your server."""

import wandb


def train():
    with wandb.init() as run:
        for step in range(20):
            run.log({"loss": (run.config.learning_rate - 0.01) ** 2 + 1 / (step + 1)})


if __name__ == "__main__":
    sweep = wandb.sweep(
        {
            "method": "grid",
            "metric": {"name": "loss", "goal": "minimize"},
            "parameters": {
                "learning_rate": {"values": [0.001, 0.01, 0.1]},
                "batch_size": {"value": 32},
            },
        },
        project="sweep-demo",
    )
    wandb.agent(sweep, function=train, count=3, project="sweep-demo")
