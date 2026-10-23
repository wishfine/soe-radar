import json
import tarfile

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from typer.testing import CliRunner

from soe_radar.cli import app
from soe_radar.config import load_settings
from soe_radar.db import initialize
from soe_radar.models import Base

runner = CliRunner()


def test_migration_matches_models(db):
    with db.engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []


def test_clone_init_doctor_and_backup_restore(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SOE_RADAR_CONFIG", raising=False)
    assert runner.invoke(app, ["init"]).exit_code == 0
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["allow_paid"] is False
    db = initialize(load_settings())
    with db.session() as s:
        from soe_radar.models import Employer

        s.add(Employer(name="恢复验证企业"))
    db.engine.dispose()
    result = runner.invoke(app, ["backup", str(tmp_path / "backup.tar.gz")])
    assert result.exit_code == 0, result.output
    with tarfile.open(tmp_path / "backup.tar.gz") as archive:
        assert "radar.db" in archive.getnames()
        assert "web-token" not in archive.getnames()
    assert runner.invoke(app, ["restore", str(tmp_path / "backup.tar.gz")]).exit_code != 0
    result = runner.invoke(app, ["restore", str(tmp_path / "backup.tar.gz"), "--force"])
    assert result.exit_code == 0, result.output
    assert runner.invoke(app, ["observe"]).exit_code == 0
    assert json.loads(runner.invoke(app, ["observe"]).output)["seven_day_observation_complete"] is False


def test_machine_generated_labels_cannot_count_as_human_acceptance(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text(
        json.dumps(
            {
                "annotation_type": "synthetic",
                "annotator": "machine",
                "split": "test",
                "expected": {"kind": "date", "value": "2035-12-31"},
            }
        )
        + "\n"
    )
    result = runner.invoke(app, ["dataset", "evaluate", str(path)])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["human_holdout"] == 0
    assert data["target_met"] is False


def test_restore_refuses_active_worker_and_serve_refuses_live_pid(tmp_path, monkeypatch):
    import os

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SOE_RADAR_CONFIG", raising=False)
    assert runner.invoke(app, ["init"]).exit_code == 0
    cfg = load_settings()
    marker = cfg.data_dir / "server.pid"
    marker.write_text(str(os.getpid()))
    assert runner.invoke(app, ["serve"]).exit_code != 0
    assert marker.read_text() == str(os.getpid())
    marker.unlink()
    db = initialize(cfg)
    db.acquire("sync", "test-worker")
    result = runner.invoke(app, ["restore", str(tmp_path / "nonexistent.tar.gz"), "--force"])
    assert result.exit_code != 0
    assert "租约" in result.output
    db.engine.dispose()
