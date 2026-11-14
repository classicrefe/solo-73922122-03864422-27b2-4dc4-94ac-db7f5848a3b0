"""/quality-release 挂载到 /docs 的演示请求。

三组场景：
  * normal_release      —— 常规放行：在控质控夹住的复测链末针可入库，序列边缘待补；
  * qc_failure          —— 质控失控：末针隔离，不退回链上更早读数；
  * chain_conflict      —— 复测分叉 + 多条无关联链：即使窗口全部合格也判冲突扣留。
"""

QUALITY_DEMO_EXAMPLES = {
    "normal_release": {
        "summary": "常规放行：复测链末针在控可入库，末针缺侧质控则待补",
        "description": (
            "合格闭区间 [9.0, 11.0]。QC-01/QC-02/QC-03 全部在控："
            "SMP-A、SMP-B 各自的唯一复测链末针（I-02、I-04）窗口合格，放行末针读数；"
            "SMP-C 只有一针且位于序列末端，缺后侧质控，末针待补，不能入库。"
        ),
        "value": {
            "acceptance_range": {"lower": 9.0, "upper": 11.0},
            "injections": [
                {"type": "qc", "injection_id": "QC-01", "reading": 10.0},
                {
                    "type": "sample",
                    "injection_id": "I-01",
                    "sample_id": "SMP-A",
                    "reading": 5.1,
                },
                {
                    "type": "sample",
                    "injection_id": "I-02",
                    "sample_id": "SMP-A",
                    "reading": 5.3,
                    "retest_of": "I-01",
                },
                {"type": "qc", "injection_id": "QC-02", "reading": 10.5},
                {
                    "type": "sample",
                    "injection_id": "I-03",
                    "sample_id": "SMP-B",
                    "reading": 7.0,
                },
                {
                    "type": "sample",
                    "injection_id": "I-04",
                    "sample_id": "SMP-B",
                    "reading": 7.2,
                    "retest_of": "I-03",
                },
                {"type": "qc", "injection_id": "QC-03", "reading": 10.8},
                {
                    "type": "sample",
                    "injection_id": "I-05",
                    "sample_id": "SMP-C",
                    "reading": 3.3,
                },
            ],
        },
    },
    "qc_failure": {
        "summary": "质控失控：末针隔离，整链不退回旧读数",
        "description": (
            "QC-02 读数 12.0 超出 [9.0, 11.0] 失控。SMP-X 的三针全部落在以 QC-02 为"
            "最近质控的窗口内，唯一复测链末针 I-03 被隔离；即使 I-01 的读数更早存在，"
            "末针不能入库时也不退回任何旧读数，样本整体 quarantined。"
        ),
        "value": {
            "acceptance_range": {"lower": 9.0, "upper": 11.0},
            "injections": [
                {"type": "qc", "injection_id": "QC-01", "reading": 10.0},
                {
                    "type": "sample",
                    "injection_id": "I-01",
                    "sample_id": "SMP-X",
                    "reading": 4.0,
                },
                {
                    "type": "sample",
                    "injection_id": "I-02",
                    "sample_id": "SMP-X",
                    "reading": 4.1,
                    "retest_of": "I-01",
                },
                {"type": "qc", "injection_id": "QC-02", "reading": 12.0},
                {
                    "type": "sample",
                    "injection_id": "I-03",
                    "sample_id": "SMP-X",
                    "reading": 4.2,
                    "retest_of": "I-02",
                },
                {"type": "qc", "injection_id": "QC-03", "reading": 10.2},
            ],
        },
    },
    "chain_conflict": {
        "summary": "复测链冲突：分叉与无关联链并存，窗口合格也扣留",
        "description": (
            "QC-01、QC-02 均在控，所有样本针窗口都合格，但：SMP-K 中 I-01 同时被 "
            "I-02、I-03 引用（复测分叉，复测树出现两个末针 I-03/I-04）；SMP-L 的 I-10、"
            "I-11 互不引用，构成两条无关联链。两个样本均判 conflict 全部扣留，"
            "接口不替质控员任选一条链放行。"
        ),
        "value": {
            "acceptance_range": {"lower": 9.0, "upper": 11.0},
            "injections": [
                {"type": "qc", "injection_id": "QC-01", "reading": 10.0},
                {
                    "type": "sample",
                    "injection_id": "I-01",
                    "sample_id": "SMP-K",
                    "reading": 1.0,
                },
                {
                    "type": "sample",
                    "injection_id": "I-02",
                    "sample_id": "SMP-K",
                    "reading": 1.1,
                    "retest_of": "I-01",
                },
                {
                    "type": "sample",
                    "injection_id": "I-03",
                    "sample_id": "SMP-K",
                    "reading": 1.2,
                    "retest_of": "I-01",
                },
                {
                    "type": "sample",
                    "injection_id": "I-04",
                    "sample_id": "SMP-K",
                    "reading": 1.3,
                    "retest_of": "I-02",
                },
                {
                    "type": "sample",
                    "injection_id": "I-10",
                    "sample_id": "SMP-L",
                    "reading": 2.0,
                },
                {
                    "type": "sample",
                    "injection_id": "I-11",
                    "sample_id": "SMP-L",
                    "reading": 2.1,
                },
                {"type": "qc", "injection_id": "QC-02", "reading": 10.3},
            ],
        },
    },
}
