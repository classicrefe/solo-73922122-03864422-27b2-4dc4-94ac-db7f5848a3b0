"""FastAPI 入口：采样登记 / 仪器检测记录配对服务 + 质控放行服务。"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime

from fastapi import Body, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from .examples import DEMO_EXAMPLES
from .matching import Analysis, Record, analyze
from .quality import (
    QCNeedle,
    QualityAnalysis,
    SampleNeedle,
    analyze_quality,
)
from .quality_examples import QUALITY_DEMO_EXAMPLES
from .quality_schemas import (
    AcceptanceRange,
    NeedleDetailOut,
    QualityReleaseRequest,
    QualityReleaseResponse,
    QualitySummaryOut,
    SampleReleaseOut,
)
from .schemas import (
    CertainPairOut,
    MatchRequest,
    MatchResponse,
    PartnerOptionOut,
    RecordIn,
    RecordVerdictOut,
    StatusCounts,
    SummaryOut,
)

app = FastAPI(
    title="采样-检测记录配对 / 质控放行服务",
    description=(
        "两个接口：\n\n"
        "1. `POST /match`：把人工采样登记与仪器检测记录按'同条码 + UTC 时间差 ≤ 10 分钟'"
        "配对，每条记录至多用一次；先最大化配对数，再最小化时间差总和。"
        "只把**所有同等最优方案中都成立**的配对列为确定配对（certain），"
        "其余记录区分为待核查（review）与始终未配对（unmatched），"
        "不会任选一套最优方案冒充确定结论。\n\n"
        "2. `POST /quality-release`：质控员从同一检测项目的上机序列（最多 48 针，"
        "按注入顺序排列的质控针与样本针）中选出可入分析库的读数。"
        "样本针仅在前后最近的两支质控针都合格时窗口可用；缺任一侧质控待补，"
        "两侧齐全而任一侧失控则隔离。同一样本只放行**唯一复测链的末针**，"
        "复测分叉或多条无关联链判冲突，末针待补/隔离时不退回旧读数。\n\n"
        "两个接口的 Examples 下拉框均内置演示请求。"
    ),
    version="1.1.0",
)


def _sanitize_non_finite(obj):
    """递归把 NaN/Infinity 替换为字符串，保证错误响应是合法 JSON。

    pydantic 对非有限数的校验错误会在 input 字段回显非法浮点值，
    Starlette 默认 JSONResponse 以 allow_nan=False 序列化会抛 500，
    因此在 422 处理器中先净化。
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else f"<non-finite:{obj}>"
    if isinstance(obj, dict):
        return {k: _sanitize_non_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_non_finite(v) for v in obj]
    return obj


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # jsonable_encoder 先把 ctx 中的异常对象等转为可序列化结构，
    # 再净化非有限浮点，保证返回合法 JSON 的 422
    encoded = jsonable_encoder(exc.errors())
    return JSONResponse(
        status_code=422,
        content={"detail": _sanitize_non_finite(encoded)},
    )


def _to_ms(dt: datetime) -> int:
    """带时区的 datetime -> UTC epoch 毫秒。"""
    return round(dt.timestamp() * 1000)


def _verdict_out(rec_in: RecordIn, analysis: Analysis, is_sampling: bool) -> RecordVerdictOut:
    verdicts = analysis.sampling if is_sampling else analysis.detection
    v = next(item for item in verdicts if item.id == rec_in.id)
    return RecordVerdictOut(
        id=v.id,
        barcode=rec_in.barcode,
        timestamp=rec_in.timestamp,
        status=v.status,
        certain_partner_id=v.certain_partner_id,
        matched_in_all_optimal=v.matched_in_all_optimal,
        possible_partners=[
            PartnerOptionOut(id=o.id, time_diff_seconds=o.time_diff_ms / 1000)
            for o in v.possible_partners
        ],
    )


def _status_counts(verdicts: tuple) -> StatusCounts:
    counter = Counter(v.status for v in verdicts)
    return StatusCounts(
        certain=counter.get("certain", 0),
        review=counter.get("review", 0),
        unmatched=counter.get("unmatched", 0),
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/match",
    response_model=MatchResponse,
    summary="配对采样登记与仪器检测记录",
    description=(
        "接收两组各不超过 12 条记录，返回确定配对（所有同等最优方案都成立）、"
        "每条记录的状态（certain / review / unmatched）及可行配对对象。"
    ),
)
def match(
    request: MatchRequest = Body(openapi_examples=DEMO_EXAMPLES),
) -> MatchResponse:
    sampling = [Record(r.id, r.barcode, _to_ms(r.timestamp)) for r in request.sampling_records]
    detection = [Record(r.id, r.barcode, _to_ms(r.timestamp)) for r in request.detection_records]

    analysis = analyze(sampling, detection)

    return MatchResponse(
        summary=SummaryOut(
            sampling_count=len(sampling),
            detection_count=len(detection),
            candidate_pair_count=analysis.candidate_pair_count,
            max_pairs=analysis.max_pairs,
            min_total_time_diff_seconds=analysis.min_total_time_diff_ms / 1000,
            certain_pair_count=len(analysis.certain_pairs),
            sampling_status=_status_counts(analysis.sampling),
            detection_status=_status_counts(analysis.detection),
        ),
        certain_pairs=[
            CertainPairOut(
                sampling_id=p.sampling_id,
                detection_id=p.detection_id,
                barcode=p.barcode,
                time_diff_seconds=p.time_diff_ms / 1000,
            )
            for p in analysis.certain_pairs
        ],
        sampling_records=[
            _verdict_out(r, analysis, is_sampling=True) for r in request.sampling_records
        ],
        detection_records=[
            _verdict_out(r, analysis, is_sampling=False) for r in request.detection_records
        ],
    )


