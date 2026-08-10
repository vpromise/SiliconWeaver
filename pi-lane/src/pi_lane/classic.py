"""Versioned metadata for LibreLane 3.0.5's built-in Classic flow."""

from __future__ import annotations

from typing import Final


# Keep this list in execution order. Repeated implementation IDs use the
# suffixes assigned by SequentialFlow.__normalize_step_ids().
CLASSIC_STEP_PHASES: Final[tuple[tuple[str, str], ...]] = (
    ("Verilator.Lint", "synthesis"),
    ("Checker.LintTimingConstructs", "synthesis"),
    ("Checker.LintErrors", "synthesis"),
    ("Checker.LintWarnings", "synthesis"),
    ("Yosys.JsonHeader", "synthesis"),
    ("Yosys.Synthesis", "synthesis"),
    ("Checker.YosysUnmappedCells", "synthesis"),
    ("Checker.YosysSynthChecks", "synthesis"),
    ("Checker.NetlistAssignStatements", "synthesis"),
    ("OpenROAD.CheckSDCFiles", "synthesis"),
    ("OpenROAD.CheckMacroInstances", "floorplan"),
    ("OpenROAD.STAPrePNR", "synthesis"),
    ("OpenROAD.Floorplan", "floorplan"),
    ("OpenROAD.DumpRCValues", "floorplan"),
    ("Odb.CheckMacroAntennaProperties", "floorplan"),
    ("Odb.SetPowerConnections", "floorplan"),
    ("Odb.ManualMacroPlacement", "floorplan"),
    ("OpenROAD.CutRows", "floorplan"),
    ("OpenROAD.TapEndcapInsertion", "floorplan"),
    ("Odb.AddPDNObstructions", "floorplan"),
    ("OpenROAD.GeneratePDN", "floorplan"),
    ("Odb.RemovePDNObstructions", "floorplan"),
    ("Odb.AddRoutingObstructions", "floorplan"),
    ("OpenROAD.GlobalPlacementSkipIO", "placement"),
    ("OpenROAD.IOPlacement", "floorplan"),
    ("Odb.CustomIOPlacement", "floorplan"),
    ("Odb.ApplyDEFTemplate", "floorplan"),
    ("OpenROAD.GlobalPlacement", "placement"),
    ("Odb.WriteVerilogHeader", "placement"),
    ("Checker.PowerGridViolations", "placement"),
    ("OpenROAD.STAMidPNR", "placement"),
    ("OpenROAD.RepairDesignPostGPL", "placement"),
    ("Odb.ManualGlobalPlacement", "placement"),
    ("OpenROAD.DetailedPlacement", "placement"),
    ("OpenROAD.CTS", "cts"),
    ("OpenROAD.STAMidPNR-1", "cts"),
    ("OpenROAD.ResizerTimingPostCTS", "cts"),
    ("OpenROAD.STAMidPNR-2", "cts"),
    ("OpenROAD.GlobalRouting", "routing"),
    ("OpenROAD.CheckAntennas", "routing"),
    ("OpenROAD.RepairDesignPostGRT", "routing"),
    ("Odb.DiodesOnPorts", "routing"),
    ("Odb.HeuristicDiodeInsertion", "routing"),
    ("OpenROAD.RepairAntennas", "routing"),
    ("OpenROAD.ResizerTimingPostGRT", "routing"),
    ("OpenROAD.STAMidPNR-3", "routing"),
    ("OpenROAD.DetailedRouting", "routing"),
    ("Odb.RemoveRoutingObstructions", "routing"),
    ("OpenROAD.CheckAntennas-1", "routing"),
    ("Checker.TrDRC", "routing"),
    ("Odb.ReportDisconnectedPins", "routing"),
    ("Checker.DisconnectedPins", "routing"),
    ("Odb.ReportWireLength", "routing"),
    ("Checker.WireLength", "routing"),
    ("OpenROAD.FillInsertion", "routing"),
    ("Odb.CellFrequencyTables", "routing"),
    ("OpenROAD.RCX", "routing"),
    ("OpenROAD.STAPostPNR", "signoff"),
    ("OpenROAD.IRDropReport", "signoff"),
    ("Magic.StreamOut", "signoff"),
    ("KLayout.StreamOut", "signoff"),
    ("KLayout.Render", "signoff"),
    ("Magic.WriteLEF", "signoff"),
    ("Odb.CheckDesignAntennaProperties", "signoff"),
    ("KLayout.XOR", "signoff"),
    ("Checker.XOR", "signoff"),
    ("Magic.DRC", "signoff"),
    ("KLayout.DRC", "signoff"),
    ("Checker.MagicDRC", "signoff"),
    ("Checker.KLayoutDRC", "signoff"),
    ("Magic.SpiceExtraction", "signoff"),
    ("Checker.IllegalOverlap", "signoff"),
    ("Netgen.LVS", "signoff"),
    ("Checker.LVS", "signoff"),
    ("Yosys.EQY", "signoff"),
    ("Checker.SetupViolations", "signoff"),
    ("Checker.HoldViolations", "signoff"),
    ("Checker.MaxSlewViolations", "signoff"),
    ("Checker.MaxCapViolations", "signoff"),
    ("Misc.ReportManufacturability", "signoff"),
)

CLASSIC_STEP_IDS: Final[tuple[str, ...]] = tuple(
    step_id for step_id, _ in CLASSIC_STEP_PHASES
)
CLASSIC_STEP_PHASE: Final[dict[str, str]] = dict(CLASSIC_STEP_PHASES)
CLASSIC_TERMINAL_STEP: Final[str] = CLASSIC_STEP_IDS[-1]
CLASSIC_REGISTRY_VERSION: Final[str] = "librelane-3.0.5-classic-v1"
LIBRELANE_VERSION: Final[str] = "3.0.5"
LIBRELANE_IMAGE: Final[str] = "ghcr.io/librelane/librelane:3.0.5"
_CLASSIC_CANONICAL: Final[dict[str, str]] = {
    step_id.lower(): step_id for step_id in CLASSIC_STEP_IDS
}


def canonical_classic_step(step_id: str) -> str:
    """Return the exact registered Classic Step ID or raise a useful error."""
    canonical = _CLASSIC_CANONICAL.get(step_id.lower())
    if canonical is None:
        raise ValueError(
            f"unknown LibreLane Classic Step ID: {step_id}; "
            "use `lane-agent steps` to list valid IDs"
        )
    return canonical


def validate_classic_range(
    frm: str | None, to: str | None
) -> tuple[str | None, str | None]:
    """Canonicalize a Classic range and reject an inverted interval."""
    canonical_from = canonical_classic_step(frm) if frm else None
    canonical_to = canonical_classic_step(to) if to else None
    if (
        canonical_from is not None
        and canonical_to is not None
        and CLASSIC_STEP_IDS.index(canonical_from) > CLASSIC_STEP_IDS.index(canonical_to)
    ):
        raise ValueError("--from must not occur after --to in the Classic flow")
    return canonical_from, canonical_to


def next_classic_step(step_id: str) -> str | None:
    canonical = canonical_classic_step(step_id)
    index = CLASSIC_STEP_IDS.index(canonical) + 1
    return CLASSIC_STEP_IDS[index] if index < len(CLASSIC_STEP_IDS) else None
