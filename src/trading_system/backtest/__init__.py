"""Portfolio APIs, loaded on demand so shared research utilities do not load F alpha."""

from importlib import import_module

__all__ = [
    "BacktestEngine",
    "ExitReason",
    "PositionAction",
    "PositionDecision",
    "PositionManager",
    "PositionState",
    "compare_position_management",
    "compare_strategies",
]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = (
        "engine"
        if name in {"BacktestEngine", "compare_position_management", "compare_strategies"}
        else "position_manager"
    )
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value
