"""Agents package — lazy imports to avoid circular dependency."""


def __getattr__(name):
    if name in ('BaseAgent', 'Agent'):
        from src.agents.agent import BaseAgent, Agent
        return BaseAgent if name == 'BaseAgent' else Agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ['BaseAgent', 'Agent']
