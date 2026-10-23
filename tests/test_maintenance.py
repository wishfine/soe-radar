import json

from sqlalchemy import func, select
from test_store import sample
from typer.testing import CliRunner

from soe_radar.cli import app
from soe_radar.extraction import extract_rules
from soe_radar.maintenance import repair
from soe_radar.models import Activity, Batch, Document, Opportunity, PersonalState
from soe_radar.store import ingest


def test_duplicate_repair_keeps_evidence_personal_state_and_separate_years(db, cfg):
    doc = sample()
    row = ingest(db, cfg, doc, extract_rules(doc))
    other = sample(2028, "2027年10月31日")
    next_year = ingest(db, cfg, other, extract_rules(other))
    with db.session() as s:
        original = s.get(Opportunity, row["id"])
        activity = Activity(
            employer_id=original.employer_id,
            identity="legacy-drift",
            title=doc.title,
            cohorts=[2027],
            season="秋季",
        )
        s.add(activity)
        s.flush()
        batch = Batch(activity_id=activity.id, name="正式批")
        s.add(batch)
        s.flush()
        duplicate = Opportunity(
            activity_id=activity.id,
            batch_id=batch.id,
            employer_id=original.employer_id,
            title=doc.title,
            official_url=doc.url + "?CT6T=volatile",
            source_id="boc",
            cohorts=[2027],
            review_status="pending",
        )
        s.add(duplicate)
        s.flush()
        s.add(
            Document(
                source_id="boc",
                url=duplicate.official_url,
                title=doc.title,
                text=doc.text,
                digest="legacy",
                opportunity_id=duplicate.id,
            )
        )
        s.add(PersonalState(opportunity_id=original.id, saved=True, notes="保留备注"))
        s.add(PersonalState(opportunity_id=duplicate.id, applied=True, notes="另一条备注"))
    assert len(repair(db, cfg)["merged"]) == 1
    applied = repair(db, cfg, apply=True)
    winner = applied["merged"][0]["to"]
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Document)) == 3
        state = s.get(PersonalState, winner)
        assert state.saved and state.applied
        assert "保留备注" in state.notes and "另一条备注" in state.notes
        assert s.get(Opportunity, next_year["id"]).merged_into_id is None
    assert repair(db, cfg, apply=True)["merged"] == []


def test_annotation_versions_cannot_leak_across_dev_and_holdout(tmp_path):
    from soe_radar.schemas import DocumentInput

    doc = DocumentInput(
        url="https://www.boc.cn/example",
        title="中国银行2035届校园招聘",
        text="面向2035届，报名截止2035年10月31日。",
    )
    base = {
        "document": doc.model_dump(mode="json"),
        "annotation_type": "human",
        "annotator": "test-reviewer",
        "expected": {"kind": "date", "value": "2035-10-31"},
    }
    path = tmp_path / "labels.jsonl"
    path.write_text("\n".join(json.dumps({**base, "split": split}) for split in ("dev", "test")))
    result = CliRunner().invoke(app, ["dataset", "evaluate", str(path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["leaked_groups"] == 1 and not data["target_met"]
