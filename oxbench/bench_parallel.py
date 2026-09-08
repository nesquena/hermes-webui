import asyncio
import time
from collections.abc import Iterable
from typing import Any

from q_agent import QAgent


async def run_one(agent: QAgent, question: str, semaphore: asyncio.Semaphore) -> tuple[str, float, str]:
    async with semaphore:
        started = time.perf_counter()
        answer = agent.ask(question)
        elapsed = time.perf_counter() - started
        return question, elapsed, answer


def run_benchmark(questions: Iterable[str], *, max_concurrency: int = 8, model: str | None = None) -> list[dict[str, Any]]:
    agent = QAgent(model=model)
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_all() -> list[tuple[str, float, str]]:
        tasks = [run_one(agent, q, semaphore) for q in questions]
        return await asyncio.gather(*tasks)

    results = asyncio.run(_run_all())
    return [
        {"question": question, "seconds": round(elapsed, 3), "answer": answer}
        for question, elapsed, answer in results
    ]


if __name__ == "__main__":
    qs = [
        "Explain in 2 sentences why benchmarking LLM latency matters.",
        "Write a short Python function that returns even numbers from 0 to 20.",
        "Summarize the benefits of async concurrency for network-bound workloads.",
    ]
    for item in run_benchmark(qs, max_concurrency=3):
        print(f"\nQ: {item['question']}\nT: {item['seconds']}s\nA: {item['answer']}\n")
