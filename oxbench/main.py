import os
import statistics
import time
import argparse

from bench_parallel import run_benchmark
from preflight import main as preflight_main

# Attempt to import the local mock server helper; it's optional
try:
    from mock_openai_server import start_mock_server
except Exception:
    start_mock_server = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ROQ/OpenAI-compatible benchmark runner")
    p.add_argument("--no-preflight", action="store_true", help="Skip the connectivity preflight check")
    p.add_argument(
        "--mock",
        action="store_true",
        help="Start a local mock OpenAI-compatible server and point ROQ_BASE_URL to it",
    )
    p.add_argument(
        "--mock-port",
        type=int,
        default=5080,
        help="Port for local mock server (default: 5080)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not os.environ.get("ROQ_API_KEY"):
        # The mock server doesn't require a real key, but the client code expects ROQ_API_KEY to exist.
        # Allow a dummy value when running with --mock to make local testing easier.
        if args.mock:
            os.environ.setdefault("ROQ_API_KEY", "mock-key")
        else:
            raise SystemExit(
                "ROQ_API_KEY is not set. Run: export ROQ_API_KEY='your-key'\n"
                "Optional: export ROQ_MODEL='meta-llama/llama-3.3-70b-versatile'\n"
                "Optional: export ROQ_BASE_URL='https://api.roq.com/openai/v1'"
            )

    server = None
    thread = None

    if args.mock:
        if start_mock_server is None:
            raise SystemExit(
                "Mock server helper not found. Ensure oxbench/mock_openai_server.py exists and is importable."
            )
        host = "127.0.0.1"
        port = args.mock_port
        server, thread = start_mock_server(port=port, host=host)
        # If base URL isn't already set, point it at the mock server
        os.environ.setdefault("ROQ_BASE_URL", f"http://{host}:{port}/v1")
        print(f"Started mock OpenAI-compatible server at {os.environ['ROQ_BASE_URL']}")

    try:
        if not args.no_preflight:
            # Run connectivity preflight before executing the benchmark
            preflight_rc = 1
            try:
                preflight_rc = preflight_main()
            except Exception as exc:
                # If preflight raised unexpectedly, present a friendly message
                raise SystemExit(f"Preflight check failed unexpectedly: {exc}") from exc
            if preflight_rc != 0:
                raise SystemExit(
                    "Preflight connectivity check failed; aborting benchmark. Use --no-preflight to override."
                )

        questions = [
            "What is the difference between a prompt and a system message in an LLM app?",
            "Give me a concise checklist for evaluating a new model for production use.",
            "Explain why batching requests can improve throughput in an OpenAI-compatible API.",
            "List 3 common failure modes when benchmarking AI APIs and how to mitigate them.",
            "Write a tiny Python program that prints the Fibonacci sequence up to 10 terms.",
        ]

        model = os.environ.get("ROQ_MODEL")
        try:
            results = run_benchmark(questions, max_concurrency=4, model=model)
        except Exception as exc:  # noqa: BLE001 - user-facing network diagnosis
            message = str(exc).lower()
            if any(
                token in message
                for token in (
                    "getaddrinfo failed",
                    "remote name could not be resolved",
                    "connection error",
                    "timed out",
                    "network",
                    "dns",
                    "unable to resolve",
                )
            ):
                raise SystemExit(
                    "ROQ benchmark could not reach the API from this machine.\n"
                    "This usually means DNS/internet access is blocked or the base URL is wrong.\n"
                    f"Base URL: {os.environ.get('ROQ_BASE_URL', 'https://api.roq.com/openai/v1')}\n"
                    f"Details: {exc}"
                ) from exc
            raise

        latencies = [item["seconds"] for item in results]
        print("Benchmark results:")
        for item in results:
            print(f"- {item['seconds']}s :: {item['question'][:80]}")
            print(f"  Answer: {item['answer'][:200]}")

        def percentile(arr, p: float) -> float:
            if not arr:
                return 0.0
            s = sorted(arr)
            n = len(s)
            if n == 1:
                return s[0]
            # linear interpolation between nearest ranks
            rank = (p / 100.0) * (n - 1)
            lo = int(rank)
            hi = min(lo + 1, n - 1)
            frac = rank - lo
            return s[lo] * (1 - frac) + s[hi] * frac

        print("\nSummary:")
        print(f"min={min(latencies):.3f}s")
        print(f"max={max(latencies):.3f}s")
        print(f"mean={statistics.fmean(latencies):.3f}s")
        print(f"p50={percentile(latencies, 50):.3f}s")
        print(f"p90={percentile(latencies, 90):.3f}s")
        print(f"p95={percentile(latencies, 95):.3f}s")

    finally:
        if server:
            print("Shutting down mock server...")
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
            # give thread a moment to exit
            time.sleep(0.1)


if __name__ == "__main__":
    main()
