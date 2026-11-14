"""/quality-release 接口的集成测试。"""

import copy

from fastapi.testclient import TestClient

from app.main import app
from app.quality_examples import QUALITY_DEMO_EXAMPLES

client = TestClient(app)


def test_quality_endpoint_in_openapi_with_three_examples():
    spec = client.get("/openapi.json").json()
    assert "/quality-release" in spec["paths"]
    examples = spec["paths"]["/quality-release"]["post"]["requestBody"]["content"][
        "application/json"
    ]["examples"]
    assert {"normal_release", "qc_failure", "chain_conflict"} <= set(examples)


def test_demo_normal_release():
    payload = copy.deepcopy(QUALITY_DEMO_EXAMPLES["normal_release"]["value"])
    resp = client.post("/quality-release", json=payload)
    assert resp.status_code == 200
    body = resp.json()

    assert body["acceptance_range"] == {"lower": 9.0, "upper": 11.0}
    summary = body["summary"]
    assert summary["total_injections"] == 8
    assert summary["qc_count"] == 3
    assert summary["qc_in_control_count"] == 3
    assert summary["qc_out_of_control_count"] == 0
    assert summary["sample_injection_count"] == 5
    assert summary["sample_count"] == 3
    assert summary["released_count"] == 2
    assert summary["pending_count"] == 1

    samples = {s["sample_id"]: s for s in body["samples"]}
    a = samples["SMP-A"]
    assert a["status"] == "released"
    assert a["released_injection_id"] == "I-02"
    assert a["released_reading"] == 5.3
    assert a["chain_tail_ids"] == ["I-02"]
    b = samples["SMP-B"]
    assert b["status"] == "released"
    assert b["released_injection_id"] == "I-04"
    c = samples["SMP-C"]
    assert c["status"] == "pending"
    assert c["reason_code"] == "tail_pending"
    assert c["released_injection_id"] is None
    # 末端样本针缺后侧质控
    c_needle = c["needles"][0]
    assert c_needle["window_status"] == "pending"
    assert c_needle["window_reason"] == "missing_next_qc"
    assert c_needle["prev_qc_id"] == "QC-03"
    assert c_needle["next_qc_id"] is None
    assert c_needle["order"] == 8  # 1 基位序


