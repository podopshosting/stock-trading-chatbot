"""Prediction research.

This package is RESEARCH_ONLY and the mode is not a variable. Nothing
here may influence scanner selection, trade hypotheses, the Risk
Governor, position sizing, exits, or any order. A prediction is a
recorded claim about the future that is later scored; it is not an
input to a decision.

The enforcement is structural rather than remembered: this package may
not import the decision path, and a test asserts it. A flag that says
RESEARCH_ONLY can be flipped by anyone in a hurry; an import that does
not exist cannot be flipped by accident.
"""
PREDICTION_MODE = "RESEARCH_ONLY"

# Modules this package must never import, because importing any of them
# would put a prediction on a code path that decides something.
FORBIDDEN_IMPORTS = (
    "agent.broker",
    "agent.risk",
    "agent.hypothesis",
    "agent.positions",
    "agent.scanner",
    "agent.orchestration",
)
