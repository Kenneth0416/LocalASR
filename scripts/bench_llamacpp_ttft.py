#!/usr/bin/env python3
"""
Benchmark llama.cpp server output speed at different input token lengths.

Measures:
  - TTFT (Time To First Token) — latency from request to first streamed token
  - TPS  (Tokens Per Second)   — generation throughput
  - Total time for the full response

Usage:
    python scripts/bench_llamacpp_ttft.py [--base-url http://localhost:8000] [--model qwen3.5:9b]
"""

import argparse
import json
import statistics
import time

import httpx


# ── Prompt templates ──────────────────────────────────────────────────────────

# Base meeting transcript chunk (~250 tokens) for generating long prompts
_BASE_CHUNK = (
    "[t=0s] 主持人A: 各位早上好，今天我们讨论Q3的产品路线图。"
    "[t=8s] 产品经理B: 我们重点关注三件事：用户增长、付费转化和留存率。"
    "[t=15s] 技术负责人C: 后端架构需要重构，目前的单体应用扩展性不够。"
    "[t=22s] 设计师D: 我建议把首页UI完全重新设计，增加个性化推荐区。"
    "[t=30s] 数据分析E: 上季度数据显示移动端转化比PC低40%，需要重点优化。"
    "[t=38s] 主持人A: 大家的意见都很好。我们投票决定优先级。"
    "[t=45s] 产品经理B: 我提议先做留存率，因为获客成本最近翻倍。"
    "[t=52s] 技术负责人C: 同意，但是架构重构不能拖，否则后期改造成本更高。"
    "[t=60s] 主持人A: 好，我们分两组并行推进。架构组和留存组。"
)


def _make_long_prompt(target_tokens: int, question: str) -> str:
    """Generate a prompt with approximately target_tokens by repeating the base chunk."""
    chunk_tokens = count_approx_tokens(_BASE_CHUNK)
    repeats = max(1, target_tokens // chunk_tokens)
    transcript = "\n".join(f"段落{i+1}: {_BASE_CHUNK}" for i in range(repeats))
    return (
        "你是一位会议助手。以下是一段会议记录：\n\n"
        f"【完整会议转录】\n{transcript}\n\n"
        f"【问题】\n{question}"
    )


def count_approx_tokens(text: str) -> int:
    """Rough token count estimate."""
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other = len(text) - cjk
    return int(cjk * 1.5 + other * 0.3)


# Approximate token counts (1 CJK char ≈ 1.5 tokens, 1 English word ≈ 1.3 tokens)
PROMPTS = {
    "~50 tokens": "请用中文简短回答：今天的天气如何？",
    "~200 tokens": (
        "你是一位会议助手。以下是一段会议记录：\n"
        "主持人：今天我们讨论Q3的营收目标。市场部提出增长15%的目标。\n"
        "产品经理：我认为12%更现实，因为竞品最近有大动作。\n"
        "主持人：那我们折中一下，定13.5%。\n"
        "请用一句话总结会议结论。"
    ),
    "~500 tokens": _make_long_prompt(500, "请用一句话总结会议主要决定。"),
    "~1000 tokens": _make_long_prompt(1000, "请用三句话总结会议要点。"),
    "~2000 tokens": _make_long_prompt(2000, "请总结会议的所有决定，用要点列表。"),
    "~5000 tokens": _make_long_prompt(5000, "请用一段话简短总结会议主要内容。"),
    "~10000 tokens": _make_long_prompt(10000, "请用一句话总结会议核心议题。"),
    "~20000 tokens": _make_long_prompt(20000, "请用一句话总结会议核心议题。"),
    "~50000 tokens": _make_long_prompt(50000, "请用一句话总结会议核心议题。"),
}


def bench_once(client: httpx.Client, base_url: str, model: str, prompt: str) -> dict:
    """Run a single streaming completion and measure TTFT + TPS."""
    t_start = time.perf_counter()
    first_token_time = None
    token_count = 0
    full_text = []

    with client.stream(
        "POST",
        f"{base_url}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
            "temperature": 0.0,
            "stream": True,
        },
        timeout=120.0,
    ) as resp:
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data.strip() == "[DONE]":
                break
            chunk = json.loads(data)
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content", "")
            if content:
                if first_token_time is None:
                    first_token_time = time.perf_counter()
                token_count += 1
                full_text.append(content)

    t_end = time.perf_counter()
    total = t_end - t_start
    ttft = (first_token_time - t_start) if first_token_time else None
    gen_time = (t_end - first_token_time) if first_token_time else 0
    tps = token_count / gen_time if gen_time > 0 else 0

    return {
        "ttft_ms": round(ttft * 1000, 1) if ttft else None,
        "output_tokens": token_count,
        "total_s": round(total, 2),
        "tps": round(tps, 1),
        "output_text": "".join(full_text)[:80],
    }


