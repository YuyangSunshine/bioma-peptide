"""Production evidence-gated scientific decision control."""
from .kernel import BeliefState, DecisionProblem, EvidenceChannel, EGVoIController
from .belief import DerivedBelief, EvidenceBeliefBridge, EvidenceLikelihoodModel
from .bellman import BellmanDecision, BellmanRiskController
from .decision import ScientificDecisionCompiler, TableScientificPredictor
from .session import DecisionSession, resolve_problem, predictor_channel
from .governed import GovernedCandidate, GovernedDecisionEpisode
