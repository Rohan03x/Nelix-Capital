"""validation — Post-model quality checks, shared-brain harness, and diagnostics."""

from .shared_brain import (
	collect_operational_diagnostics,
	evaluate_default_suite,
	evaluate_shared_brain,
	summarize_acceptance,
)

__all__ = [
	"collect_operational_diagnostics",
	"evaluate_default_suite",
	"evaluate_shared_brain",
	"summarize_acceptance",
]
