import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.dirname(__file__))
OX = os.path.join(ROOT, 'oxbench')
MAIN = os.path.join(OX, 'main.py')

def test_run_benchmark_with_mock():
    # Run the benchmark with mock server and no preflight skip
    cmd = [sys.executable, MAIN, '--mock']
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=30)
    print(proc.stdout)
    assert proc.returncode == 0, f"Benchmark failed: {proc.stderr}"
    assert 'Benchmark results' in proc.stdout

if __name__ == '__main__':
    test_run_benchmark_with_mock()
    print('ok')
