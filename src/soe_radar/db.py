from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import Employer, SourceState, stamp


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30}
        )

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        with self.sessions() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def migrate(self):
        cfg = Config()
        cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.engine.begin() as connection:
            cfg.attributes["connection"] = connection
            command.upgrade(cfg, "head")

    def acquire(self, name: str, owner: str, seconds: int = 300) -> bool:
        now = stamp()
        until = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
        with self.session() as s:
            s.execute(
                text("INSERT OR IGNORE INTO leases(name,owner,expires_at) VALUES(:n,'','')"), {"n": name}
            )
            changed = s.execute(
                text(
                    "UPDATE leases SET owner=:o, expires_at=:e WHERE name=:n AND (expires_at<:t OR owner=:o)"
                ),
                {"o": owner, "e": until, "n": name, "t": now},
            )
            return changed.rowcount == 1

    def release(self, name: str, owner: str):
        with self.session() as s:
            s.execute(text("DELETE FROM leases WHERE name=:n AND owner=:o"), {"n": name, "o": owner})


def seed(s: Session, cfg: Settings):
    for source in cfg.sources:
        if not s.get(SourceState, source.id):
            s.add(SourceState(id=source.id))
        if source.employer and not s.scalar(select(Employer).where(Employer.name == source.employer)):
            s.add(
                Employer(
                    name=source.employer,
                    aliases=source.aliases,
                    org_type="soe",
                    proof_url=source.homepage_url or source.url,
                    industry=source.industry,
                )
            )
    s.flush()
    for entry in cfg.employers:
        employer = s.scalar(select(Employer).where(Employer.name == entry.name))
        if not employer:
            employer = Employer(name=entry.name)
            s.add(employer)
        employer.aliases = entry.aliases
        employer.org_type = "soe"
        employer.proof_url = entry.proof_url
        employer.verified_at = stamp()
        employer.industry = entry.industry
    s.flush()
    for entry in cfg.employers:
        if entry.parent:
            employer = s.scalar(select(Employer).where(Employer.name == entry.name))
            parent = s.scalar(select(Employer).where(Employer.name == entry.parent))
            if not parent or parent.id == employer.id:
                raise ValueError(f"企业名录的上级主体不存在或引用自身：{entry.name}")
            employer.parent_id = parent.id


def initialize(cfg: Settings) -> Database:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.data_dir / "radar.db")
    db.migrate()
    with db.session() as s:
        seed(s, cfg)
    return db
