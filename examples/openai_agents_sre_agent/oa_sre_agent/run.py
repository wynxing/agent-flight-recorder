"""命令行跑一次示例场景并上报到本地平台。

    python -m oa_sre_agent.run --endpoint http://127.0.0.1:7710
    python -m oa_sre_agent.run --prompt grounded     # 用修正后的 Prompt
"""

from __future__ import annotations

import argparse

from agent_flight_recorder import Recorder
from agent_flight_recorder.client import DEFAULT_ENDPOINT

from .agent import run_scenario
from .prompts import DEFAULT_SYSTEM_PROMPT, GROUNDED_SYSTEM_PROMPT
from .scripted_model import SCRIPTED_MODEL_NAME

PROMPTS = {"default": DEFAULT_SYSTEM_PROMPT, "grounded": GROUNDED_SYSTEM_PROMPT}


def main() -> None:
    parser = argparse.ArgumentParser(description="跑一次示例 SRE 调查场景并录制（OpenAI Agents SDK 版）")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--prompt", choices=sorted(PROMPTS), default="default")
    parser.add_argument("--model", default=SCRIPTED_MODEL_NAME)
    parser.add_argument("--offline", action="store_true", help="不实际上报，只本地跑一遍")
    args = parser.parse_args()

    recorder = Recorder(
        "checkout-api-sre-agents-sdk",
        endpoint=args.endpoint,
        model=args.model,
        agent_version="0.1.0",
        prompt_version=args.prompt,
        enabled=not args.offline,
    )
    try:
        run_id = run_scenario(
            recorder,
            system_prompt=PROMPTS[args.prompt],
            model=args.model,
        )
        print(f"run_id: {run_id}")
        print(f"stats : {recorder.stats.as_dict()}")
        if not recorder.stats.healthy:
            print("提示：录制链路有失败，可以用 afr.doctor() 做一次端到端自检")
    finally:
        recorder.close()


if __name__ == "__main__":
    main()
