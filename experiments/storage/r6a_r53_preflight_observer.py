"""Pass-through observation of the R51 scanner instance loaded by production."""
from __future__ import annotations

from typing import Any, Callable


class R51ScannerObserver:
    """Attach callbacks to the exact scanner object returned inside the gate.

    The loader and scanner methods always call through to their original
    implementations. Callback failures are recorded and cannot replace a
    production return value or exception; the harness must fail closed after
    the gate if ``callback_errors`` is non-empty.
    """

    METHODS = (
        "run_lsof_bounded",
        "process_snapshot",
        "executable_identity",
        "parse_lsof",
    )

    def __init__(
        self,
        retention: Any,
        *,
        on_load: Callable[[dict[str, Any]], None] | None = None,
        on_call: Callable[[dict[str, Any]], None] | None = None,
        on_exception: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.retention = retention
        self.on_load = on_load
        self.on_call = on_call
        self.on_exception = on_exception
        self.loaded_modules: list[Any] = []
        self.calls: dict[str, list[dict[str, Any]]] = {
            name: [] for name in self.METHODS
        }
        self.callback_errors: list[dict[str, str]] = []
        self._original_loader: Callable[..., Any] | None = None
        self._attached: list[tuple[Any, str, Any]] = []
        self._active = False

    def _notify(
        self,
        callback: Callable[[dict[str, Any]], None] | None,
        event: dict[str, Any],
        label: str,
    ) -> None:
        if callback is None:
            return
        try:
            callback(event)
        except BaseException as error:  # Evidence errors must not alter the gate call.
            self.callback_errors.append({
                "callback": label,
                "error_type": type(error).__name__,
                "error": str(error),
            })

    def _load_and_observe(self, *args: Any, **kwargs: Any) -> Any:
        assert self._original_loader is not None
        module = self._original_loader(*args, **kwargs)
        self.loaded_modules.append(module)
        load_event = {
            "module": module,
            "module_id": id(module),
            "module_name": getattr(module, "__name__", None),
            "module_file": getattr(module, "__file__", None),
            "module_spec_origin": getattr(getattr(module, "__spec__", None), "origin", None),
            "loader_callable_id": id(self._original_loader),
        }
        self._notify(self.on_load, load_event, "on_load")

        for name in self.METHODS:
            original = getattr(module, name)
            module_id = id(module)

            def observed(
                *call_args: Any,
                _method: str = name,
                _original: Any = original,
                _module_id: int = module_id,
                **call_kwargs: Any,
            ) -> Any:
                try:
                    result = _original(*call_args, **call_kwargs)
                except BaseException as error:
                    event = {
                        "method": _method,
                        "module_id": _module_id,
                        "callable_id": id(_original),
                        "args": call_args,
                        "kwargs": call_kwargs,
                        "exception": error,
                        "exception_id": id(error),
                    }
                    self.calls[_method].append(event)
                    self._notify(self.on_exception, event, "on_exception")
                    raise

                event = {
                    "method": _method,
                    "module_id": _module_id,
                    "callable_id": id(_original),
                    "args": call_args,
                    "kwargs": call_kwargs,
                    "result": result,
                    "result_id": id(result),
                    "passed_object_id": id(result),
                    "pass_through_identity": True,
                }
                self.calls[_method].append(event)
                self._notify(self.on_call, event, "on_call")
                return result

            self._attached.append((module, name, original))
            setattr(module, name, observed)

        return module

    def __enter__(self) -> "R51ScannerObserver":
        if self._active:
            raise RuntimeError("R51 scanner observer is already active")
        self._original_loader = self.retention._load_r51_lsof_scanner
        self.retention._load_r51_lsof_scanner = self._load_and_observe
        self._active = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            for module, name, original in reversed(self._attached):
                setattr(module, name, original)
        finally:
            self._attached.clear()
            if self._original_loader is not None:
                self.retention._load_r51_lsof_scanner = self._original_loader
            self._active = False
        return False
