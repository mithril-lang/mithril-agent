"""Private target fence for owned calls; never a public target grant or completeness claim."""

import json
from contextlib import nullcontext

from agent.code_child_attempts import _digest


class OwnedTargetBinding:
    def __init__(self, registration, args, task_id):
        self.resolver = registration.target_resolver if registration is not None else None
        self.scope = getattr(registration, "target_scope", None)
        self.admission_error = False
        self.target = None
        try:
            self.target = self._target(args, task_id)
            self.digest = _digest(self.target)[0] if self.target is not None else None
        except (TypeError, ValueError, OSError, RuntimeError):
            # Keep the failure inside the durable not-dispatched policy path.
            self.digest = None
            self.admission_error = True

    def _target(self, args, task_id):
        if self.resolver is None:
            return None
        captured = json.loads(json.dumps(args, ensure_ascii=False, allow_nan=False))
        target = self.resolver(captured, task_id)
        return json.loads(json.dumps(target, ensure_ascii=False, allow_nan=False))

    @property
    def consumes_revision(self):
        return self.scope is not None and self.target is not None

    def execution_scope(self, args, task_id):
        if self.scope is None:
            return nullcontext()
        return self.scope(args, task_id, self.target)

    def rejection(self, args, task_id):
        if self.admission_error:
            return "The owned tool target could not be resolved at admission."
        try:
            current = self._target(args, task_id)
            if (_digest(current)[0] if current is not None else None) != self.digest:
                return "The owned tool target changed after admission."
        except (TypeError, ValueError, OSError, RuntimeError):
            return "The owned tool target is no longer resolvable."
        return None
