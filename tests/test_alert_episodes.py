"""设备告警周期查询 GET /devices/{device_id}/alert-episodes 测试。

覆盖：
- 按转换流水自增顺序（id/created_at）配对 entered/cleared，起止时刻取 created_at；
- 迟到事件不按 event_time 重排（event_time 更早的迟到 entered 仍与流水其后的 cleared 配对）；
- 半开区间 [start, end) 相交裁剪：仅端点相触不算、左右裁剪、完整包含、总秒数为明细之和；
- 尚未解除（开放）周期只计至 min(end, 查询时刻)；
- 422（缺参、end<=start）、404（设备不存在、无告警规则）、无相交周期返回空与零；
- 经 /ingest 真实写入产生的流水同样可用；接口只读。
"""

import pytest

from app import db
from app import main as main_module


def make_device(client, device_id="dev-1"):
    r = client.put(f"/devices/{device_id}/config", json={})
    assert r.status_code == 200, r.text


def set_rule(client, device_id="dev-1", trigger=80.0, recovery=70.0):
    r = client.put(
        f"/devices/{device_id}/alert-rule",
        json={"trigger_threshold": trigger, "recovery_threshold": recovery},
    )
    assert r.status_code == 200, r.text


def episodes(client, device_id, start, end):
    return client.get(
        f"/devices/{device_id}/alert-episodes",
        params={"start": start, "end": end},
    )


def insert_transition(device_id, event_id, kind, created_at, event_time=None, value=90.0):
    """直接写入一条转换流水，created_at 可控，用于确定性的秒数断言。"""
    with db.session() as conn:
        conn.execute(
            "INSERT INTO device_alert_transitions"
            " (device_id, event_id, event_time, value, kind, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (device_id, event_id, event_time if event_time is not None else created_at, value, kind, created_at),
        )


def setup_rule_device(client, device_id="dev-1"):
    make_device(client, device_id)
    set_rule(client, device_id)


# ---------- 404 / 参数校验 ----------

def test_unknown_device_returns_404(client):
    assert episodes(client, "ghost", "2026-09-23T00:00:00Z", "2026-09-23T01:00:00Z").status_code == 404


def test_device_without_rule_returns_404(client):
    make_device(client, "dev-1")
    assert episodes(client, "dev-1", "2026-09-23T00:00:00Z", "2026-09-23T01:00:00Z").status_code == 404


@pytest.mark.parametrize("qs", [
    "start=2026-09-23T00:00:00Z",                                  # 缺 end
    "end=2026-09-23T01:00:00Z",                                    # 缺 start
    "",                                                            # 都缺
])
def test_missing_params_return_422(client, qs):
    setup_rule_device(client)
    r = client.get(f"/devices/dev-1/alert-episodes?{qs}")
    assert r.status_code == 422


def test_end_not_after_start_returns_422(client):
    setup_rule_device(client)
    # end == start
    r = episodes(client, "dev-1", "2026-09-23T00:00:00Z", "2026-09-23T00:00:00Z")
    assert r.status_code == 422
    # end < start
    r = episodes(client, "dev-1", "2026-09-23T01:00:00Z", "2026-09-23T00:00:00Z")
    assert r.status_code == 422


def test_invalid_datetime_returns_422(client):
    setup_rule_device(client)
    r = episodes(client, "dev-1", "not-a-time", "2026-09-23T01:00:00Z")
    assert r.status_code == 422


# ---------- 配对与裁剪 ----------

def test_no_episodes_returns_empty_and_zero(client):
    setup_rule_device(client)
    r = episodes(client, "dev-1", "2026-09-23T00:00:00Z", "2026-09-23T01:00:00Z")
    assert r.status_code == 200
    body = r.json()
    assert body["episodes"] == []
    assert body["total_alert_seconds"] == 0
    assert body["start"] == "2026-09-23T00:00:00Z"
    assert body["end"] == "2026-09-23T01:00:00Z"


