"""Native peptide computation services, independent of Agent decision policy.

The research controller selects methods; these services enforce job and result
contracts. No fixed scientific pipeline is executed by importing this package.
"""
from .broker import Broker

__all__ = ["Broker"]
