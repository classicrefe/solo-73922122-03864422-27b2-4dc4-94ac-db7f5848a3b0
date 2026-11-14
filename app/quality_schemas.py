"""/quality-release 质控放行接口的请求/响应模型。"""

from __future__ import annotations

import math
from typing import Annotated, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MAX_INJECTIONS = 48


def _finite_float(name: str):
    """生成校验有限实数的 field_validator（错误信息不携带非法值，保证 422 可序列化）。"""

    @field_validator(name)
    @classmethod
    def _check(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError(f"{name} 必须是有限实数，不接受 NaN 或无穷大")
        return v

    return _check

# 单针窗口状态
WINDOW_RELEASED = "released"        # 前后最近质控齐全且均合格
WINDOW_PENDING = "pending"          # 缺任一侧质控，待补
WINDOW_QUARANTINED = "quarantined"  # 两侧齐全但至少一侧失控

# 样本级处置状态
SAMPLE_RELEASED = "released"
SAMPLE_PENDING = "pending"
SAMPLE_QUARANTINED = "quarantined"
SAMPLE_CONFLICT = "conflict"

WindowStatus = Literal["released", "pending", "quarantined"]
SampleStatus = Literal["released", "pending", "quarantined", "conflict"]
WindowReason = Literal[
    "missing_prev_qc",
    "missing_next_qc",
    "missing_both_qc",
    "qc_out_of_control",
]
ReasonCode = Literal["chain_conflict", "tail_pending", "tail_quarantined"]
ConflictKind = Literal["retest_fork", "multiple_unrelated_chains", "both"]


class QCInjectionIn(BaseModel):
    """质控针：有序列内唯一注入号与一个有限读数。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["qc"]
    injection_id: str = Field(
        min_length=1,
        max_length=64,
        description="注入号，整个上机序列内唯一",
        examples=["QC-01"],
    )
    reading: float = Field(
        description="质控读数（有限实数），按 acceptance_range 闭区间判定合格",
        examples=[10.2],
    )

    _finite_reading = _finite_float("reading")


class SampleInjectionIn(BaseModel):
    """样本针：带样本号；复测针通过 retest_of 引用同样本更早的一针。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["sample"]
    injection_id: str = Field(
        min_length=1,
        max_length=64,
        description="注入号，整个上机序列内唯一",
        examples=["I-02"],
    )
    sample_id: str = Field(
        min_length=1,
        max_length=64,
        description="样本号，同一样本的多针（含复测）共享",
        examples=["SMP-A"],
    )
    reading: float = Field(
        description="样本读数（有限实数）",
        examples=[5.5],
    )
    retest_of: str | None = Field(
        default=None,
        description=(
            "复测引用：同样本更早一针的注入号；"
            "首针（未复测）留空。引用必须指向已存在的、同样本的更早样本针"
        ),
    )

    _finite_reading = _finite_float("reading")


InjectionIn = Annotated[
    Union[QCInjectionIn, SampleInjectionIn],
    Field(discriminator="type"),
]


class AcceptanceRange(BaseModel):
    """质控读数的合格闭区间 [lower, upper]，含边界。"""

    model_config = ConfigDict(extra="forbid")

    lower: float = Field(
        description="合格闭区间下限（含边界）", examples=[9.0]
    )
    upper: float = Field(
        description="合格闭区间上限（含边界）", examples=[11.0]
    )

    _finite_lower = _finite_float("lower")
    _finite_upper = _finite_float("upper")

    @model_validator(mode="after")
    def _bounds_ordered(self) -> "AcceptanceRange":
        if self.lower > self.upper:
            raise ValueError("acceptance_range 的下限不得大于上限")
        return self


class QualityReleaseRequest(BaseModel):
    """质控放行请求：按注入顺序排列的质控针/样本针（最多 48 针）+ 合格闭区间。"""

    model_config = ConfigDict(extra="forbid")

    injections: list[InjectionIn] = Field(
        min_length=1,
        max_length=MAX_INJECTIONS,
        description="按实际上机注入顺序排列的针次，最多 48 针",
    )
    acceptance_range: AcceptanceRange = Field(
        description="质控读数合格闭区间，落在区间内（含边界）的质控针判为在控"
    )

    @model_validator(mode="after")
    def _cross_field_checks(self) -> "QualityReleaseRequest":
        ids = [inj.injection_id for inj in self.injections]
        if len(ids) != len(set(ids)):
            raise ValueError("injection_id 在整个上机序列内必须唯一")
        index_of = {iid: i for i, iid in enumerate(ids)}

        for inj in self.injections:
            if inj.type != "sample" or inj.retest_of is None:
                continue
            ref_id = inj.retest_of
            if ref_id not in index_of:
                raise ValueError(
                    f"注入号 {inj.injection_id} 的 retest_of={ref_id} "
                    "指向的注入号不存在"
                )
            if index_of[ref_id] >= index_of[inj.injection_id]:
                raise ValueError(
                    f"注入号 {inj.injection_id} 的复测引用 {ref_id} "
                    "必须是更早注入的一针"
                )
            target = self.injections[index_of[ref_id]]
            if target.type != "sample":
                raise ValueError(
                    f"注入号 {inj.injection_id} 的复测引用 {ref_id} 指向质控针，"
                    "复测只能引用同样本的样本针"
                )
            if target.sample_id != inj.sample_id:
                raise ValueError(
                    f"注入号 {inj.injection_id} 的复测引用 {ref_id} "
                    f"属于不同样本（{target.sample_id} ≠ {inj.sample_id}），"
                    "复测只能引用同样本的更早一针"
                )
        return self


class NeedleDetailOut(BaseModel):
    """单支样本针的窗口判定与复测链位置。"""

    injection_id: str = Field(description="注入号")
    sample_id: str = Field(description="所属样本号")
    reading: float = Field(description="该针读数")
    order: int = Field(description="在整个上机序列中的 1 基位序")
    retest_of: str | None = Field(description="复测引用的更早注入号，首针为 null")
    window_status: WindowStatus = Field(
        description=(
            "released=前后最近质控齐全且均合格；"
            "pending=缺任一侧质控，待补；"
            "quarantined=两侧齐全但至少一侧失控"
        )
    )
    window_reason: WindowReason | None = Field(
        default=None,
        description="window_status 非 released 时的具体原因；released 时为 null",
    )
    prev_qc_id: str | None = Field(description="前侧最近质控针注入号，缺失为 null")
    next_qc_id: str | None = Field(description="后侧最近质控针注入号，缺失为 null")
    is_chain_tail: bool = Field(
        description="是否为所属复测链的末针（无后续复测引用它）"
    )


class SampleReleaseOut(BaseModel):
    """单个样本的放行结论。"""

    sample_id: str
    status: SampleStatus = Field(
        description=(
            "released=可入库（唯一复测链末针且窗口合格）；"
            "pending=末针待补质控；"
            "quarantined=末针被失控质控隔离；"
            "conflict=复测链不唯一（分叉或多条无关联链）"
        )
    )
    reason_code: ReasonCode | None = Field(
        default=None, description="非 released 时的机器可读原因码"
    )
    reason_message: str | None = Field(
        default=None, description="非 released 时的中文扣留/冲突说明"
    )
    conflict_kind: ConflictKind | None = Field(
        default=None,
        description=(
            "status=conflict 时的冲突类型："
            "retest_fork=同一复测树中一针被多针引用（复测分叉）；"
            "multiple_unrelated_chains=存在多个互不引用的起始针（多条无关联链）；"
            "both=两种情形同时存在"
        ),
    )
    released_injection_id: str | None = Field(
        default=None, description="status=released 时放行的复测链末针注入号"
    )
    released_reading: float | None = Field(
        default=None, description="status=released 时可入库的末针读数"
    )
    chain_tail_ids: list[str] = Field(
        description="复测链末针注入号列表（唯一链恰 1 个，冲突时可能多个）"
    )
    needle_count: int = Field(description="该样本在本序列中的针次数")
    needles: list[NeedleDetailOut] = Field(
        description="该样本全部针次的逐针判定，按注入顺序排列"
    )


class QualitySummaryOut(BaseModel):
    total_injections: int = Field(description="序列总针数（质控针 + 样本针）")
    qc_count: int = Field(description="质控针数量")
    qc_in_control_count: int = Field(description="在控（读数落入合格闭区间）质控针数")
    qc_out_of_control_count: int = Field(description="失控质控针数")
    sample_injection_count: int = Field(description="样本针总针次")
    sample_count: int = Field(description="涉及的不同样本数")
    released_count: int = Field(description="可入库样本数")
    pending_count: int = Field(description="待补质控样本数")
    quarantined_count: int = Field(description="隔离样本数")
    conflict_count: int = Field(description="复测链冲突样本数")


class QualityReleaseResponse(BaseModel):
    summary: QualitySummaryOut
    acceptance_range: AcceptanceRange = Field(description="回显请求中的合格闭区间")
    samples: list[SampleReleaseOut] = Field(
        description="各样本的放行结论，按样本首次出现顺序排列"
    )