def test_closed_episode_fully_inside_range(client):
    setup_rule_device(client)
    insert_transition("dev-1", "e1", "entered", "2026-09-23T00:00:10Z")
    insert_transition("dev-1", "e2", "cleared", "2026-09-23T00:00:40Z")

    body = episodes(client, "dev-1", "2026-09-23T00:00:00Z", "2026-09-23T01:00:00Z").json()
    assert len(body["episodes"]) == 1
    ep = body["episodes"][0]
    assert ep["entered_at"] == "2026-09-23T00:00:10Z"
    assert ep["cleared_at"] == "2026-09-23T00:00:40Z"
    assert ep["ongoing"] is False
    assert ep["seconds_in_range"] == 30
    assert ep["range_start"] == "2026-09-23T00:00:10Z"
    assert ep["range_end"] == "2026-09-23T00:00:40Z"
    assert body["total_alert_seconds"] == 30


def test_episode_clipped_on_both_sides_and_total_is_sum(client):
    setup_rule_device(client)
    # 周期 [00:00:10, 00:00:40)
    insert_transition("dev-1", "e1", "entered", "2026-09-23T00:00:10Z")
    insert_transition("dev-1", "e2", "cleared", "2026-09-23T00:00:40Z")
    # 周期 [00:01:10, 00:01:40)
    insert_transition("dev-1", "e3", "entered", "2026-09-23T00:01:10Z")
    insert_transition("dev-1", "e4", "cleared", "2026-09-23T00:01:40Z")

    # 查询 [00:00:20, 00:01:20)：第一段裁出 [20, 40)=20s，第二段裁出 [10, 20)=10s
    body = episodes(client, "dev-1", "2026-09-23T00:00:20Z", "2026-09-23T00:01:20Z").json()
    assert [e["seconds_in_range"] for e in body["episodes"]] == [20, 10]
    assert body["episodes"][0]["range_start"] == "2026-09-23T00:00:20Z"
    assert body["episodes"][0]["range_end"] == "2026-09-23T00:00:40Z"
    assert body["episodes"][1]["range_start"] == "2026-09-23T00:01:10Z"
    assert body["episodes"][1]["range_end"] == "2026-09-23T00:01:20Z"
    assert body["total_alert_seconds"] == 30


def test_episodes_outside_range_are_excluded(client):
    setup_rule_device(client)
    # 完全早于查询区间
    insert_transition("dev-1", "e1", "entered", "2026-09-23T00:00:00Z")
    insert_transition("dev-1", "e2", "cleared", "2026-09-23T00:00:10Z")
    # 完全晚于查询区间
    insert_transition("dev-1", "e3", "entered", "2026-09-23T00:02:00Z")
    insert_transition("dev-1", "e4", "cleared", "2026-09-23T00:02:10Z")

    body = episodes(client, "dev-1", "2026-09-23T00:01:00Z", "2026-09-23T00:01:30Z").json()
    assert body["episodes"] == []
    assert body["total_alert_seconds"] == 0


def test_half_open_boundary_touch_is_not_an_intersection(client):
    setup_rule_device(client)
    # 周期恰在 start 处解除：[..., 00:00:30) ∩ [00:00:30, ...) 为空
    insert_transition("dev-1", "e1", "entered", "2026-09-23T00:00:00Z")
    insert_transition("dev-1", "e2", "cleared", "2026-09-23T00:00:30Z")
    body = episodes(client, "dev-1", "2026-09-23T00:00:30Z", "2026-09-23T00:01:00Z").json()
    assert body["episodes"] == []

    # 周期恰在 end 处进入：[00:01:00, ...) ∩ [..., 00:01:00) 为空
    insert_transition("dev-1", "e3", "entered", "2026-09-23T00:01:00Z")
    insert_transition("dev-1", "e4", "cleared", "2026-09-23T00:01:30Z")
    body = episodes(client, "dev-1", "2026-09-23T00:00:30Z", "2026-09-23T00:01:00Z").json()
    assert body["episodes"] == []


