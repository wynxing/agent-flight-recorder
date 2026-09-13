"""成本估算。

只做小价格表估算，UI 上必须标注"估算"。没有匹配价格时返回 None，绝不猜。
"""

from __future__ import annotations

# 单位：USD / 1M tokens。价格会变，这只是本地估算用的快照。
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "o4-mini": (1.10, 4.40),
}


def normalize_model(model: str | None) -> str | None:
    if not model:
        return None
    name = model.strip().lower()
    if ":" in name:
        # 形如 openai:gpt-5 的标识，取冒号后的部分。
        name = name.split(":", 1)[1]
    return name or None


def estimate_cost(model: str | None, tokens_input: int | None, tokens_output: int | None) -> float | None:
    """返回估算成本（USD）。未知模型返回 None，表示"不知道"，而不是 0。"""

    name = normalize_model(model)
    if name is None:
        return None

    prices = PRICES.get(name)
    if prices is None:
        for known, known_prices in PRICES.items():
            if name.startswith(known):
                prices = known_prices
                break
    if prices is None:
        return None

    input_price, output_price = prices
    cost = (tokens_input or 0) / 1_000_000 * input_price
    cost += (tokens_output or 0) / 1_000_000 * output_price
    return round(cost, 6)