def test_demo_qc_failure_quarantine_no_fallback():
    payload = copy.deepcopy(QUALITY_DEMO_EXAMPLES["qc_failure"]["value"])
    resp = client.post("/quality-release", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["qc_out_of_control_count"] == 1
    x = body["samples"][0]
    assert x["sample_id"] == "SMP-X"
    assert x["status"] == "quarantined"
    assert x["reason_code"] == "tail_quarantined"
    assert x["released_injection_id"] is None
    assert x["released_reading"] is None
    assert x["chain_tail_ids"] == ["I-03"]
    # 末针 I-03 的前后质控为 QC-02（失控）/ QC-03
    tail = next(n for n in x["needles"] if n["injection_id"] == "I-03")
    assert tail["window_status"] == "quarantined"
    assert tail["prev_qc_id"] == "QC-02"
    assert tail["next_qc_id"] == "QC-03"


def test_demo_chain_conflict():
    payload = copy.deepcopy(QUALITY_DEMO_EXAMPLES["chain_conflict"]["value"])
    resp = client.post("/quality-release", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["conflict_count"] == 2
    samples = {s["sample_id"]: s for s in body["samples"]}

    k = samples["SMP-K"]
    assert k["status"] == "conflict"
    assert k["conflict_kind"] == "retest_fork"  # 单根复测树中 I-01 被分叉引用
    assert set(k["chain_tail_ids"]) == {"I-03", "I-04"}
    assert k["released_reading"] is None

    l = samples["SMP-L"]
    assert l["status"] == "conflict"
    assert l["conflict_kind"] == "multiple_unrelated_chains"
    assert set(l["chain_tail_ids"]) == {"I-10", "I-11"}


def test_response_is_deterministic():
    payload = copy.deepcopy(QUALITY_DEMO_EXAMPLES["chain_conflict"]["value"])
    first = client.post("/quality-release", json=payload).json()
    second = client.post("/quality-release", json=payload).json()
    assert first == second


def test_acceptance_range_boundary_is_inclusive():
    """读数恰好等于闭区间边界判在控。"""
    payload = {
        "acceptance_range": {"lower": 9.0, "upper": 11.0},
        "injections": [
            {"type": "qc", "injection_id": "Q1", "reading": 9.0},
            {"type": "sample", "injection_id": "I1", "sample_id": "S", "reading": 1.0},
            {"type": "qc", "injection_id": "Q2", "reading": 11.0},
        ],
    }
    body = client.post("/quality-release", json=payload).json()
    assert body["summary"]["qc_in_control_count"] == 2
    assert body["samples"][0]["status"] == "released"


def test_retest_chain_tail_released_via_api():
    payload = {
        "acceptance_range": {"lower": 0.0, "upper": 100.0},
        "injections": [
            {"type": "qc", "injection_id": "Q1", "reading": 50.0},
            {"type": "sample", "injection_id": "I1", "sample_id": "S", "reading": 1.0},
            {
                "type": "sample",
                "injection_id": "I2",
                "sample_id": "S",
                "reading": 1.5,
                "retest_of": "I1",
            },
            {"type": "qc", "injection_id": "Q2", "reading": 50.0},
        ],
    }
    body = client.post("/quality-release", json=payload).json()
    s = body["samples"][0]
    assert s["status"] == "released"
    assert s["released_injection_id"] == "I2"
    assert s["released_reading"] == 1.5
    assert s["needles"][0]["retest_of"] is None
    assert s["needles"][1]["retest_of"] == "I1"


# ---------- 422 校验 ----------

def test_rejects_more_than_48_injections():
    injections = [{"type": "qc", "injection_id": f"Q{i}", "reading": 1.0} for i in range(49)]
    resp = client.post(
        "/quality-release",
        json={"acceptance_range": {"lower": 0.0, "upper": 2.0}, "injections": injections},
    )
    assert resp.status_code == 422


def test_rejects_duplicate_injection_ids():
    payload = {
        "acceptance_range": {"lower": 0.0, "upper": 2.0},
        "injections": [
            {"type": "qc", "injection_id": "X1", "reading": 1.0},
            {"type": "qc", "injection_id": "X1", "reading": 1.0},
        ],
    }
    assert client.post("/quality-release", json=payload).status_code == 422


def test_rejects_retest_of_unknown_injection():
    payload = {
        "acceptance_range": {"lower": 0.0, "upper": 2.0},
        "injections": [
            {"type": "qc", "injection_id": "Q1", "reading": 1.0},
            {
                "type": "sample",
                "injection_id": "I1",
                "sample_id": "S",
                "reading": 1.0,
                "retest_of": "GHOST",
            },
        ],
    }
    assert client.post("/quality-release", json=payload).status_code == 422


def test_rejects_retest_pointing_forward():
    payload = {
        "acceptance_range": {"lower": 0.0, "upper": 2.0},
        "injections": [
            {
                "type": "sample",
                "injection_id": "I1",
                "sample_id": "S",
                "reading": 1.0,
                "retest_of": "I2",
            },
            {"type": "sample", "injection_id": "I2", "sample_id": "S", "reading": 1.1},
        ],
    }
    assert client.post("/quality-release", json=payload).status_code == 422


def test_rejects_retest_across_samples():
    payload = {
        "acceptance_range": {"lower": 0.0, "upper": 2.0},
        "injections": [
            {"type": "sample", "injection_id": "I1", "sample_id": "S1", "reading": 1.0},
            {
                "type": "sample",
                "injection_id": "I2",
                "sample_id": "S2",
                "reading": 1.1,
                "retest_of": "I1",
            },
        ],
    }
    assert client.post("/quality-release", json=payload).status_code == 422


def test_rejects_retest_pointing_at_qc():
    payload = {
        "acceptance_range": {"lower": 0.0, "upper": 2.0},
        "injections": [
            {"type": "qc", "injection_id": "Q1", "reading": 1.0},
            {
                "type": "sample",
                "injection_id": "I1",
                "sample_id": "S",
                "reading": 1.0,
                "retest_of": "Q1",
            },
        ],
    }
    assert client.post("/quality-release", json=payload).status_code == 422


def test_rejects_inverted_acceptance_range():
    payload = {
        "acceptance_range": {"lower": 10.0, "upper": 1.0},
        "injections": [{"type": "qc", "injection_id": "Q1", "reading": 5.0}],
    }
    assert client.post("/quality-release", json=payload).status_code == 422


def test_rejects_non_finite_reading():
    # 1e999 解析为 Infinity，规避客户端 JSON 编码器直接拒绝 inf 的问题
    raw = (
        '{"acceptance_range": {"lower": 0.0, "upper": 2.0},'
        ' "injections": [{"type": "qc", "injection_id": "Q1", "reading": 1e999}]}'
    )
    resp = client.post(
        "/quality-release",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_rejects_unknown_fields_and_bad_discriminator():
    base = {
        "acceptance_range": {"lower": 0.0, "upper": 2.0},
        "injections": [
            {"type": "qc", "injection_id": "Q1", "reading": 1.0, "operator": "x"},
        ],
    }
    assert client.post("/quality-release", json=base).status_code == 422
    base["injections"] = [{"type": "calibrator", "injection_id": "C1", "reading": 1.0}]
    assert client.post("/quality-release", json=base).status_code == 422


def test_empty_injection_list_rejected():
    payload = {
        "acceptance_range": {"lower": 0.0, "upper": 2.0},
        "injections": [],
    }
    assert client.post("/quality-release", json=payload).status_code == 422


def test_qc_only_sequence_has_no_samples():
    payload = {
        "acceptance_range": {"lower": 0.0, "upper": 2.0},
        "injections": [
            {"type": "qc", "injection_id": "Q1", "reading": 1.0},
            {"type": "qc", "injection_id": "Q2", "reading": 3.0},
        ],
    }
    body = client.post("/quality-release", json=payload).json()
    assert body["samples"] == []
    assert body["summary"] == {
        "total_injections": 2,
        "qc_count": 2,
        "qc_in_control_count": 1,
        "qc_out_of_control_count": 1,
        "sample_injection_count": 0,
        "sample_count": 0,
        "released_count": 0,
        "pending_count": 0,
        "quarantined_count": 0,
        "conflict_count": 0,
    }