def _sample_out(
    verdict,
    needle_map: dict[str, SampleNeedle],
) -> SampleReleaseOut:
    """把核心层的样本结论组装为响应模型。"""
    return SampleReleaseOut(
        sample_id=verdict.sample_id,
        status=verdict.status,
        reason_code=verdict.reason_code,
        reason_message=verdict.reason_message,
        conflict_kind=verdict.conflict_kind,
        released_injection_id=verdict.released_injection_id,
        released_reading=verdict.released_reading,
        chain_tail_ids=list(verdict.chain_tail_ids),
        needle_count=len(verdict.needles),
        needles=[
            NeedleDetailOut(
                injection_id=nv.injection_id,
                sample_id=needle_map[nv.injection_id].sample_id,
                reading=needle_map[nv.injection_id].reading,
                order=nv.order + 1,
                retest_of=needle_map[nv.injection_id].retest_of,
                window_status=nv.window_status,
                window_reason=nv.window_reason,
                prev_qc_id=nv.prev_qc_id,
                next_qc_id=nv.next_qc_id,
                is_chain_tail=nv.is_chain_tail,
            )
            for nv in verdict.needles
        ],
    )


@app.post(
    "/quality-release",
    response_model=QualityReleaseResponse,
    summary="质控放行：从上机序列选出可入分析库的样本读数",
    description=(
        "接收最多 48 针按注入顺序排列的质控针与样本针，以及质控读数的合格闭区间。\n\n"
        "- 样本针仅在**前后最近的两支质控针都合格（读数落入闭区间，含边界）**时窗口可用；\n"
        "- 缺任一侧质控 -> 待补（pending）；两侧齐全而任一侧失控 -> 隔离（quarantined）；\n"
        "- 复测须通过 retest_of 引用同样本更早的一针；同一样本只放行**唯一复测链的末针**；\n"
        "- 复测分叉（一针被多针引用）或多条无关联链 -> conflict，全部扣留；\n"
        "- 末针待补或隔离时**不退回**链上任何更早读数。"
    ),
)
def quality_release(
    request: QualityReleaseRequest = Body(openapi_examples=QUALITY_DEMO_EXAMPLES),
) -> QualityReleaseResponse:
    rng = request.acceptance_range
    qc_needles: list[QCNeedle] = []
    sample_needles: list[SampleNeedle] = []
    for order, inj in enumerate(request.injections):
        if inj.type == "qc":
            qc_needles.append(
                QCNeedle(
                    injection_id=inj.injection_id,
                    order=order,
                    reading=inj.reading,
                    in_control=rng.lower <= inj.reading <= rng.upper,
                )
            )
        else:
            sample_needles.append(
                SampleNeedle(
                    injection_id=inj.injection_id,
                    order=order,
                    sample_id=inj.sample_id,
                    reading=inj.reading,
                    retest_of=inj.retest_of,
                )
            )

    analysis: QualityAnalysis = analyze_quality(qc_needles, sample_needles)
    needle_map = {s.injection_id: s for s in sample_needles}

    counter = Counter(v.status for v in analysis.verdicts)
    summary = QualitySummaryOut(
        total_injections=len(request.injections),
        qc_count=len(qc_needles),
        qc_in_control_count=analysis.qc_in_control_count,
        qc_out_of_control_count=analysis.qc_out_of_control_count,
        sample_injection_count=len(sample_needles),
        sample_count=len(analysis.verdicts),
        released_count=counter.get("released", 0),
        pending_count=counter.get("pending", 0),
        quarantined_count=counter.get("quarantined", 0),
        conflict_count=counter.get("conflict", 0),
    )

    return QualityReleaseResponse(
        summary=summary,
        acceptance_range=AcceptanceRange(lower=rng.lower, upper=rng.upper),
        samples=[_sample_out(v, needle_map) for v in analysis.verdicts],
    )
