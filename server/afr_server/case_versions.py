"""用例集版本：让一个批次的结论**自证它跑的是哪一版用例**。

## 这个问题为什么必须回答

批量套件交付之后，「上一批 20 条里 18 条通过」这句话仍然没有前提：套件只存了
`case_ids`，而 `cases` 行是可变的——断言、起点、副作用策略、模型都能被改写。
用例一旦被编辑，历史批次的那 18 条就说不清是按哪一版断言判的，趋势也就无从谈起。

这里补上的是**归属**：提交套件的那一刻，把它用到的用例定义固化成一个可寻址的版本，
套件与每个格子都带着它。归属不是分数，它不参与任何计数与比率（见 PRD 6.5）。

## 规范化方案 cs1

**版本标识是内容决定的**：`cs1:<sha256(规范化 JSON)>`。同一个标识必然对应同一份内容，
因此「两个套件是不是同一版用例」由标识本身回答。标识的前缀就是规范化方案的版本号，
以后换算法就换前缀（cs2），旧标识仍然按 cs1 解释，不会变成一句无从考证的字符串。

**哪些字段计入**（判据相关的那一面，即能改变四态结论的东西）：

```
source_run_id   回放的是哪一次运行
from_seq        从第几步开始
to_seq          声明到第几步为止（目前执行层只用 from_seq，但它是用例声明的边界：
                以后执行层一旦采用它，历史版本的归属不该突然失效，所以计入）
assertions      断言集（先按 AssertionSpec 归一化，见下）
effect_policy   {preset, policy, model, system_prompt}
```

**哪些字段不计入**，以及为什么：

```
name / description  展示用。改个名字不是「换了一份定义」；把它算进去只会让
                    每一次改名都触发一句「用例变了」，把可信的告警淹成噪音。
labels              筛选用的元数据，不参与任何一条断言的判定。
created_at          出生时间，不是判据。
last_*              **执行记账**（最近一次结论、时间、成因、条件、定义摘要）。
                    它们每次跑完都会变：算进去的话，同一份定义跑第二遍就会得到
                    一个新的版本号，版本化立刻变成一句废话。
```

**字典键序无关，列表顺序有关**：规范化 JSON 会排序字典键，因此
`{"type": "no_error", "note": ""}` 与 `{"note": "", "type": "no_error"}` 是同一份定义；
而断言列表的顺序参与标识——它同时决定断言结果列表的顺序，是判据呈现的一部分。

**用例的提交顺序无关**：版本是「这一组定义的集合」，成员按 `case_id` 排序后参与摘要。
「这批怎么排」由套件自己的 `case_ids` / 格子的 `position` 记录，与「这是哪一版定义」是两件事。

**断言的归一化**：断言先按 `AssertionSpec` 解析再计入摘要，因此省略字段与显式写 null
（`{"type": "no_error"}` 与 `{"type": "no_error", "value": null, "tool": null, ...}`）
是同一份定义。解析不了的旧数据照原样参与摘要，绝不因此让一次提交失败。

## 定义与「有效定义」

用例行上的那一份是**定义**（`definition_digest`）；一次执行真正按着跑的那一份是
**有效定义** = 定义 ⊕ 这次执行**显式给出的回放覆盖**（见 `run_overrides`）。

两者必须分开记，因为覆盖是真实存在的：单条运行可以指定 `from_seq` / `preset` / `policy` /
`model` / `system_prompt`，批量格子还会把当列条件（模型、Prompt 正文）带下来。只记基础定义、
却把结论说成「是在这一版定义下得出的」，就是**错误归属**：那条结论其实不是在它上面判出来的。
因此 `last_definition_digest` 记的是**有效定义**，`last_definition_overrides` 记的是覆盖本身
（结构化；空 = 没有覆盖）。

**没有覆盖时两者相同**，因此默认路径下「最近一次结论所用的定义」与用例自己的定义（同样也是
用例集版本里那个成员摘要）可以直接比对。**有覆盖时必然不同**，此时读者要看的是覆盖那一列。

**预算不计入有效定义。** 它约束的是「这次跑多少 / 跑没跑完」，不是「判据是什么」：因预算停下
的那次结论已经由成因码 `budget_exceeded` 与回放 Run 上的记账如实表达，再把它塞进定义摘要，
只会让同一个数字既表示判据、又表示额度。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from agent_flight_recorder.models import utcnow

from .assertions import AssertionSpec
from .storage import aware_utc, get_case, get_case_set_version
from .tables import CaseSetVersionTable, CaseTable

#: 规范化方案的版本号。它同时是标识前缀：换算法就换前缀，旧标识仍可被解释。
CANONICALIZATION = "cs1"
#: 用例集版本标识的前缀（`cs1:<hex>`）。
VERSION_PREFIX = f"{CANONICALIZATION}:"
#: 单条用例定义摘要的前缀（`cs1m:<hex>`）。m = member，即「某版里的一个成员」。
MEMBER_PREFIX = f"{CANONICALIZATION}m:"

#: 一次执行可以显式覆盖的回放前提。它们与 `snapshot_of` 的字段**同名**，因此合并就是同名字段
#: 覆盖（见 `effective_snapshot`）——覆盖与定义的字段名不搞第二套，避免两处各写一份映射。
OVERRIDE_FIELDS: tuple[str, ...] = ("from_seq", "preset", "policy", "model", "system_prompt")


def canonical_json(payload: Any) -> str:
    """规范化 JSON：字典键排序、无多余空白。

    这是「内容决定」这句话的全部实现——同一份内容的两种写法必须落成同一个字符串，
    否则摘要会因为无关的书写差异而变。
    """

    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def snapshot_of(case: CaseTable) -> dict[str, Any]:
    """一条用例被执行时需要的全部输入。

    批次在提交那一刻取一次快照，之后每个格子都跑这一份，因此用例在**批次执行期间**
    被编辑不会造成「前半批按旧断言、后半批按新断言，却报成一个数」。
    """

    policy = case.effect_policy or {}
    return {
        "id": case.id,
        "name": case.name,
        "source_run_id": case.source_run_id,
        "from_seq": case.from_seq,
        "to_seq": case.to_seq,
        "assertions": list(case.assertions or []),
        "labels": dict(case.labels or {}),
        "preset": policy.get("preset"),
        "policy": policy.get("policy"),
        "model": policy.get("model"),
        "system_prompt": policy.get("system_prompt"),
    }


def _canonical_assertions(items: Iterable[Any]) -> list[Any]:
    """断言按 AssertionSpec 归一化后再计入摘要。

    归一化的是**书写形式**而不是语义：省略字段与显式 null 是同一条断言（两边在仓库里都
    真实存在——接口建用例走模型、Agent 自带用例走原始 dict）。解析不了的存量数据原样参与，
    摘要只要求「同一份内容给出同一个字符串」，不需要它一定看得懂。
    """

    canonical: list[Any] = []
    for item in items:
        try:
            canonical.append(AssertionSpec.model_validate(item).model_dump(mode="json"))
        except Exception:  # noqa: BLE001 - 摘要不该因为一条坏断言而算不出来
            canonical.append(item)
    return canonical


def definition_of(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """从用例快照里取出**判据相关**的那一面（版本标识就是它的摘要）。

    只列字段本身，不做任何补默认值的动作：`from_seq` 是 null 就如实是 null，
    不替它写一个 1——版本要反映的是这一行当时的内容，不是我们对它的理解。
    """

    return {
        "source_run_id": snapshot.get("source_run_id"),
        "from_seq": snapshot.get("from_seq"),
        "to_seq": snapshot.get("to_seq"),
        "assertions": _canonical_assertions(snapshot.get("assertions") or []),
        "effect_policy": {
            "preset": snapshot.get("preset"),
            "policy": snapshot.get("policy"),
            "model": snapshot.get("model"),
            "system_prompt": snapshot.get("system_prompt"),
        },
    }


def run_overrides(
    *,
    from_seq: int | None = None,
    preset: Any = None,
    policy: Any = None,
    model: str | None = None,
    system_prompt: str | None = None,
    budget: Any = None,
) -> dict[str, Any]:
    """这次执行**显式给出**的回放覆盖（没给的就是没给，不补默认值）。

    None 一律读作「调用方没有指定」，因此它不会出现在结果里：用例定义里那个值照旧生效。
    这与 `resolve_plan` 一直以来的读法一致（`from_seq or ...`、`system_prompt if ... is not None`），
    这里只是把同一件事**记下来**。

    `preset` / `policy` 落成 JSON 友好的取值（枚举取 `.value`、模型取 json dump），因为覆盖
    要与定义一起进摘要：同一个语义在摘要里必须是同一个字符串。

    `budget` **不参与**：它不改变判据，只约束这次跑多少（见模块说明）。它在这里被显式收下，
    是为了让调用方一眼看到「预算不算覆盖」这件事是有意为之，而不是漏了。
    """

    overrides: dict[str, Any] = {}
    if from_seq is not None:
        overrides["from_seq"] = int(from_seq)
    if preset is not None:
        overrides["preset"] = getattr(preset, "value", None) or str(preset)
    if policy is not None:
        overrides["policy"] = (
            policy if isinstance(policy, Mapping) else policy.model_dump(mode="json")
        )
    if model is not None:
        overrides["model"] = model
    if system_prompt is not None:
        overrides["system_prompt"] = system_prompt
    del budget  # 只为了在签名里露面（见上），不参与覆盖
    return overrides


def effective_snapshot(snapshot: Mapping[str, Any], overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """有效定义：基础定义盖上这次执行的覆盖。

    **这是覆盖唯一被合并的地方。** 计划与记录都必须走这里，否则「按什么跑」与「记成什么」
    会各说一套——那正是「实际从第 1 步回放、却把结论记成第 15 步那一版」的成因。

    只认 `OVERRIDE_FIELDS` 里的字段：写错一个键（例如 `fromSeq`）会被当场拒绝，而不是安静地
    留下一个「摘要变了、执行没变」的幽灵差异。
    """

    merged = dict(snapshot)
    for name, value in dict(overrides or {}).items():
        if name not in OVERRIDE_FIELDS:
            raise ValueError(f"unknown override: {name}")
        merged[name] = value
    return merged


def effective_digest(snapshot: Mapping[str, Any], overrides: Mapping[str, Any] | None = None) -> str:
    """有效定义的摘要：`last_definition_digest` 记的就是它。"""

    return member_digest(definition_of(effective_snapshot(snapshot, overrides)))


def member_digest(definition: Mapping[str, Any]) -> str:
    """单条用例定义的摘要（`cs1m:<hex>`）。"""

    return MEMBER_PREFIX + hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()


def member_of(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    definition = definition_of(snapshot)
    return {
        "case_id": str(snapshot["id"]),
        "digest": member_digest(definition),
        "definition": definition,
    }


def _members(snapshots: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """成员按 case_id 排序：用例的提交顺序不参与版本标识（见模块说明）。"""

    return sorted((member_of(snapshot) for snapshot in snapshots), key=lambda item: item["case_id"])


def case_set_version_id(snapshots: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]]) -> str:
    """这一组用例定义的版本标识（纯计算，不读库）。

    先算每条用例自己的摘要，再对「成员表」求摘要：因此只改其中一条用例就会得到一个新的
    版本号，而把同样的用例换个顺序提交仍然落在同一个版本上。
    """

    members = [
        {"case_id": item["case_id"], "digest": item["digest"]} for item in _members(snapshots)
    ]
    payload = {"canonicalization": CANONICALIZATION, "members": members}
    return VERSION_PREFIX + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def record_version(session: Any, snapshots: Sequence[Mapping[str, Any]]) -> str:
    """把这一组定义固化成一个版本，返回它的标识。

    标识已是主键，因此内容相同的第二次提交什么都不用写（内容决定 = 天然去重）。
    与套件行在同一个事务里提交：新套件不可能出现「有版本标识、没有定义可反查」的状态。
    """

    members = _members(snapshots)
    version_id = case_set_version_id(snapshots)
    if session.get(CaseSetVersionTable, version_id) is None:
        session.add(
            CaseSetVersionTable(
                id=version_id,
                canonicalization=CANONICALIZATION,
                case_count=len(members),
                cases=members,
                created_at=utcnow(),
            )
        )
        session.flush()
    return version_id


def definition_digest_of(snapshot: Mapping[str, Any]) -> str:
    """一条用例**当前**定义的摘要（用例行那一份）。"""

    return member_digest(definition_of(snapshot))


def definition_digest_of_case(case: CaseTable) -> str:
    return definition_digest_of(snapshot_of(case))


def drift_of(session: Any, version: CaseSetVersionTable) -> dict[str, Any]:
    """这一版里的用例，现在还是不是当时那一份。

    这是「用例变了」这句话的唯一出处：拿**当前行**的定义摘要与版本里固化的摘要比。
    它只影响提示，不影响任何一条已经跑出来的结论——那些结论的归属永远在提交那一刻。
    """

    changed: list[str] = []
    missing: list[str] = []
    unchanged = 0
    for member in version.cases or []:
        case_id = str(member.get("case_id"))
        row = get_case(session, case_id)
        if row is None:
            # 行不在了（删库/换库/手工清理）。如实归到「找不到了」，不假装它没变过。
            missing.append(case_id)
            continue
        if definition_digest_of_case(row) != member.get("digest"):
            changed.append(case_id)
        else:
            unchanged += 1
    return {"changed": changed, "missing": missing, "unchanged": unchanged}


def case_set_payload(session: Any, suite: Any) -> dict[str, Any] | None:
    """套件详情里那一块「这批跑的是哪一版用例集」。

    没有版本记录时返回 None：这是历史批次（版本化之前建的）的真实状态，读出来就是
    「无版本记录」，不按当前用例反推一个版本号塞回去。
    """

    version_id = getattr(suite, "case_set_version", None)
    if not version_id:
        return None
    version = get_case_set_version(session, version_id)
    if version is None:
        # 标识在、定义行不在（导出的库、手工清理）。既不能假装「无版本记录」（那是另一句
        # 话），也不能假装定义还在，只能如实说：这版定义现在反查不到。
        return {
            "id": version_id,
            "canonicalization": CANONICALIZATION,
            "case_count": len(suite.case_ids or []),
            "recorded": False,
            "recorded_at": None,
            "drift": None,
        }
    return {
        "id": version.id,
        "canonicalization": version.canonicalization,
        "case_count": version.case_count,
        "recorded": True,
        "recorded_at": aware_utc(version.created_at),
        "drift": drift_of(session, version),
    }


def case_set_version_payload(session: Any, version_id: str) -> dict[str, Any] | None:
    """按标识取回当时固化的用例定义（旧套件的可反查路径）。"""

    version = get_case_set_version(session, version_id)
    if version is None:
        return None
    return {
        "id": version.id,
        "canonicalization": version.canonicalization,
        "case_count": version.case_count,
        "recorded_at": aware_utc(version.created_at),
        "cases": [
            {
                "case_id": member.get("case_id"),
                "digest": member.get("digest"),
                "definition": dict(member.get("definition") or {}),
            }
            for member in (version.cases or [])
        ],
    }


__all__ = [
    "CANONICALIZATION",
    "MEMBER_PREFIX",
    "VERSION_PREFIX",
    "canonical_json",
    "case_set_payload",
    "case_set_version_id",
    "case_set_version_payload",
    "definition_digest_of",
    "definition_digest_of_case",
    "definition_of",
    "drift_of",
    "effective_digest",
    "effective_snapshot",
    "member_digest",
    "member_of",
    "record_version",
    "run_overrides",
    "snapshot_of",
]
