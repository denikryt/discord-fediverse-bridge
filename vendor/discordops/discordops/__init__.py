"""DiscordOps public API for declarative operations and body-less policies."""

from .framework import (
    Operation,
    OperationDefinition,
    PolicyDefinition,
    Precondition,
    evaluate_policy,
    evaluate_policy_async,
    run_operation_definition,
    run_operation_definition_async,
)
from .types import OperationResult, PolicyResult
from . import gates

__version__ = "0.1.0"
__all__ = [
    "OperationDefinition",
    "PolicyDefinition",
    "Precondition",
    "Operation",
    "evaluate_policy",
    "evaluate_policy_async",
    "run_operation_definition",
    "run_operation_definition_async",
    "OperationResult",
    "PolicyResult",
    "gates",
]