def test_open_ended_episode_capped_at_end_when_end_before_now(client, monkeypatch):
    setup_rule_device(client)
    # 进入于 00:00:10，流水末尾停留 entered（尚未解除）
    insert_transition("dev-1", "e1", "entered", "2026-09-23T00:00:10Z")
    # 查询时刻远晚于 end
    monkeypatch.setattr(main_module.time, "time", lambda: parse_iso("2026-09-24T00:00:00Z"))

    body = episodes(client, "dev-1", "2026-09-23T00:00:00Z", "2026-09-23T00:01:00Z").json()
    assert len(body["episodes"]) == 1
    ep = body["episodes"][0]
    assert ep["ongoing"] is True
    assert ep["cleared_at"] is None
    assert ep["seconds_in_range"] == 50  # [00:00:10, 00:01:00)
    assert ep["range_end"] == "2026-09-23T00:01:00Z"
    assert body["total_alert_seconds"] == 50


def test_open_ended_episode_capped_at_query_time_when_now_before_end(client, monkeypatch):
    setup_rule_device(client)
    insert_transition("dev-1", "e1", "entered", "2026-09-23T00:00:00Z")
    # 查询时刻 00:00:30，早于 end 00:01:00：只计 30s
    monkeypatch.setattr(main_module.time, "time", lambda: parse_iso("2026-09-23T00:00:30Z"))

    body = episodes(client, "dev-1", "2026-09-23T00:00:00Z", "2026-09-23T00:01:00Z").json()
    ep = body["episodes"][0]
    assert ep["ongoing"] is True
    assert ep["seconds_in_range"] == 30
    assert ep["range_start"] == "2026-09-23T00:00:00Z"
    assert ep["range_end"] == "2026-09-23T00:00:30Z"
    assert body["total_alert_seconds"] == 30


def test_open_episode_entered_after_query_time_is_not_counted(client, monkeypatch):
    setup_rule_device(client)
    insert_transition("dev-1", "e1", "entered", "2026-09-23T00:00:40Z")
    # 查询时刻 00:00:30：进入时刻在未来不可能（created_at 是写入时刻），
    # 这里模拟时钟回拨/裁剪上界早于进入的情形，交集为空
    monkeypatch.setattr(main_module.time, "time", lambda: parse_iso("2026-09-23T00:00:30Z"))
    body = episodes(client, "dev-1", "2026-09-23T00:00:00Z", "2026-09-23T00:01:00Z").json()
    assert body["episodes"] == []
    assert body["total_alert_seconds"] == 0


def test_late_entered_is_paired_in_log_order_not_event_time(client):
    """迟到事件不按 event_time 重排：配对只看流水 id（created_at）顺序。"""
    setup_rule_device(client)
    # 先：正常 entered（created 00:00:10），再 cleared（00:00:20）——一个完整周期
    insert_transition(
        "dev-1", "normal-enter", "entered",
        created_at="2026-09-23T00:00:10Z", event_time="2026-09-23T00:00:10Z",
    )
    insert_transition(
        "dev-1", "normal-clear", "cleared",
        created_at="2026-09-23T00:00:20Z", event_time="2026-09-23T00:00:20Z",
    )
    # 后到：迟到 entered——event_time 早到 00:00:05，但 created_at 在 00:00:30；
    # 它不与之前的 cleared 重排，而是开启一个新的开放周期
    insert_transition(
        "dev-1", "late-enter", "entered",
        created_at="2026-09-23T00:00:30Z", event_time="2026-09-23T00:00:05Z",
    )

    body = episodes(client, "dev-1", "2026-09-23T00:00:00Z", "2026-09-23T00:01:00Z").json()
    assert len(body["episodes"]) == 2
    assert body["episodes"][0]["entered_at"] == "2026-09-23T00:00:10Z"
    assert body["episodes"][0]["cleared_at"] == "2026-09-23T00:00:20Z"
    assert body["episodes"][0]["ongoing"] is False
    assert body["episodes"][1]["entered_at"] == "2026-09-23T00:00:30Z"
    assert body["episodes"][1]["cleared_at"] is None
    assert body["episodes"][1]["ongoing"] is True