def main():
    parser = argparse.ArgumentParser(description="Benchmark llama.cpp output speed")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--runs", type=int, default=3, help="Runs per prompt length")
    args = parser.parse_args()

    print(f"llama.cpp Benchmark — {args.model}")
    print(f"Server: {args.base_url}")
    print(f"Runs per prompt: {args.runs}")
    print("=" * 80)

    client = httpx.Client()

    # Warmup
    print("\n⏳ Warming up server...")
    try:
        client.post(
            f"{args.base_url}/v1/chat/completions",
            json={"model": args.model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1},
            timeout=30.0,
        )
        print("✅ Warmup done\n")
    except Exception as e:
        print(f"❌ Warmup failed: {e}")
        return

    results = []

    for label, prompt in PROMPTS.items():
        input_tokens = count_approx_tokens(prompt)
        print(f"\n{'─' * 60}")
        print(f"📝 Input: {label} (~{input_tokens} tokens)")
        print(f"{'─' * 60}")

        runs = []
        for i in range(args.runs):
            r = bench_once(client, args.base_url, args.model, prompt)
            runs.append(r)
            print(f"  Run {i+1}: TTFT={r['ttft_ms']}ms  "
                  f"tokens={r['output_tokens']}  "
                  f"TPS={r['tps']}  "
                  f"total={r['total_s']}s")

        ttfts = [r["ttft_ms"] for r in runs if r["ttft_ms"] is not None]
        tps_list = [r["tps"] for r in runs if r["tps"] > 0]
        totals = [r["total_s"] for r in runs]
        out_tokens = [r["output_tokens"] for r in runs]

        summary = {
            "prompt": label,
            "input_tokens": input_tokens,
            "ttft_avg_ms": round(statistics.mean(ttfts), 1) if ttfts else None,
            "ttft_min_ms": round(min(ttfts), 1) if ttfts else None,
            "ttft_max_ms": round(max(ttfts), 1) if ttfts else None,
            "tps_avg": round(statistics.mean(tps_list), 1) if tps_list else None,
            "tps_min": round(min(tps_list), 1) if tps_list else None,
            "output_tokens_avg": round(statistics.mean(out_tokens)),
            "total_avg_s": round(statistics.mean(totals), 2),
        }
        results.append(summary)

    client.close()

    # Summary table
    print(f"\n{'=' * 80}")
    print("📊 Summary")
    print(f"{'=' * 80}")
    print(f"{'Input':<15} {'InTok':>6} {'TTFT avg':>10} {'TTFT min':>10} "
          f"{'OutTok':>7} {'TPS avg':>8} {'TPS min':>8} {'Total':>7}")
    print("-" * 80)
    for r in results:
        ttft_avg = f"{r['ttft_avg_ms']}ms" if r['ttft_avg_ms'] else "N/A"
        ttft_min = f"{r['ttft_min_ms']}ms" if r['ttft_min_ms'] else "N/A"
        tps_avg = f"{r['tps_avg']}" if r['tps_avg'] else "N/A"
        tps_min = f"{r['tps_min']}" if r['tps_min'] else "N/A"
        print(f"{r['prompt']:<15} {r['input_tokens']:>6} {ttft_avg:>10} {ttft_min:>10} "
              f"{r['output_tokens_avg']:>7} {tps_avg:>8} {tps_min:>8} {r['total_avg_s']:>6.2f}s")


if __name__ == "__main__":
    main()
