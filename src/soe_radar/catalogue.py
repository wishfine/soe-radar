"""Install reviewable official-index excerpts without claiming a live verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from .models import Employer, RegistrySnapshot, RegistryState


def seed_catalogue(session, cfg):
    if not cfg.bundled_catalogue:
        return
    configured = {r.id for r in cfg.registries}
    for file in sorted(Path(__file__).with_name("catalogue").glob("*.json")):
        for bundle in json.loads(file.read_text())["bundles"]:
            registry_id = bundle["registry_id"]
            if registry_id not in configured:
                continue
            raw = json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode()
            digest = hashlib.sha256(raw).hexdigest()
            snapshot = session.scalar(
                select(RegistrySnapshot).where(
                    RegistrySnapshot.registry_id == registry_id, RegistrySnapshot.digest == digest
                )
            )
            if not snapshot:
                folder = cfg.data_dir / "registries"
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / (digest + ".json")
                path.write_bytes(raw)
                snapshot = RegistrySnapshot(
                    registry_id=registry_id,
                    digest=digest,
                    source_url=bundle["source_url"],
                    entries=bundle["entries"],
                    raw_path=str(path.relative_to(cfg.data_dir)),
                    provenance=bundle["provenance"],
                )
                session.add(snapshot)
                session.flush()
            state = session.get(RegistryState, registry_id)
            if not state:
                state = RegistryState(id=registry_id, status="bundled")
                session.add(state)
            # Live refreshes and manual imports always win over shipped data.
            active = (
                session.get(RegistrySnapshot, state.active_snapshot_id) if state.active_snapshot_id else None
            )
            if active and active.provenance.get("acquisition") != "official_index_excerpt":
                continue
            state.active_snapshot_id = snapshot.id
            for entry in bundle["entries"]:
                existing = session.scalar(select(Employer).where(Employer.name == entry["name"]))
                if not existing:
                    session.add(
                        Employer(
                            name=entry["name"],
                            aliases=entry.get("aliases", []),
                            org_type="unknown",
                            proof_url=bundle["source_url"],
                        )
                    )
            # Do not set last_success/verified_at: indexing is not a successful local fetch.
            session.flush()
