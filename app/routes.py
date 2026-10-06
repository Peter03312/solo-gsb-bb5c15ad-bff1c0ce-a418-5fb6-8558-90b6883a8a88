import json
import re

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from . import coding
from .auth import login_required, role_required
from .db import get_db

bp = Blueprint("main", __name__)


@bp.route("/healthz")
def healthz():
    return {"status": "ok"}


@bp.route("/")
@login_required
def index():
    batches = get_db().execute(
        """
        SELECT b.*, (SELECT COUNT(*) FROM samples s WHERE s.batch_id = b.id) AS sample_count
        FROM batches b ORDER BY b.id DESC
        """
    ).fetchall()
    return render_template("index.html", batches=batches)


def _parse_groups(raw):
    return [p.strip() for p in re.split(r"[,，、;；\s]+", raw or "") if p.strip()]


def _parse_strata(form):
    strata = []
    for label, raw in zip(form.getlist("stratum_label"), form.getlist("stratum_count")):
        label, raw = label.strip(), raw.strip()
        if not label and not raw:
            continue
        try:
            count = int(raw)
        except ValueError:
            count = None
        strata.append((label, count))
    return strata


def _parse_seed(raw):
    raw = (raw or "").strip()
    if not raw:
        return current_app.config["DEFAULT_SEED"]
    try:
        return int(raw)
    except ValueError:
        raise coding.ValidationError(f"随机种子「{raw}」不是有效整数")


@bp.route("/batches/new", methods=("GET", "POST"))
@role_required("manager")
def new_batch():
    if request.method == "POST":
        try:
            batch_id = coding.create_batch(
                get_db(),
                name=request.form.get("name", ""),
                seed=_parse_seed(request.form.get("seed")),
                group_names=_parse_groups(request.form.get("groups")),
                strata=_parse_strata(request.form),
                prefix=current_app.config["CODE_PREFIX"],
                created_by=g.user["username"],
            )
        except coding.CodeConflictError as e:
            flash(
                f"本次生成被拒绝：{len(e.conflicts)} 个样品码已被发放，不可复用。整批未生成。",
                "error",
            )
            for code in e.conflicts[:20]:
                flash(f"冲突样品码：{code}", "error")
            if len(e.conflicts) > 20:
                flash(f"……以及另外 {len(e.conflicts) - 20} 个冲突", "error")
            flash("请更换随机种子后重新生成。", "error")
        except coding.ValidationError as e:
            flash("本次生成被拒绝，整批未生成：", "error")
            for err in e.errors:
                flash(err, "error")
        else:
            flash("编码批次生成成功，样品码已发放。", "success")
            return redirect(url_for("main.batch_detail", batch_id=batch_id))
    return render_template("new_batch.html", default_seed=current_app.config["DEFAULT_SEED"])


def _get_batch_or_404(db, batch_id):
    batch = db.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if batch is None:
        abort(404)
    return batch


@bp.route("/batches/<int:batch_id>")
@login_required
def batch_detail(batch_id):
    db = get_db()
    batch = _get_batch_or_404(db, batch_id)
    strata = db.execute(
        "SELECT * FROM strata WHERE batch_id = ? ORDER BY id", (batch_id,)
    ).fetchall()
    groups = db.execute(
        "SELECT * FROM groups WHERE batch_id = ? ORDER BY id", (batch_id,)
    ).fetchall()
    # 盲态视图：只展示样品码与所属分层，不展示组别
    samples = db.execute(
        """
        SELECT s.code, st.label AS stratum
        FROM samples s JOIN strata st ON st.id = s.stratum_id
        WHERE s.batch_id = ? ORDER BY s.code
        """,
        (batch_id,),
    ).fetchall()
    events = db.execute(
        "SELECT id, operator1, operator2, reason, created_at FROM unblinding_events"
        " WHERE batch_id = ? ORDER BY id DESC",
        (batch_id,),
    ).fetchall()
    return render_template(
        "batch_detail.html",
        batch=batch,
        strata=strata,
        groups=groups,
        samples=samples,
        events=events,
    )


@bp.route("/batches/<int:batch_id>/mapping")
@role_required("viewer")
def mapping(batch_id):
    db = get_db()
    batch = _get_batch_or_404(db, batch_id)
    rows = db.execute(
        """
        SELECT s.code, st.label AS stratum, g.name AS group_name
        FROM samples s
        JOIN strata st ON st.id = s.stratum_id
        JOIN groups g ON g.id = s.group_id
        WHERE s.batch_id = ? ORDER BY s.code
        """,
        (batch_id,),
    ).fetchall()
    return render_template("mapping.html", batch=batch, rows=rows)


@bp.route("/batches/<int:batch_id>/unblind", methods=("GET", "POST"))
@role_required("manager", "viewer")
def unblind(batch_id):
    db = get_db()
    batch = _get_batch_or_404(db, batch_id)
    if request.method == "POST":
        try:
            coding.unblind_batch(
                db,
                batch_id=batch_id,
                operator1=request.form.get("operator1"),
                operator2=request.form.get("operator2"),
                reason=request.form.get("reason"),
            )
        except coding.ValidationError as e:
            flash("揭盲被拒绝，编码状态未改变：", "error")
            for err in e.errors:
                flash(err, "error")
        else:
            flash("揭盲成功，已记录时间、操作者与映射快照。", "success")
            return redirect(url_for("main.unblinding_events", batch_id=batch_id))
    return render_template("unblind.html", batch=batch)


@bp.route("/batches/<int:batch_id>/events")
@login_required
def unblinding_events(batch_id):
    db = get_db()
    batch = _get_batch_or_404(db, batch_id)
    events = db.execute(
        "SELECT * FROM unblinding_events WHERE batch_id = ? ORDER BY id DESC", (batch_id,)
    ).fetchall()
    # 映射快照属授权信息，仅 viewer 角色可见内容
    can_view_snapshot = g.user["role"] == "viewer"
    parsed = {e["id"]: json.loads(e["snapshot"]) for e in events} if can_view_snapshot else {}
    return render_template(
        "events.html",
        batch=batch,
        events=events,
        snapshots=parsed,
        can_view_snapshot=can_view_snapshot,
    )
