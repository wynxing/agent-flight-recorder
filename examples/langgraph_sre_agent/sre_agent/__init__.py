"""示例：一个真实的 LangGraph SRE 调查 Agent，用 Agent Flight Recorder 录制。

场景取自一个真实可复现的故障模式：日志里的报错是症状，真正的根因在同期部署
引入的配置回归里。Agent 会看到全部证据，但可能把症状当成根因。
"""

from .agent import AGENT_NAME, build_sre_agent, agent_spec, run_scenario
from .prompts import DEFAULT_SYSTEM_PROMPT, GROUNDED_SYSTEM_PROMPT
from .scripted_model import SCRIPTED_MODEL_NAME, ScriptedChatModel
from .tools import ALL_TOOLS, TOOL_SIDE_EFFECTS

__all__ = [
    "AGENT_NAME",
    "ALL_TOOLS",
    "DEFAULT_SYSTEM_PROMPT",
    "GROUNDED_SYSTEM_PROMPT",
    "SCRIPTED_MODEL_NAME",
    "ScriptedChatModel",
    "TOOL_SIDE_EFFECTS",
    "agent_spec",
    "build_sre_agent",
    "run_scenario",
]

