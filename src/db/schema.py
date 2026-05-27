"""Schema bootstrap helpers for the bridge database.

Database owns engine construction and calls into this module for metadata-based
schema creation. This module must not create engines or session factories.
"""

from sqlalchemy.engine import Engine

from ..models import Base


def create_all(engine: Engine) -> None:
    """Create the full clean-schema set required by the current codebase."""
    Base.metadata.create_all(engine)
