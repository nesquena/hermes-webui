oxbench - ROQ / OpenAI-compatible benchmark

This folder provides a small benchmark harness and a local mock OpenAI-compatible
server to run the benchmark entirely offline for development and testing.

Quick examples

1) Run with the local mock server (recommended for offline testing)

# Bash / macOS / Linux
export ROQ_API_KEY="any-value"            # mock server will accept any key
python oxbench/main.py --mock

# Windows PowerShell (current session only)
$env:ROQ_API_KEY = "any-value"
python .\oxbench\main.py --mock

By default the mock server runs on 127.0.0.1:5080 and the runner points ROQ_BASE_URL
at http://127.0.0.1:5080/v1 for the duration of the run.

2) Run with simulated latency and a canned response

# Add environment variables to simulate latency (milliseconds) and canned text
# Bash
export MOCK_MIN_LATENCY_MS=100
export MOCK_MAX_LATENCY_MS=350
export MOCK_RESPONSE_MODE=canned
export MOCK_CANNED_TEXT="This is a canned mock reply for testing."
python oxbench/main.py --mock

# PowerShell
$env:MOCK_MIN_LATENCY_MS = "100"
$env:MOCK_MAX_LATENCY_MS = "350"
$env:MOCK_RESPONSE_MODE = "canned"
$env:MOCK_CANNED_TEXT = "This is a canned mock reply for testing."
python .\oxbench\main.py --mock

3) Skip the preflight connectivity check

If you want to run the benchmark but skip the preflight step (not recommended for real network runs):

python oxbench/main.py --no-preflight

Or with the mock server:

python oxbench/main.py --mock --no-preflight

4) Customize mock port

python oxbench/main.py --mock --mock-port 6000

Notes
- The mock server is intentionally minimal and meant to exercise the benchmark harness and
  provider wiring. It returns predictable responses, so benchmark timings reflect local
  processing time rather than real model latency.
- Use MOCK_LATENCY_MS or MOCK_MIN_LATENCY_MS / MOCK_MAX_LATENCY_MS to simulate response time.
- Use MOCK_RESPONSE_MODE=canned and MOCK_CANNED_TEXT to return a static canned reply.
- If you want more realistic behavior (streaming, multiple models, or advanced canned replies),
  tell me and I can extend the mock server.
