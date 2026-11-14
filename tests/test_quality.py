"""质控放行核心算法的单元测试。"""

from app.quality import (
    QCNeedle,
    SampleNeedle,
    analyze_quality,
)


def qc(iid: str, order: int, reading: float = 10.0, in_control: bool = True) -> QCNeedle:
    return QCNeedle(injection_id=iid, order=order, reading=reading, in_control=in_control)


def smp(
    iid: str,
    order: int,
    sample: str = "S1",
    reading: float = 5.0,
    retest_of: str | None = None,
) -> SampleNeedle:
    return SampleNeedle(
        injection_id=iid, order=order, sample_id=sample, reading=reading, retest_of=retest_of
    )


def by_sample(result):
    return {v.sample_id: v for v in result.verdicts}


def window_map(verdict):
    return {nv.injection_id: nv.window_status for nv in verdict.needles}


def test_single_sample_between_two_in_control_qc_is_released():
    """两支在控质控夹住的首针（即末针）直接放行。"""
    result = analyze_quality(
        [qc("Q1", 0), qc("Q2", 2)],
        [smp("I1", 1, reading=5.5)],
    )
    v = result.verdicts[0]
    assert v.status == "released"
    assert v.released_injection_id == "I1"
    assert v.released_reading == 5.5
    assert v.chain_tail_ids == ("I1",)
    nv = v.needles[0]
    assert nv.window_status == "released"
    assert nv.prev_qc_id == "Q1" and nv.next_qc_id == "Q2"
    assert nv.is_chain_tail is True


def test_nearest_qc_skips_over_other_needles():
    """前后最近质控之间隔着其他针，不影响最近邻选择。"""
    result = analyze_quality(
        [qc("Q1", 0), qc("Q2", 4)],
        [smp("I1", 1), smp("I2", 2), smp("I3", 3)],
    )
    v = by_sample(result)["S1"]
    assert all(nv.prev_qc_id == "Q1" and nv.next_qc_id == "Q2" for nv in v.needles)


def test_missing_qc_on_each_side_is_pending():
    """序列首/尾缺侧质控：分别给出 missing_prev/missing_next，双侧缺失 both。"""
    result = analyze_quality(
        [qc("Q1", 1)],
        [
            smp("I0", 0),  # 只有后侧
            smp("I2", 2),  # 只有前侧
        ],
    )
    v = result.verdicts[0]
    reasons = {nv.injection_id: nv.window_reason for nv in v.needles}
    statuses = window_map(v)
    assert statuses == {"I0": "pending", "I2": "pending"}
    assert reasons["I0"] == "missing_prev_qc"
    assert reasons["I2"] == "missing_next_qc"

    only = analyze_quality([], [smp("I9", 0)])
    nv = only.verdicts[0].needles[0]
    assert nv.window_status == "pending"
    assert nv.window_reason == "missing_both_qc"


def test_out_of_control_qc_quarantines_window():
    """两侧齐全但任一失控 -> 隔离；只一侧失控也隔离。"""
    result = analyze_quality(
        [qc("Q1", 0, in_control=False), qc("Q2", 2, in_control=True)],
        [smp("I1", 1)],
    )
    v = result.verdicts[0]
    assert v.status == "quarantined"
    assert v.reason_code == "tail_quarantined"
    assert v.needles[0].window_status == "quarantined"
    assert v.needles[0].window_reason == "qc_out_of_control"
    assert v.released_injection_id is None

    both_bad = analyze_quality(
        [qc("Q1", 0, in_control=False), qc("Q2", 2, in_control=False)],
        [smp("I1", 1)],
    )
    assert both_bad.verdicts[0].status == "quarantined"


def test_retest_chain_releases_only_last_needle():
    """唯一复测链只放行末针读数。"""
    result = analyze_quality(
        [qc("Q1", 0), qc("Q2", 4)],
        [
            smp("I1", 1, reading=5.0),
            smp("I2", 2, reading=5.2, retest_of="I1"),
            smp("I3", 3, reading=5.4, retest_of="I2"),
        ],
    )
    v = result.verdicts[0]
    assert v.status == "released"
    assert v.released_injection_id == "I3"
    assert v.released_reading == 5.4
    assert v.chain_tail_ids == ("I3",)
    tails = {nv.injection_id: nv.is_chain_tail for nv in v.needles}
    assert tails == {"I1": False, "I2": False, "I3": True}


def test_pending_tail_does_not_fall_back_to_earlier_reading():
    """末针缺后侧质控待补，即使链上前一针窗口合格也不退回。"""
    result = analyze_quality(
        [qc("Q1", 0), qc("Q2", 3)],
        [
            smp("I1", 1, reading=5.0),                       # Q1..Q2 夹住，窗口合格
            smp("I2", 4, reading=5.2, retest_of="I1"),      # Q2 之后，缺后侧 -> 待补
        ],
    )
    v = result.verdicts[0]
    assert v.status == "pending"
    assert v.reason_code == "tail_pending"
    assert v.released_injection_id is None
    assert v.released_reading is None
    statuses = window_map(v)
    assert statuses["I1"] == "released"
    assert statuses["I2"] == "pending"


