import argparse
import json
import os
import random
import sys
import time


def main():
    parser = argparse.ArgumentParser(
        prog="open-train", description="Self-hosted training telemetry"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--data-dir", default=os.environ.get("OPEN_TRAIN_DATA_DIR", "data")
    )
    importer = commands.add_parser(
        "import-tensorboard",
        help="Import scalar metrics from TensorBoard event directories",
    )
    importer.add_argument("logdir")
    importer.add_argument("--project", required=True)
    importer.add_argument("--entity", default=os.environ.get("WANDB_ENTITY", "local"))
    importer.add_argument(
        "--base-url", default=os.environ.get("WANDB_BASE_URL", "http://127.0.0.1:8080")
    )
    importer.add_argument(
        "--run-id", help="Existing TensorBoard run ID (one event directory only)"
    )
    importer.add_argument(
        "--source",
        help="Stable dataset identity so moving log directories preserves run IDs",
    )
    importer.add_argument(
        "--watch", action="store_true", help="Poll for new events until Ctrl-C"
    )
    importer.add_argument("--interval", type=float, default=10)
    sync = commands.add_parser(
        "sync-watch", help="Automatically upload a persistent offline W&B spool"
    )
    sync.add_argument("spool", help="Persistent WANDB_DIR (or its wandb subdirectory)")
    sync.add_argument(
        "--interval", type=float, default=5, help="Retry base delay in seconds"
    )
    sync.add_argument(
        "--workers", type=int, default=4, help="Maximum concurrent run uploads"
    )
    sync.add_argument(
        "--once",
        action="store_true",
        help="Recover stopped writers, then exit; do not use on live runs",
    )
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(args.data_dir), host=args.host, port=args.port)
    elif args.command == "sync-watch":
        from .sync import watch

        if args.interval <= 0 or args.workers <= 0:
            parser.error("--interval and --workers must be positive")
        try:
            raise SystemExit(
                watch(
                    args.spool,
                    interval=args.interval,
                    workers=args.workers,
                    once=args.once,
                )
            )
        except (RuntimeError, OSError) as error:
            parser.exit(1, f"Sync failed: {error}\n")
    else:
        import httpx

        from .tensorboard import import_once

        if args.interval <= 0:
            parser.error("--interval must be positive")
        failures = 0
        try:
            while True:
                try:
                    result = import_once(
                        args.logdir,
                        args.project,
                        args.base_url,
                        args.entity,
                        args.run_id,
                        args.source,
                        os.environ.get("WANDB_API_KEY"),
                    )
                except (httpx.TransportError, httpx.HTTPStatusError) as error:
                    transient = not isinstance(error, httpx.HTTPStatusError) or (
                        error.response.status_code >= 500
                        or error.response.status_code in {408, 429}
                    )
                    if not args.watch or not transient:
                        raise
                    failures += 1
                    delay = min(300, args.interval * 2 ** min(failures - 1, 10))
                    print(
                        f"TensorBoard upload unavailable; event files retained. Retrying in ~{delay:g}s.",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(delay * random.uniform(0.8, 1.2))
                    continue
                failures = 0
                print(json.dumps(result, indent=2), flush=True)
                if not args.watch:
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
        except Exception as error:
            parser.exit(1, f"Import failed: {error}\n")


if __name__ == "__main__":
    main()
