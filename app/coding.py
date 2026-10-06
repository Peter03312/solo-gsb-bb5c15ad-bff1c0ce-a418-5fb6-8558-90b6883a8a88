"""盲法编码核心逻辑：生成、校验、冲突检测与揭盲。

设计要点：
- 样品码由固定随机种子确定性生成，格式为「前缀-XXXX-XXXX」，字符集去除
  易混淆字符（0/O、1/I/L），编码本身不含任何组别含义。
- 每层内部按处理组做平衡区组随机分配，再将打乱后的样品码随机对应到样品，
  因此编码顺序不泄露分层与组别信息。
- 所有写入在单个事务中完成：任何校验失败或样品码冲突都会整批回滚。
"""

import hashlib
import json
import random

# 去除易混淆字符 0/O、1/I/L
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

MIN_REASON_LENGTH = 10


class ValidationError(Exception):
    """业务校验失败，errors 为可展示给用户的原因列表。"""

    def __init__(self, errors):
        self.errors = list(errors) if isinstance(errors, (list, tuple)) else [errors]
        super().__init__("; ".join(self.errors))


class CodeConflictError(Exception):
    """本次生成会复用已发放的样品码。"""

    def __init__(self, conflicts):
        self.conflicts = sorted(conflicts)
        super().__init__("样品码冲突: " + ", ".join(self.conflicts))


def _stratum_seed(batch_seed, label):
    """由批次种子确定性派生子种子（不受 PYTHONHASHSEED 影响）。"""
    digest = hashlib.sha256(f"{batch_seed}::{label}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _make_code(rng, prefix):
    part = lambda: "".join(rng.choice(ALPHABET) for _ in range(4))  # noqa: E731
    return f"{prefix}-{part()}-{part()}"


def validate_plan(name, group_names, strata):
    """校验编码方案。strata 为 [(label, count|None), ...]，返回清洗后的组名列表。"""
    errors = []
    if not name.strip():
        errors.append("批次名称不能为空")

    groups = [g.strip() for g in group_names if g and g.strip()]
    if len(groups) < 2:
        errors.append("处理组至少需要 2 个")
    if len(set(groups)) != len(groups):
        errors.append("处理组名称存在重复")

    if not strata:
        errors.append("至少需要录入一个分层")

    seen = set()
    for label, count in strata:
        label = label.strip()
        if not label:
            errors.append("存在未填写名称的分层（缺少分层因素）")
            continue
        if label in seen:
            errors.append(f"分层「{label}」重复录入")
        seen.add(label)
        if count is None:
            errors.append(f"分层「{label}」缺少样品数量或数量格式不正确")
        elif count <= 0:
            errors.append(f"分层「{label}」缺少样品（数量必须大于 0）")
        elif groups and count % len(groups) != 0:
            errors.append(
                f"分层「{label}」数量 {count} 无法平衡分配到 {len(groups)} 个处理组"
                f"（数量不平衡，需为 {len(groups)} 的整数倍）"
            )

    if errors:
        raise ValidationError(errors)
    return groups


def create_batch(db, *, name, seed, group_names, strata, prefix, created_by):
    """生成一个编码批次。失败时抛出 ValidationError / CodeConflictError，且不写入任何数据。"""
    groups = validate_plan(name, group_names, strata)
    strata = [(label.strip(), count) for label, count in strata]
    total = sum(count for _, count in strata)

    # 1) 基于固定种子确定性生成本批样品码
    rng = random.Random(_stratum_seed(seed, "__codes__"))
    codes = set()
    while len(codes) < total:
        codes.add(_make_code(rng, prefix))
    codes = list(codes)
    rng.shuffle(codes)

    # 2) 冲突预检：任何样品码已被发放 -> 拒绝整批并说明冲突
    placeholders = ",".join("?" for _ in codes)
    rows = db.execute(
        f"SELECT code FROM samples WHERE code IN ({placeholders})", codes
    ).fetchall()
    if rows:
        raise CodeConflictError([r["code"] for r in rows])

    # 3) 每层内平衡区组随机分配组别
    assignments = []  # [(stratum_label, group_name), ...] 与 codes 一一对应
    for label, count in strata:
        per_group = count // len(groups)
        pool = [g for g in groups for _ in range(per_group)]
        random.Random(_stratum_seed(seed, label)).shuffle(pool)
        assignments.extend((label, g) for g in pool)

    # 4) 单事务写入；任何一步失败整批回滚
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute(
            "INSERT INTO batches (name, seed, created_by) VALUES (?, ?, ?)",
            (name.strip(), seed, created_by),
        )
        batch_id = cur.lastrowid

        group_ids = {}
        for gname in groups:
            cur = db.execute(
                "INSERT INTO groups (batch_id, name) VALUES (?, ?)", (batch_id, gname)
            )
            group_ids[gname] = cur.lastrowid

        stratum_ids = {}
        for label, count in strata:
            cur = db.execute(
                "INSERT INTO strata (batch_id, label, expected_count) VALUES (?, ?, ?)",
                (batch_id, label, count),
            )
            stratum_ids[label] = cur.lastrowid

        for code, (label, gname) in zip(codes, assignments):
            db.execute(
                "INSERT INTO samples (batch_id, stratum_id, group_id, code) VALUES (?, ?, ?, ?)",
                (batch_id, stratum_ids[label], group_ids[gname], code),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return batch_id


def unblind_batch(db, *, batch_id, operator1, operator2, reason):
    """揭盲。任一条件不满足时抛出 ValidationError，且不改变任何数据。"""
    op1 = (operator1 or "").strip()
    op2 = (operator2 or "").strip()
    reason = (reason or "").strip()

    errors = []
    if not op1 or not op2:
        errors.append("揭盲需要输入两位操作者")
    elif op1 == op2:
        errors.append("两位操作者不能为同一人")
    if len(reason) < MIN_REASON_LENGTH:
        errors.append(f"揭盲理由不完整（需不少于 {MIN_REASON_LENGTH} 个字符）")
    if errors:
        raise ValidationError(errors)

    db.execute("BEGIN IMMEDIATE")
    try:
        batch = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if batch is None:
            raise ValidationError("批次不存在")
        if batch["status"] == "unblinded":
            raise ValidationError("该批次已揭盲，不能重复揭盲")

        rows = db.execute(
            """
            SELECT s.code AS code, st.label AS stratum, g.name AS group_name
            FROM samples s
            JOIN strata st ON st.id = s.stratum_id
            JOIN groups g ON g.id = s.group_id
            WHERE s.batch_id = ?
            ORDER BY s.code
            """,
            (batch_id,),
        ).fetchall()
        snapshot = json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2)

        db.execute(
            "INSERT INTO unblinding_events (batch_id, operator1, operator2, reason, snapshot)"
            " VALUES (?, ?, ?, ?, ?)",
            (batch_id, op1, op2, reason, snapshot),
        )
        db.execute(
            "UPDATE batches SET status = 'unblinded', unblinded_at = datetime('now') WHERE id = ?",
            (batch_id,),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