def test_episodes_ordered_by_entry_and_isolated_per_device(client):
    setup_rule_device(client, "dev-1")
    setup_rule_device(client, "dev-2")
    insert_transition("dev-1", "a", "entered", "2026-09-23T00:00:30Z")
    insert_transition("dev-1", "b", "cleared", "2026-09-23T00:00:50Z")
    insert_transition("dev-1", "c", "entered", "2026-09-23T00:00:10Z")
    insert_transition("dev-1", "d", "cleared", "2026-09-23T00:00:20Z")
    insert_transition("dev-2", "x", "entered", "2026-09-23T00:00:10Z")
    insert_transition("dev-2", "y", "cleared", "2026-09-23T00:02:10Z")

    body1 = episodes(client, "dev-1", "2026-09-23T00:00:00Z", "2026-09-23T00:01:00Z").json()
    # 流水自增顺序即 created_at 顺序，结果按进入时刻升序
    assert [e["entered_at"] for e in body1["episodes"]] == [
        "2026-09-23T00:00:10Z",
        "2026-09-23T00:00:30Z",
    ]
    # dev-2 的周期不串入 dev-1
    body2 = episodes(client, "dev-2", "2026-09-23T00:00:00Z", "2026-09-23T00:01:00Z").json()
    assert len(body2["episodes"]) == 1
    assert body2["episodes"][0]["seconds_in_range"] == 50  # [00:00:10, 00:01:00) 裁剪


# ---------- 经 /ingest 真实流水 ----------

def test_episodes_from_real_ingest_cycle(client):
    setup_rule_device(client)
    for eid, t, v in [
        ("e1", "2026-09-23T00:00:01Z", 81.0),   # entered
        ("e2", "2026-09-23T00:00:02Z", 69.0),   # cleared
        ("e3", "2026-09-23T00:00:03Z", 90.0),   # entered again（开放周期）
    ]:
        r = client.post("/ingest", json={"device_id": "dev-1", "events": [
            {"event_id": eid, "event_time": t, "value": v},
        ]})
        assert r.status_code == 200, r.text

    # 真实流水的 created_at 是写入时刻（测试运行当时），用跨越当前时刻的宽窗口查询
    body = episodes(client, "dev-1", "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z").json()
    assert len(body["episodes"]) == 2
    closed, ongoing = body["episodes"]
    assert closed["ongoing"] is False
    assert closed["entered_at"] is not None and closed["cleared_at"] is not None
    assert closed["seconds_in_range"] >= 0
    assert ongoing["ongoing"] is True
    assert ongoing["cleared_at"] is None
    assert ongoing["range_end"] is not None
    assert body["total_alert_seconds"] == closed["seconds_in_range"] + ongoing["seconds_in_range"]


def test_episodes_endpoint_is_read_only(client):
    setup_rule_device(client)
    insert_transition("dev-1", "e1", "entered", "2026-09-23T00:00:10Z")
    insert_transition("dev-1", "e2", "cleared", "2026-09-23T00:00:40Z")
    with db.session() as conn:
        before = conn.execute(
            "SELECT id, kind FROM device_alert_transitions WHERE device_id='dev-1' ORDER BY id"
        ).fetchall()
        before = [(r["id"], r["kind"]) for r in before]
    episodes(client, "dev-1", "2026-09-23T00:00:00Z", "2026-09-23T00:01:00Z")
    with db.session() as conn:
        after = conn.execute(
            "SELECT id, kind FROM device_alert_transitions WHERE device_id='dev-1' ORDER BY id"
        ).fetchall()
        after = [(r["id"], r["kind"]) for r in after]
    assert after == before


# ---------- 工具 ----------

def parse_iso(text):
    from datetime import datetime, timezone
    return datetime.fromisoformat(text.replace("Z", "Z")).astimezone(timezone.utc).timestamp()
