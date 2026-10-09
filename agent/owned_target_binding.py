"""Private target fence for owned calls; never a public target grant or completeness claim."""

import json

from agent.code_child_attempts import _digest


class OwnedTargetBinding:
    def __init__(self, registration, args, task_id):
        self.resolver = registration.target_resolver if registration is not None else None
        self.admission_error = False
        try:
            self.digest = self._resolve(args, task_id)
        except (TypeError, ValueError, OSError, RuntimeError):
            # Keep the failure inside the durable not-dispatched policy path.
            self.digest = None
            self.admission_error = True

    def _resolve(self, args, task_id):
        if self.resolver is None:
            return None
        captured = json.loads(json.dumps(args, ensure_ascii=False, allow_nan=False))
        target = self.resolver(captured, task_id)
        # None means this resolver cannot bind the selected execution namespace.
        return _digest(target)[0] if target is not None else None

    def rejection(self, args, task_id):
        if self.admission_error:
            return "The owned tool target could not be resolved at admission."
        try:
            if self._resolve(args, task_id) != self.digest:
                return "The owned tool target changed after admission."
        except (TypeError, ValueError, OSError, RuntimeError):
            return "The owned tool target is no longer resolvable."
        return None
