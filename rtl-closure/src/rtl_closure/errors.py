"""Typed controller failures with durable status and reporting identity."""

from __future__ import annotations


class PreRtlGateError(RuntimeError):
    """A verification-configuration gate failed before RTL generation."""

    failure_class = "verification_configuration"

    def __init__(self, gate: str, message: str, *, category: str | None = None):
        super().__init__(message)
        self.failure_gate = gate
        self.failure_category = category or gate
        self.failure_code = f"{self.failure_class}/{self.failure_category}"


class RepairExhaustedError(RuntimeError):
    """The frozen RTL verification suite could not be closed by repair."""

    failure_gate = "rtl_repair"

    def __init__(self, message: str, *, failure_class: str):
        super().__init__(message)
        self.failure_class = failure_class or "verification_failure"
        self.failure_code = f"{self.failure_class}/repair_exhausted"


class LaneLaunchError(RuntimeError):
    """LibreLane could not be launched; changing RTL cannot repair this."""

    failure_class = "infrastructure"
    failure_gate = "lane_launch"
    failure_code = "infrastructure/lane_launch"
    retryable = True
    repairable_by_rtl = False


class NonRepairableInfrastructureError(RuntimeError):
    """An external service or execution substrate failed independently of RTL."""

    failure_class = "infrastructure"
    repairable_by_rtl = False

    def __init__(
        self,
        message: str,
        *,
        gate: str,
        code: str,
        provider: str,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.failure_gate = gate
        self.failure_code = f"infrastructure/{code}"
        self.retryable = retryable
        self.provider = provider
        self.error = {
            "code": self.failure_code,
            "category": "infrastructure",
            "domain": gate,
            "repairable": False,
            "repairable_by_rtl": False,
            "retryable": retryable,
            "provider": provider,
            "message": message,
            "context": {},
            "evidence": [],
        }


class ModelInfrastructureError(NonRepairableInfrastructureError):
    """The configured model service could not satisfy a generation request."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(
            message,
            gate="model_request",
            code="model_request",
            provider="llm_backend",
            retryable=retryable,
        )


class ContainerInfrastructureError(NonRepairableInfrastructureError):
    """Docker or a container runtime failed before a tool verdict was available."""

    def __init__(self, message: str, *, provider: str, retryable: bool = True) -> None:
        super().__init__(
            message,
            gate="container_runtime",
            code=f"{provider}_container_runtime",
            provider=provider,
            retryable=retryable,
        )


class LaneReconciliationError(NonRepairableInfrastructureError):
    """A completed Lane delivery could not be re-attested."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            gate="lane_reconcile",
            code="lane_reconcile",
            provider="lane-agent",
            retryable=True,
        )
