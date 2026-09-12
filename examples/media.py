"""Upload an image, an audio file, and a table with the official SDK."""

import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageDraw

import wandb

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="open-train-media-") as directory:
        image = Image.new("RGB", (640, 320), "#edf3ed")
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 60, 220, 240), fill="#328971")
        draw.ellipse((280, 60, 460, 240), fill="#9380c6")
        audio_path = Path(directory) / "silence.wav"
        with wave.open(str(audio_path), "wb") as output:
            output.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            output.writeframes(bytes(16000))
        with wandb.init(
            project="demo-training", name="media-example", tags=["demo"]
        ) as run:
            run.log(
                {
                    "sample": wandb.Image(image, caption="Synthetic shapes"),
                    "audio": wandb.Audio(str(audio_path)),
                    "predictions": wandb.Table(
                        columns=["sample", "prediction", "confidence"],
                        data=[
                            ["rectangle", "rectangle", 0.98],
                            ["circle", "circle", 0.96],
                        ],
                    ),
                }
            )
