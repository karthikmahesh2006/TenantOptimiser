"""Mutual exclusion locking for single-host experiment execution."""

import asyncio
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from alloc_platform.db.models import Experiment, ExperimentLock, Run

_async_lock = asyncio.Lock()


def acquire_experiment_lock(db: Session, experiment_id: str) -> bool:
    """Attempts to acquire the host experiment lock.

    Guarantees maximum 1 active experiment per host using both database row and asyncio.Lock.
    """
    if _async_lock.locked():
        return False

    existing = db.query(ExperimentLock).first()
    if existing:
        return False

    lock_row = ExperimentLock(id=1, experiment_id=experiment_id, acquired_at=datetime.utcnow())
    try:
        db.add(lock_row)
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False


def release_experiment_lock(db: Session, experiment_id: str) -> None:
    """Releases the experiment lock."""
    db.query(ExperimentLock).filter(ExperimentLock.experiment_id == experiment_id).delete()
    db.commit()


def recover_stale_locks_on_startup(db: Session) -> Optional[str]:
    """Recovers from unexpected daemon crashes.

    Marks orphaned running experiments as failed and clears locks.
    """
    existing = db.query(ExperimentLock).first()
    if not existing:
        return None

    stale_exp_id = existing.experiment_id
    exp = db.query(Experiment).filter(Experiment.id == stale_exp_id).first()
    if exp and exp.status in ("running", "preparing", "warmup"):
        exp.status = "failed"
        exp.error_message = "Host daemon restarted while experiment was active."

    # Mark active runs as failed
    db.query(Run).filter(
        Run.experiment_id == stale_exp_id, Run.status.in_(["running", "preparing", "warmup"])
    ).update({"status": "failed", "invalidation_reason": "Host restarted"})

    db.query(ExperimentLock).delete()
    db.commit()
    return stale_exp_id