def test_quarantined_tail_does_not_fall_back():
    """末针隔离时不退回旧读数，整链样本隔离。"""
    result = analyze_quality(
        [qc("Q1", 0, in_control=True), qc("Q2", 3, in_control=False)],
        [
            smp("I1", 1, reading=5.0),
            smp("I2", 2, reading=5.2, retest_of="I1"),
        ],
    )
    v = result.verdicts[0]
    assert v.status == "quarantined"
    assert v.released_reading is None


def test_retest_fork_is_conflict():
    """一针被两支后续针引用 -> 复测分叉冲突（同根单链树，分叉本身即异常）。"""
    result = analyze_quality(
        [qc("Q1", 0), qc("Q2", 5)],
        [
            smp("I1", 1),
            smp("I2", 2, retest_of="I1"),
            smp("I3", 3, retest_of="I1"),  # 分叉：I1 被引用两次
            smp("I4", 4, retest_of="I2"),
        ],
    )
    v = result.verdicts[0]
    assert v.status == "conflict"
    assert v.conflict_kind == "retest_fork"
    assert v.reason_code == "chain_conflict"
    assert set(v.chain_tail_ids) == {"I3", "I4"}
    assert v.released_reading is None


def test_multiple_unrelated_chains_is_conflict():
    """同一样本两支互不引用的首针 -> 多条无关联链冲突。"""
    result = analyze_quality(
        [qc("Q1", 0), qc("Q2", 3)],
        [smp("I1", 1), smp("I2", 2)],
    )
    v = result.verdicts[0]
    assert v.status == "conflict"
    assert v.conflict_kind == "multiple_unrelated_chains"
    assert set(v.chain_tail_ids) == {"I1", "I2"}


def test_fork_and_multiple_chains_conflict_kind_both():
    """两个起始根且其中发生分叉 -> conflict_kind=both。"""
    result = analyze_quality(
        [qc("Q1", 0), qc("Q2", 6)],
        [
            smp("I1", 1),                        # 根 1
            smp("I2", 2, retest_of="I1"),
            smp("I3", 3, retest_of="I1"),       # 分叉
            smp("I4", 4),                        # 根 2（无关联链）
            smp("I5", 5, retest_of="I4"),
        ],
    )
    v = result.verdicts[0]
    assert v.status == "conflict"
    assert v.conflict_kind == "both"
    assert set(v.chain_tail_ids) == {"I3", "I2", "I5"}


def test_conflict_takes_priority_over_window_failure():
    """链冲突优先于窗口状态：即使末针窗口不合格也先报冲突。"""
    result = analyze_quality(
        [qc("Q1", 0, in_control=False)],
        [
            smp("I1", 1),
            smp("I2", 2),  # 无关联链 + 缺后侧 + 前侧失控
        ],
    )
    v = result.verdicts[0]
    assert v.status == "conflict"


def test_samples_are_independent():
    """多样本各自判定，互不干扰且保留首次出现顺序。"""
    result = analyze_quality(
        [qc("Q1", 0), qc("Q2", 5)],
        [
            smp("A1", 1, sample="A"),
            smp("B1", 2, sample="B"),
            smp("A2", 3, sample="A", retest_of="A1"),
            smp("B2", 4, sample="B"),  # B 的第二条无关联链
        ],
    )
    assert [v.sample_id for v in result.verdicts] == ["A", "B"]
    verdicts = by_sample(result)
    assert verdicts["A"].status == "released"
    assert verdicts["A"].released_injection_id == "A2"
    assert verdicts["B"].status == "conflict"


def test_qc_in_control_counts():
    """质控在控/失控计数正确。"""
    result = analyze_quality(
        [
            qc("Q1", 0, in_control=True),
            qc("Q2", 2, in_control=False),
            qc("Q3", 4, in_control=True),
        ],
        [smp("I1", 1), smp("I2", 3)],
    )
    assert result.qc_in_control_count == 2
    assert result.qc_out_of_control_count == 1


def test_qc_boundary_needles_share_windows():
    """紧邻质控针两侧的样本针分别取其为最近质控。"""
    result = analyze_quality(
        [qc("Q1", 1), qc("Q2", 3)],
        [smp("L", 0), smp("R", 2), smp("T", 4)],
    )
    v = result.verdicts[0]
    qc_refs = {
        nv.injection_id: (nv.prev_qc_id, nv.next_qc_id) for nv in v.needles
    }
    assert qc_refs["L"] == (None, "Q1")
    assert qc_refs["R"] == ("Q1", "Q2")
    assert qc_refs["T"] == ("Q2", None)


def test_empty_sample_set():
    """只有质控针时没有样本结论。"""
    result = analyze_quality([qc("Q1", 0), qc("Q2", 1)], [])
    assert result.verdicts == ()
    assert result.qc_in_control_count == 2
