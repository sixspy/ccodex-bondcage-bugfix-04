"""开户名口径门禁（BUGFIX-04）。

拍板见 docs/开户名议事卡.md：甲路径 —— 空名拒开户。
- 失败测甲：空名建户 → 正式表零增 + 审计记拒写（开户名偏航）
- 失败测乙：空名仍能入笼 → 拒开后无户可入；遗产空名户同口径禁入
- 争用失败测：双人同键抢账户锁 → 后到 409 且争用痕落库

开户命运（测甲/测乙）与争用锁（争用测）分两段验收。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _count(sql: str, *args) -> int:
    with connect() as conn:
        return conn.execute(sql, args).fetchone()["c"]


# ---------- 验收段一：开户命运 ----------


def test_empty_name_open_rejected_zero_growth_and_logged():
    """失败测甲：空名建户。甲路径——拒写，正式表零增，审计留拒写痕。"""
    c = TestClient(app)
    h = _auth(c)
    before = _count("SELECT COUNT(*) c FROM bond_accounts")
    for bad_name in ("", "   "):
        r = c.post(
            "/api/bonds",
            headers=h,
            json={
                "party_key": "P-NONAME",
                "party_name": bad_name,
                "initial_deposit": 5000,
            },
        )
        assert r.status_code == 422, r.text
    # 正式名册零增
    assert _count("SELECT COUNT(*) c FROM bond_accounts") == before
    assert (
        _count("SELECT COUNT(*) c FROM bond_accounts WHERE party_key=?", "P-NONAME")
        == 0
    )
    # 入金流水零写
    assert (
        _count("SELECT COUNT(*) c FROM deposit_ledger WHERE party_key=?", "P-NONAME")
        == 0
    )
    # 拒写已记：开户名偏航
    drift = _count(
        "SELECT COUNT(*) c FROM audit_trail WHERE action='开户名偏航' AND target=?",
        "P-NONAME",
    )
    assert drift >= 1
    # 审计面可按口径检索拒写记录
    items = c.get("/api/audit", headers=h, params={"action": "开户名偏航"}).json()[
        "items"
    ]
    assert any(x["target"] == "P-NONAME" for x in items)
    # 对账面：无名正式户计数为零
    summary = c.get("/api/reconcile", headers=h).json()["summary"]
    assert summary["nameless_accounts"] == 0


def test_empty_name_never_enters_cage():
    """失败测乙：空名仍能入笼。拒开后无户可入；遗产空名户同口径挡在笼外。"""
    c = TestClient(app)
    h = _auth(c)
    cages_before = _count("SELECT COUNT(*) c FROM cages")
    # 空名开户被拒 → 名册无此键 → 入笼无户可挂
    c.post(
        "/api/bonds",
        headers=h,
        json={"party_key": "P-GHOST", "party_name": "", "initial_deposit": 5000},
    )
    r = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": "P-GHOST",
            "contract_no": "HT-GHOST-1",
            "amount": 1000,
            "beneficiary": "某业主",
        },
    )
    assert r.status_code in (404, 409), r.text
    assert _count("SELECT COUNT(*) c FROM cages WHERE party_key=?", "P-GHOST") == 0
    # 遗产脏数据防线：库里即便存在空名户，也禁止入笼
    with connect() as conn:
        conn.execute(
            """INSERT INTO bond_accounts(party_key,party_name,balance,frozen,status,created_at)
               VALUES ('P-LEGACY','',9000,0,'正常','2026-11-20T00:00:00')"""
        )
        conn.commit()
    r2 = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": "P-LEGACY",
            "contract_no": "HT-LEG-1",
            "amount": 1000,
            "beneficiary": "某业主",
        },
    )
    assert r2.status_code == 409, r2.text
    assert _count("SELECT COUNT(*) c FROM cages WHERE party_key=?", "P-LEGACY") == 0
    # 笼总数全程零增
    assert _count("SELECT COUNT(*) c FROM cages") == cages_before
    # 对账面须能亮出遗产无名户（检测口径存在且生效）
    summary = c.get("/api/reconcile", headers=h).json()["summary"]
    assert summary["nameless_accounts"] == 1


# ---------- 验收段二：争用锁 ----------


def test_same_key_open_contention_leaves_trace():
    """争用失败测：双人同键抢账户锁，后到 409 留争用痕，业务零写。"""
    c = TestClient(app)
    h_admin = _auth(c, "admin")
    h_clerk = _auth(c, "clerk")
    # admin 先持账户锁（其开户会话进行中）
    assert (
        c.post("/api/locks/acquire", headers=h_admin, json={"lock_key": "bond:P-RACE"})
        .status_code
        == 200
    )
    # clerk 同键开户：被锁挡下
    r = c.post(
        "/api/bonds",
        headers=h_clerk,
        json={"party_key": "P-RACE", "party_name": "竞态乙方", "initial_deposit": 1000},
    )
    assert r.status_code == 409, r.text
    # 后到一方留争用痕（当前实现回滚丢痕 → 红）
    with connect() as conn:
        cont = conn.execute(
            "SELECT * FROM contention_log WHERE lock_key=?", ("bond:P-RACE",)
        ).fetchall()
    assert len(cont) == 1
    assert cont[0]["actor"] == "clerk"
    # 业务零写：正式表无 P-RACE
    assert _count("SELECT COUNT(*) c FROM bond_accounts WHERE party_key=?", "P-RACE") == 0
    # 锁释后同键可开户：开户命运不被争用痕株连
    assert (
        c.post("/api/locks/release", headers=h_admin, json={"lock_key": "bond:P-RACE"})
        .status_code
        == 200
    )
    r2 = c.post(
        "/api/bonds",
        headers=h_clerk,
        json={"party_key": "P-RACE", "party_name": "竞态乙方", "initial_deposit": 1000},
    )
    assert r2.status_code == 200, r2.text
    assert _count("SELECT COUNT(*) c FROM bond_accounts WHERE party_key=?", "P-RACE") == 1
