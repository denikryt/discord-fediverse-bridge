"""Discord Command Framework — declarative operations and preconditions for Discord bots."""

from .framework import (
    OperationDefinition,
    Operation,
    Precondition,
    run_operation_definition,
    run_operation_definition_async,
)
from .types import OperationResult
from . import gates

__version__ = "0.1.0"
__all__ = [
    "OperationDefinition",
    "Precondition",
    "Operation",
    "run_operation_definition",
    "run_operation_definition_async",
    "OperationResult",
    "gates",
]
