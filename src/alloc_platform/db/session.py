"""Database session and SQLite engine initialization with WAL pragmas."""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from alloc_platform.config import config
from alloc_platform.db.models import Base

# Construct sqlite connection URL
_db_url = (
    f"sqlite:///{config.db_path}" if not config.db_path.startswith("sqlite") else config.db_path
)

engine = create_engine(
    _db_url,
    connect_args={"check_same_thread": False},
    echo=False,
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Initializes tables in the SQLite database."""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency for yielding database sessions."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
