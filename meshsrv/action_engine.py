"""Lightweight action registry and runner for MeshCenter.

The engine intentionally stays synchronous for now because Node Tools already
serialize access to the Meshtastic radio.  It provides one stable abstraction
for current core actions and future plugin/device actions without changing the
existing HTTP endpoint or UI contract.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
import time
import uuid


ActionHandler = Callable[["ActionContext"], "ActionResult"]


@dataclass(frozen=True)
class ActionDefinition:
    """Metadata and handler for a registered action."""

    action_id: str
    title: str
    category: str
    handler: ActionHandler
    requires_radio: bool = False
    timeout_seconds: int = 70
    aliases: tuple[str, ...] = ()


@dataclass
class ActionContext:
    """Runtime context passed to an action handler."""

    action: ActionDefinition
    node_id: str
    node_name: str
    request_data: Mapping[str, Any]
    started_at: float = field(default_factory=time.time)
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class ActionResult:
    """Standard result returned by every MeshCenter action."""

    ok: bool
    state: str
    message: str
    data: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_code: str = ""
    http_status: int = 200

    @classmethod
    def success(
        cls,
        message: str,
        *,
        state: str = "success",
        data: Optional[Dict[str, Any]] = None,
        http_status: int = 200,
    ) -> "ActionResult":
        return cls(
            ok=True,
            state=state,
            message=message,
            data=dict(data or {}),
            http_status=http_status,
        )

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        error_code: str = "action_failed",
        state: str = "error",
        data: Optional[Dict[str, Any]] = None,
        http_status: int = 500,
    ) -> "ActionResult":
        return cls(
            ok=False,
            state=state,
            message=error,
            error=error,
            error_code=error_code,
            data=dict(data or {}),
            http_status=http_status,
        )


class ActionRegistry:
    """Register and resolve built-in or plugin-provided actions."""

    def __init__(self) -> None:
        self._actions: Dict[str, ActionDefinition] = {}
        self._aliases: Dict[str, str] = {}

    def register(self, definition: ActionDefinition) -> None:
        action_id = str(definition.action_id or "").strip()
        if not action_id:
            raise ValueError("Action ID must not be empty")
        if action_id in self._actions or action_id in self._aliases:
            raise ValueError(f"Action already registered: {action_id}")

        self._actions[action_id] = definition

        for alias in definition.aliases:
            alias_id = str(alias or "").strip()
            if not alias_id:
                continue
            if alias_id in self._actions or alias_id in self._aliases:
                raise ValueError(f"Action alias already registered: {alias_id}")
            self._aliases[alias_id] = action_id

    def resolve(self, action_id: str) -> Optional[ActionDefinition]:
        requested = str(action_id or "").strip()
        canonical = self._aliases.get(requested, requested)
        return self._actions.get(canonical)

    def contains(self, action_id: str) -> bool:
        return self.resolve(action_id) is not None

    def list(self, category: Optional[str] = None) -> Iterable[ActionDefinition]:
        actions = self._actions.values()
        if category is None:
            return tuple(actions)
        return tuple(action for action in actions if action.category == category)


class ActionRunner:
    """Execute registered actions and normalize unexpected failures."""

    def __init__(
        self,
        registry: ActionRegistry,
        *,
        before_run: Optional[Callable[[ActionContext], None]] = None,
        after_run: Optional[Callable[[ActionContext, ActionResult], None]] = None,
    ) -> None:
        self.registry = registry
        self.before_run = before_run
        self.after_run = after_run

    def run(
        self,
        action_id: str,
        *,
        node_id: str,
        node_name: str,
        request_data: Optional[Mapping[str, Any]] = None,
    ) -> tuple[Optional[ActionContext], ActionResult]:
        definition = self.registry.resolve(action_id)
        if definition is None:
            return None, ActionResult.failure(
                "Unsupported node action",
                error_code="unsupported_action",
                http_status=400,
            )

        context = ActionContext(
            action=definition,
            node_id=node_id,
            node_name=node_name,
            request_data=request_data or {},
        )

        result: ActionResult
        try:
            if self.before_run is not None:
                self.before_run(context)

            result = definition.handler(context)
            if not isinstance(result, ActionResult):
                raise TypeError(
                    f"Action handler {definition.action_id} returned "
                    f"{type(result).__name__}, expected ActionResult"
                )
        except Exception as error:
            result = ActionResult.failure(
                "The action failed unexpectedly. See System Log for details.",
                error_code="action_exception",
                data={"technical_error": str(error)},
                http_status=500,
            )
        finally:
            if "result" in locals() and self.after_run is not None:
                try:
                    self.after_run(context, result)
                except Exception:
                    pass

        elapsed = max(0.0, time.time() - context.started_at)
        result.data.setdefault("job_id", context.job_id)
        result.data.setdefault("action", definition.action_id)
        result.data.setdefault("action_title", definition.title)
        result.data.setdefault("category", definition.category)
        result.data.setdefault("node_id", node_id)
        result.data.setdefault("node_name", node_name)
        result.data.setdefault("state", result.state)
        result.data.setdefault("started_at", context.started_at)
        result.data.setdefault("finished_at", time.time())
        result.data.setdefault("elapsed_seconds", round(elapsed, 3))
        return context, result
