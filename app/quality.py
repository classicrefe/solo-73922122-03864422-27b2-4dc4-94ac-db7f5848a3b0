"""质控放行核心算法：窗口判定 + 复测链完整性分析。

业务规则（实验室质控）
======================
1. 上机序列按注入顺序排列，混排质控针（QC）与样本针。
2. 每支样本针只看**前后最近的两支质控针**：
   - 两侧齐全且读数都落入合格闭区间 -> released（窗口合格）；
   - 缺任一侧质控 -> pending（待补）；
   - 两侧齐全但至少一侧失控 -> quarantined（隔离）。
3. 复测通过 retest_of 引用同样本更早的一针，引用关系构成有向森林
   （由 schema 校验保证：只指向存在的、同样本的、更早的样本针）。
4. 同一样本只放行**唯一复测链的末针**：
   - 恰有一条链（一个末针）且末针窗口合格 -> 放行末针读数，可入库；
   - 一支针被多支后续针引用（复测分叉）或存在多条无关联链 -> conflict；
   - 唯一末针待补 -> pending；唯一末针隔离 -> quarantined；
     末针不可入库时**不退回**链上任何更早的读数。

规模：序列 <= 48 针，全部判定均为线性扫描。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---- 窗口状态 ----
WINDOW_RELEASED = "released"
WINDOW_PENDING = "pending"
WINDOW_QUARANTINED = "quarantined"

# ---- 样本级处置状态 ----
SAMPLE_RELEASED = "released"
SAMPLE_PENDING = "pending"
SAMPLE_QUARANTINED = "quarantined"
SAMPLE_CONFLICT = "conflict"

# ---- 窗口原因 ----
REASON_MISSING_PREV_QC = "missing_prev_qc"
REASON_MISSING_NEXT_QC = "missing_next_qc"
REASON_MISSING_BOTH_QC = "missing_both_qc"
REASON_QC_OUT_OF_CONTROL = "qc_out_of_control"


@dataclass(frozen=True)
class QCNeedle:
    """质控针内部模型。"""

    injection_id: str
    order: int               # 0 基位序
    reading: float
    in_control: bool         # 读数是否落入合格闭区间


@dataclass(frozen=True)
class SampleNeedle:
    """样本针内部模型。"""

    injection_id: str
    order: int
    sample_id: str
    reading: float
    retest_of: str | None


@dataclass(frozen=True)
class NeedleVerdict:
    """单支样本针的窗口判定结果。"""

    injection_id: str
    order: int
    window_status: str
    window_reason: str | None
    prev_qc_id: str | None
    next_qc_id: str | None
    is_chain_tail: bool


@dataclass(frozen=True)
class SampleVerdict:
    """单个样本的最终放行结论。"""

    sample_id: str
    status: str
    reason_code: str | None
    reason_message: str | None
    conflict_kind: str | None
    released_injection_id: str | None
    released_reading: float | None
    chain_tail_ids: tuple[str, ...]
    needles: tuple[NeedleVerdict, ...]


@dataclass(frozen=True)
class QualityAnalysis:
    """整段序列的放行分析结果。"""

    qc_needles: tuple[QCNeedle, ...]
    sample_needles: tuple[SampleNeedle, ...]
    verdicts: tuple[SampleVerdict, ...]
    qc_in_control_count: int
    qc_out_of_control_count: int


def _window_status(
    order: int,
    prev_qc: QCNeedle | None,
    next_qc: QCNeedle | None,
) -> tuple[str, str | None]:
    """按前后最近质控针判定一支样本针的窗口状态。"""
    if prev_qc is None and next_qc is None:
        return WINDOW_PENDING, REASON_MISSING_BOTH_QC
    if prev_qc is None:
        return WINDOW_PENDING, REASON_MISSING_PREV_QC
    if next_qc is None:
        return WINDOW_PENDING, REASON_MISSING_NEXT_QC
    if prev_qc.in_control and next_qc.in_control:
        return WINDOW_RELEASED, None
    return WINDOW_QUARANTINED, REASON_QC_OUT_OF_CONTROL


def analyze_quality(
    qc_needles: list[QCNeedle],
    sample_needles: list[SampleNeedle],
) -> QualityAnalysis:
    """对整段上机序列做质控放行分析。

    入参针次的 order 即其在原序列中的 0 基位序，调用方需保证 order 唯一且
    覆盖 0..N-1；样本针的 retest_of 已通过 schema 跨字段校验。
    """
    # order 为全序列位序（质控针与样本针共享），数组按最大位序 +1 分配
    all_orders = [q.order for q in qc_needles] + [s.order for s in sample_needles]
    n = (max(all_orders) + 1) if all_orders else 0

    # 1) 每个位序上前后最近的质控针（线性扫描两遍）
    prev_qc_at: list[QCNeedle | None] = [None] * n
    nearest: QCNeedle | None = None
    qc_by_order = {q.order: q for q in qc_needles}
    for i in range(n):
        if i in qc_by_order:
            nearest = qc_by_order[i]
        else:
            prev_qc_at[i] = nearest
    next_qc_at: list[QCNeedle | None] = [None] * n
    nearest = None
    for i in range(n - 1, -1, -1):
        if i in qc_by_order:
            nearest = qc_by_order[i]
        else:
            next_qc_at[i] = nearest

    # 2) 按样本分组，保留首次出现顺序
    grouped: dict[str, list[SampleNeedle]] = {}
    for s in sample_needles:
        grouped.setdefault(s.sample_id, []).append(s)

    # 3) 逐样本做复测链完整性分析
    verdicts: list[SampleVerdict] = []
    for sample_id, needles in grouped.items():
        by_id = {s.injection_id: s for s in needles}
        # 被引用次数：>1 即复测分叉
        ref_count: dict[str, int] = {s.injection_id: 0 for s in needles}
        # 根 = 没有 retest_of 的针；根的数量即无关联链数
        root_ids = sorted(s.injection_id for s in needles if s.retest_of is None)
        for s in needles:
            if s.retest_of is not None:
                ref_count[s.retest_of] += 1
        fork_ids = sorted(iid for iid, c in ref_count.items() if c > 1)
        # 末针 = 没有任何后续针引用它
        tail_needles = sorted(
            (s for s in needles if ref_count[s.injection_id] == 0),
            key=lambda s: s.order,
        )
        tail_ids = tuple(s.injection_id for s in tail_needles)
        has_fork = bool(fork_ids)
        has_multiple_chains = len(root_ids) > 1

        needle_verdicts: list[NeedleVerdict] = []
        for s in needles:
            prev_qc = prev_qc_at[s.order]
            next_qc = next_qc_at[s.order]
            status, reason = _window_status(s.order, prev_qc, next_qc)
            needle_verdicts.append(
                NeedleVerdict(
                    injection_id=s.injection_id,
                    order=s.order,
                    window_status=status,
                    window_reason=reason,
                    prev_qc_id=prev_qc.injection_id if prev_qc else None,
                    next_qc_id=next_qc.injection_id if next_qc else None,
                    is_chain_tail=ref_count[s.injection_id] == 0,
                )
            )
        needle_verdicts.sort(key=lambda v: v.order)
        window_by_id = {v.injection_id: v.window_status for v in needle_verdicts}

        # 4) 样本级结论
        status: str
        reason_code: str | None = None
        reason_message: str | None = None
        conflict_kind: str | None = None
        released_id: str | None = None
        released_reading: float | None = None

        if has_fork or has_multiple_chains:
            status = SAMPLE_CONFLICT
            if has_fork and has_multiple_chains:
                conflict_kind = "both"
            elif has_fork:
                conflict_kind = "retest_fork"
            else:
                conflict_kind = "multiple_unrelated_chains"
            reason_code = "chain_conflict"
            parts = []
            if has_fork:
                parts.append(
                    "复测分叉：注入号 "
                    + "、".join(fork_ids)
                    + " 被多支后续针同时引用，无法确定唯一复测链"
                )
            if has_multiple_chains:
                parts.append(
                    "多条无关联复测链：存在 "
                    + str(len(root_ids))
                    + " 个互不引用的起始针（"
                    + "、".join(root_ids)
                    + "），末针为 "
                    + "、".join(tail_ids)
                )
            parts.append("全部针次扣留，待质控员人工核定后重新组织复测链")
            reason_message = "；".join(parts)
        else:
            # 唯一链、唯一末针：只看末针窗口，绝不退回旧读数
            tail = tail_needles[0]
            tail_window = window_by_id[tail.injection_id]
            if tail_window == WINDOW_RELEASED:
                status = SAMPLE_RELEASED
                released_id = tail.injection_id
                released_reading = tail.reading
            elif tail_window == WINDOW_PENDING:
                status = SAMPLE_PENDING
                reason_code = "tail_pending"
                reason_message = (
                    f"唯一复测链末针 {tail.injection_id} 缺少前/后侧最近质控针，"
                    "待补质控后重判；末针未放行，不退回链上更早读数"
                )
            else:
                status = SAMPLE_QUARANTINED
                reason_code = "tail_quarantined"
                reason_message = (
                    f"唯一复测链末针 {tail.injection_id} 前后最近质控针至少一侧失控，"
                    "整链隔离；不退回链上更早读数"
                )

        verdicts.append(
            SampleVerdict(
                sample_id=sample_id,
                status=status,
                reason_code=reason_code,
                reason_message=reason_message,
                conflict_kind=conflict_kind,
                released_injection_id=released_id,
                released_reading=released_reading,
                chain_tail_ids=tail_ids,
                needles=tuple(needle_verdicts),
            )
        )

    qc_in_control = sum(1 for q in qc_needles if q.in_control)
    return QualityAnalysis(
        qc_needles=tuple(sorted(qc_needles, key=lambda q: q.order)),
        sample_needles=tuple(sorted(sample_needles, key=lambda s: s.order)),
        verdicts=tuple(verdicts),
        qc_in_control_count=qc_in_control,
        qc_out_of_control_count=len(qc_needles) - qc_in_control,
    )
