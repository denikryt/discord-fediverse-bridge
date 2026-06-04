# discordops

A declarative operation, policy, and precondition framework for Discord bots.

## What is it?

This framework provides a clean way to define bot operations (commands, actions) with ordered preconditions. Each operation:
1. Declares what conditions must be true (preconditions)
2. Defines what happens on rejection
3. Defines the main operation logic

The framework enforces the order of precondition checks and short-circuits on the first failure.

## Quick Start

```python
from dataclasses import dataclass
from discordops import Operation, Precondition, OperationResult

@dataclass
class GreetInput:
    user_name: str
    is_admin: bool

class GreetOperation(Operation):
    name = "greet"
    
    preconditions = (
        Precondition(
            name="admin_only",
            message="Only admins can greet",
            predicate=lambda inp: inp.is_admin,
        ),
    )
    
    def reject(self, input, *, reason, message, **kwargs):
        return OperationResult(applied=False, message=message, reason=reason)
    
    def body(self, input):
        return OperationResult(
            applied=True,
            message=f"Hello {input.user_name}!"
        )

# Use it
operation = GreetOperation()
result = operation.execute(GreetInput(user_name="Alice", is_admin=True))
print(result.message)  # "Hello Alice!"
```

## Installation

```bash
pip install -e .
```

Or as a git submodule:
```bash
git submodule add <repo-url> discordops
pip install -e discordops/
```

## Concepts

### Precondition
Guards an operation. Each precondition has:
- `name`: Identifier (e.g., "admin_only")
- `message`: String or callable returning user-visible message
- `predicate`: Function returning True if condition passes

Preconditions are evaluated in order and short-circuit on first failure.

### OperationDefinition
Describes an operation declaratively:
- `name`: Operation identifier
- `preconditions`: Tuple of Precondition instances (evaluated in order)
- `reject`: Handler called when a precondition fails
- `body`: Handler called when all preconditions pass


### PolicyDefinition and PolicyResult
A policy evaluates an ordered tuple of the same reusable `Precondition` objects
without defining an operation body or rejection callback. It returns a neutral
`PolicyResult`, leaving presentation and control flow to the application.

```python
from discordops import PolicyDefinition, Precondition, evaluate_policy_async

GUILD_ACCESS = PolicyDefinition(
    name="guild_access",
    preconditions=(
        Precondition(
            name="guild_required",
            message="Use this command in a server.",
            predicate=lambda value: value.guild_id is not None,
        ),
    ),
)

result = await evaluate_policy_async(GUILD_ACCESS, command_input)
if not result.allowed:
    await interaction.response.send_message(result.message, ephemeral=True)
```

Policy evaluation never sends Discord responses. Commands, autocomplete, HTTP
adapters, and other consumers decide how a denial is presented.

### OperationResult
Returned by operations. Contains:
- `applied`: True if operation succeeded, False if rejected
- `message`: User-visible message (shown to user)
- `reason`: Optional reason code identifying failed precondition (e.g., "admin_only")
- `extra_kwargs`: Optional dict for passing extra fields to reject handler

### Operation (Base Class)
Declarative base class for operations. Subclass to define your operation:
- Set `name` and `preconditions` as class attributes
- Implement `reject()` method for handling rejections
- Implement `body()` method for operation logic
- Call `execute(input)` to run the operation

### Discord Gates
Basic checks for common Discord scenarios:
- `has_actor_authority(interaction)` — returns True if user is admin (pure check, no response)
- `require_guild_context(interaction)` — returns guild if in server, else sends rejection
- `require_actor_authority(interaction)` — returns True if admin, else sends rejection

## API Reference

### framework.py
- `Precondition(name, message, predicate, reject_kwargs_factory=None)` — defines a condition
- `OperationDefinition(name, preconditions, reject, body)` — defines operation contract
- `PolicyDefinition(name, preconditions)` — defines a body-less ordered policy
- `evaluate_policy(...)` / `evaluate_policy_async(...)` — evaluate policy conditions
- `run_operation_definition(...)` / `run_operation_definition_async(...)` — execute an operation
- `Operation` — base class for declarative operations

### types.py
- `OperationResult(applied, message, reason=None, extra_kwargs=None)` — operation result
- `PolicyResult(allowed, reason=None, message=None, extra_kwargs=None)` — policy result

### gates.py
- `has_actor_authority(interaction)` — check if user is admin
- `require_guild_context(interaction)` — ensure command is in a server
- `require_actor_authority(interaction)` — require admin with automatic rejection

## Testing Operations

Test operations without Discord by passing mock input objects:

```python
def test_greet_rejects_non_admin():
    op = GreetOperation()
    result = op.execute(GreetInput(user_name="Bob", is_admin=False))
    assert result.applied is False
    assert result.reason == "admin_only"
    assert "admin" in result.message.lower()

def test_greet_succeeds_for_admin():
    op = GreetOperation()
    result = op.execute(GreetInput(user_name="Alice", is_admin=True))
    assert result.applied is True
    assert "Alice" in result.message
```

Operations can have multiple preconditions that are evaluated in order:

```python
class ComplexOperation(Operation):
    name = "complex"
    preconditions = (
        Precondition(
            name="admin_required",
            message="Must be admin",
            predicate=lambda inp: inp.is_admin,
        ),
        Precondition(
            name="valid_value",
            message="Value must be positive",
            predicate=lambda inp: inp.value > 0,
        ),
    )
    
    def reject(self, input, *, reason, message, **kwargs):
        return OperationResult(applied=False, message=message, reason=reason)
    
    def body(self, input):
        return OperationResult(applied=True, message=f"Processed {input.value}")
```

The framework short-circuits on the first failing precondition.

## Using in Discord Handlers

Handlers are your application code, not part of the framework. A typical handler:

1. Extracts context from interaction
2. Evaluates any body-less ingress policy
3. Builds operation input
4. Executes operation
5. Sends response

```python
async def handle_greet(interaction, user_name: str):
    # 1. Extract context
    is_admin = interaction.user.guild_permissions.administrator
    
    # 2. Build input
    input = GreetInput(user_name=user_name, is_admin=is_admin)
    
    # 3. Execute operation
    op = GreetOperation()
    result = op.execute(input)
    
    # 4. Send response
    await interaction.response.send_message(result.message, ephemeral=True)
```

## License

MIT
