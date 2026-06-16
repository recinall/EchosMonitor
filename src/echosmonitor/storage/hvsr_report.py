"""HVSR report generation — PDF + raw data export (Stage D).

Persistence/export lives in ``storage/`` (rule 8). Given the GUI-facing
frozen :class:`~echosmonitor.core.hvsr.HvsrResult`, this module writes:

* a **PDF report** (via matplotlib's ``PdfPages``) — header, the H/V curve
  (pre + post rejection, f0 +/- sigma, the SESAME strips), the 3-channel
  PSD, the numeric results, the SESAME reliability(3)+clarity(6) verdicts,
  the processing settings, and the same-response assumption statement;
* **raw exports** — JSON (the full reproducible structure) and CSV (the
  curve table), enough to reproduce the plots and numbers outside the app.

matplotlib is a transitive dependency of ``hvsrpy`` (a core dep). It is used
HERE ONLY for the offline PDF — live plotting stays on pyqtgraph, so the
"no matplotlib for live" stack rule holds. The backend is forced to the
non-interactive ``Agg`` inside :func:`write_hvsr_pdf` so report generation
never needs a display.
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import textwrap
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from echosmonitor import __version__

# Monospace character budget per line on A4 portrait at 9 pt — long lines are
# wrapped to this so nothing runs past the right page margin.
_WRAP_WIDTH = 92

if TYPE_CHECKING:
    from pathlib import Path

    from echosmonitor.core.hvsr import HvsrResult
    from echosmonitor.core.hvsr_array import ArrayHvsrResult

# App version surfaced in the report header. Derived from the single source of
# truth (the git-tag version resolved once at package import, M7-A) rather than
# a hand-maintained literal — no per-call packaging-metadata read.
APP_VERSION = __version__


@dataclass(frozen=True, slots=True)
class ReportContext:
    """Host-supplied context the result itself does not carry.

    ``nslc_by_component`` is the Z/N/E NSLC map; ``period_label`` is the
    measurement period (live span or archive range) as a human string.
    """

    nslc_by_component: dict[str, str]
    period_label: str
    generated_at: str  # ISO timestamp, supplied by the caller (no Date.now here)


class HvsrExportError(ValueError):
    """Raised when there is nothing valid to export (no/zero-valid result)."""


def _require_exportable(result: HvsrResult | None) -> HvsrResult:
    if result is None:
        raise HvsrExportError("no HVSR result to export — run a measurement first")
    if result.n_windows_total == 0 or result.n_windows_valid == 0:
        raise HvsrExportError("no valid HVSR windows to export")
    return result


# ----------------------------------------------------------------------
# Structured (JSON / CSV) export
# ----------------------------------------------------------------------
def result_to_dict(result: HvsrResult, ctx: ReportContext) -> dict[str, object]:
    """A JSON-serialisable dict reproducing the H/V curve, f0 and settings."""
    return {
        "schema": "echosmonitor.hvsr/1",
        "app_version": APP_VERSION,
        "generated_at": ctx.generated_at,
        "device": result.device,
        "station_key": result.station_key,
        "nslc_by_component": dict(ctx.nslc_by_component),
        "provenance": result.provenance,
        "period_label": ctx.period_label,
        "t_start": str(result.t_start),
        "t_end": str(result.t_end),
        "n_windows_total": result.n_windows_total,
        "n_windows_valid": result.n_windows_valid,
        "f0_hz": _num(result.f0_hz),
        "f0_sigma": _num(result.f0_sigma),
        "a0": _num(result.a0),
        "site_period_s": _num(1.0 / result.f0_hz) if result.f0_hz > 0 else None,
        "same_response": result.same_response,
        "same_response_detail": result.same_response_detail,
        "settings": result.settings.model_dump(),
        "frequency_hz": [float(x) for x in result.frequency],
        "mean_curve": [_num(x) for x in result.mean_curve],
        "median_curve": [_num(x) for x in result.median_curve],
        "lognormal_sigma": [_num(x) for x in result.lognormal_sigma],
        "window_ids": list(result.window_ids),
        "window_curves": [[_num(x) for x in row] for row in result.window_curves],
        "auto_accept_mask": [bool(x) for x in result.auto_accept_mask],
        "manual_override_mask": [bool(x) for x in result.manual_override_mask],
        "effective_mask": [bool(x) for x in result.effective_mask],
        "reliability": [_crit(c) for c in result.reliability],
        "clarity": [_crit(c) for c in result.clarity],
        "reliability_passed": result.reliability_passed,
        "clarity_passed": result.clarity_passed,
    }


def export_hvsr_json(result: HvsrResult | None, path: Path, ctx: ReportContext) -> None:
    """Write the full reproducible result as JSON."""
    res = _require_exportable(result)
    payload = result_to_dict(res, ctx)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def export_hvsr_csv(result: HvsrResult | None, path: Path, ctx: ReportContext) -> None:
    """Write the curve table as CSV (scalars + settings in comment lines).

    Columns: frequency_hz, mean_hv, median_hv, lognormal_sigma, then one
    ``win_<id>`` column per window. The per-window accept states and the
    scalar results live in leading ``#`` comment lines so the file stays a
    single self-describing table.
    """
    res = _require_exportable(result)
    period = (1.0 / res.f0_hz) if res.f0_hz > 0 else float("nan")

    def _c(text: object) -> str:
        # Keep a free-form value on one comment line (a stray newline would
        # corrupt the single-table layout the CSV reader assumes).
        return str(text).replace("\n", " ").replace("\r", " ")

    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(f"# HVSR export — {_c(res.device)} {_c(res.station_key)} ({res.provenance})\n")
        fh.write(f"# generated_at: {_c(ctx.generated_at)}  app_version: {APP_VERSION}\n")
        fh.write(f"# period: {_c(ctx.period_label)}\n")
        fh.write(
            f"# f0_hz: {res.f0_hz:.6f}  f0_sigma: {res.f0_sigma:.6f}  site_period_s: {period:.6f}\n"
        )
        fh.write(
            f"# n_windows_valid: {res.n_windows_valid}  n_windows_total: {res.n_windows_total}\n"
        )
        fh.write(f"# settings: {json.dumps(res.settings.model_dump())}\n")
        fh.write(f"# same_response: {res.same_response} — {_c(res.same_response_detail)}\n")
        fh.write(f"# window_ids: {list(res.window_ids)}\n")
        fh.write(f"# effective_mask: {[bool(x) for x in res.effective_mask]}\n")
        rel = " ".join(f"{c.name}={'P' if c.passed else 'F'}" for c in res.reliability)
        cla = " ".join(f"{c.name}={'P' if c.passed else 'F'}" for c in res.clarity)
        fh.write(f"# sesame_reliability: {rel}\n")
        fh.write(f"# sesame_clarity: {cla}\n")
        writer = csv.writer(fh)
        header = ["frequency_hz", "mean_hv", "median_hv", "lognormal_sigma"]
        header += [f"win_{wid}" for wid in res.window_ids]
        writer.writerow(header)
        for i in range(res.frequency.shape[0]):
            row = [
                f"{res.frequency[i]:.6g}",
                _csv_num(res.mean_curve[i]),
                _csv_num(res.median_curve[i]),
                _csv_num(res.lognormal_sigma[i]),
            ]
            row += [_csv_num(res.window_curves[w][i]) for w in range(res.window_curves.shape[0])]
            writer.writerow(row)


# ----------------------------------------------------------------------
# PDF report — text content (pure, testable without rendering)
# ----------------------------------------------------------------------
def report_title(result: HvsrResult, ctx: ReportContext) -> str:
    """The report header. Emits the provenance suffix exactly ONCE.

    ``ctx.period_label`` must carry only the time span (no provenance) — the
    provenance is appended here, so a label that also embedded it produced the
    old ``(live) (live)`` duplication.
    """
    nslc = ", ".join(f"{c}={ctx.nslc_by_component.get(c, '?')}" for c in ("Z", "N", "E"))
    return (
        f"HVSR report — {result.device} / {result.station_key}\n"
        f"{nslc}\n"
        f"{ctx.period_label} ({result.provenance})"
    )


def numeric_report_lines(result: HvsrResult, ctx: ReportContext) -> list[str]:
    """The page-2 text block, with long lines wrapped to the page width."""
    res = result
    period = (1.0 / res.f0_hz) if res.f0_hz > 0 else float("nan")
    lines: list[str] = [
        "RESULTS",
        f"  f0 (lognormal median)   : {res.f0_hz:.4f} Hz",
        f"  f0 dispersion (sigma)   : {res.f0_sigma:.4f} Hz",
        f"  site period T0 = 1/f0   : {period:.4f} s",
        f"  H/V amplitude at f0     : {res.a0:.3f}",
        f"  windows valid / total   : {res.n_windows_valid} / {res.n_windows_total}",
        "",
        "SESAME (2004) RELIABILITY",
    ]
    for c in res.reliability:
        lines += _wrap_line(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}  ({c.detail})")
    lines.append(f"  => reliability {'PASSES' if res.reliability_passed else 'FAILS'}")
    lines += ["", "SESAME (2004) CLARITY"]
    for c in res.clarity:
        lines += _wrap_line(f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}  ({c.detail})")
    lines.append(f"  => clarity {'PASSES' if res.clarity_passed else 'FAILS'}")
    lines += ["", "PROCESSING SETTINGS (for reproducibility)"]
    for k, v in res.settings.model_dump().items():
        lines += _wrap_line(f"  {k:24}: {v}")
    lines += ["", "COUNTS vs PHYSICAL UNITS"]
    lines += _wrap_line(f"  {res.same_response_detail}")
    return lines


def _wrap_line(text: str) -> list[str]:
    """Wrap one logical line to the page width with a hanging indent."""
    indent = " " * (len(text) - len(text.lstrip()) + 2)
    return textwrap.wrap(
        text, width=_WRAP_WIDTH, subsequent_indent=indent, break_long_words=False
    ) or [""]


# ----------------------------------------------------------------------
# PDF report — rendering
# ----------------------------------------------------------------------
def write_hvsr_pdf(result: HvsrResult | None, path: Path, ctx: ReportContext) -> None:
    """Render the scientific PDF report (two pages: plots, then the numbers)."""
    res = _require_exportable(result)
    import matplotlib

    matplotlib.use("Agg")  # offline, no display
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    with PdfPages(str(path)) as pdf:
        _pdf_page_plots(plt, pdf, res, ctx)
        _pdf_page_numbers(plt, pdf, res, ctx)


def _pdf_page_plots(plt: Any, pdf: Any, res: HvsrResult, ctx: ReportContext) -> None:
    fig, (ax_hv, ax_psd) = plt.subplots(2, 1, figsize=(8.27, 11.69))  # A4 portrait
    fig.suptitle(report_title(res, ctx), fontsize=11)

    freq = res.frequency
    mask = freq > 0
    f = freq[mask]
    # Per-window faint curves.
    for i in range(res.window_curves.shape[0]):
        ax_hv.plot(f, res.window_curves[i][mask], color="0.7", lw=0.4, alpha=0.5)
    # Pre-rejection mean (all windows) vs post (valid only).
    log_all = np.log(np.maximum(res.window_curves, 1e-12))
    pre_mean = np.exp(np.mean(log_all, axis=0))
    ax_hv.plot(f, pre_mean[mask], color="#d08a00", lw=1.2, ls="--", label="mean (pre-rejection)")
    mean, sigma = res.mean_curve, res.lognormal_sigma
    if np.any(np.isfinite(mean)):
        ax_hv.fill_between(
            f,
            (mean * np.exp(-sigma))[mask],
            (mean * np.exp(sigma))[mask],
            color="#3aa3ff",
            alpha=0.2,
        )
        ax_hv.plot(f, mean[mask], color="#1f6fd0", lw=2.0, label="mean (post-rejection)")
    # SESAME unreliable low-frequency strip.
    f_unreliable = res.settings.min_reliable_frequency_hz()
    if f_unreliable > f[0]:
        ax_hv.axvspan(f[0], f_unreliable, color="#e0526b", alpha=0.12)
    # f0 marker + dispersion strip.
    if np.isfinite(res.f0_hz) and res.f0_hz > 0:
        ax_hv.axvspan(
            max(res.f0_hz - res.f0_sigma, f[0]), res.f0_hz + res.f0_sigma, color="0.5", alpha=0.2
        )
        ax_hv.axvline(res.f0_hz, color="#c0203a", ls="--", lw=1.5, label=f"f0 = {res.f0_hz:.3f} Hz")
    ax_hv.set_xscale("log")
    ax_hv.set_xlabel("Frequency (Hz)")
    ax_hv.set_ylabel("H/V amplitude")
    ax_hv.grid(True, which="both", alpha=0.25)
    ax_hv.legend(fontsize=8)

    for comp, (freqs, db), color in (
        ("Z", res.psd_z, "#1f6fd0"),
        ("N", res.psd_n, "#2e8b2e"),
        ("E", res.psd_e, "#d08a00"),
    ):
        if freqs.size == 0:
            continue
        m = freqs > 0
        ax_psd.plot(freqs[m], db[m], color=color, lw=1.0, label=comp)
    ax_psd.set_xscale("log")
    ax_psd.set_xlabel("Frequency (Hz)")
    ax_psd.set_ylabel("PSD (dB rel. counts^2/Hz)")
    ax_psd.grid(True, which="both", alpha=0.25)
    ax_psd.legend(fontsize=8)

    fig.tight_layout(rect=(0, 0, 1, 0.94))
    pdf.savefig(fig)
    plt.close(fig)


def _pdf_page_numbers(plt: Any, pdf: Any, res: HvsrResult, ctx: ReportContext) -> None:
    fig = plt.figure(figsize=(8.27, 11.69))
    lines = numeric_report_lines(res, ctx)
    # Deliberate framed layout: a title anchored at the top, the monospace
    # block below it, and a footer anchored at the bottom — so the page reads
    # as intentional whitespace, not a truncated cut-off.
    fig.text(
        0.5,
        0.965,
        "HVSR report — numeric results",
        ha="center",
        va="top",
        fontsize=13,
        weight="bold",
    )
    fig.add_artist(plt.Line2D([0.07, 0.93], [0.94, 0.94], color="0.6", lw=0.8))
    fig.text(
        0.07,
        0.915,
        "\n".join(lines),
        ha="left",
        va="top",
        family="monospace",
        fontsize=9,
        linespacing=1.35,
    )
    fig.add_artist(plt.Line2D([0.07, 0.93], [0.045, 0.045], color="0.6", lw=0.8))
    fig.text(
        0.07,
        0.03,
        f"Generated {ctx.generated_at} by EchosMonitor v{APP_VERSION}",
        ha="left",
        va="bottom",
        family="monospace",
        fontsize=8,
        color="0.35",
    )
    pdf.savefig(fig)
    plt.close(fig)


# ----------------------------------------------------------------------
# Multi-station (array) report — M5-C
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ArrayReportContext:
    """Host-supplied context for the array report.

    ``group_by_device`` maps each device to its Z/N/E NSLCs (the widget's
    start-time selection); per-station pages derive their single-station
    :class:`ReportContext` from it.
    """

    group_by_device: dict[str, dict[str, str]]
    period_label: str
    generated_at: str


_A0_ARRAY_NOTE = (
    "A0 comparison ACROSS stations is response-sensitive (H/V cancels the "
    "instrument response per station only); f0 comparison is not."
)


def _require_array_exportable(result: ArrayHvsrResult | None) -> ArrayHvsrResult:
    if result is None:
        raise HvsrExportError("no array HVSR result to export — run a measurement first")
    if not any(r.n_windows_valid > 0 for r in result.results.values()):
        raise HvsrExportError("no station with valid HVSR windows to export")
    return result


def _station_ctx(ctx: ArrayReportContext, device: str) -> ReportContext:
    return ReportContext(
        nslc_by_component=dict(ctx.group_by_device.get(device, {})),
        period_label=ctx.period_label,
        generated_at=ctx.generated_at,
    )


def array_result_to_dict(result: ArrayHvsrResult, ctx: ArrayReportContext) -> dict[str, object]:
    """A JSON-serialisable dict: comparison scalars + geometry + full stations.

    Each station with a result embeds its complete single-station structure
    (:func:`result_to_dict`, schema ``echosmonitor.hvsr/1``) so the array
    file is a superset — anything reproducible from a single-station export
    is reproducible per station from this one.
    """
    geometry = result.geometry
    return {
        "schema": "echosmonitor.hvsr-array/1",
        "app_version": APP_VERSION,
        "generated_at": ctx.generated_at,
        "period_label": ctx.period_label,
        "provenance": result.provenance,
        "settings": result.settings.model_dump(),
        "devices": list(result.devices),
        "errors": dict(result.errors),
        "a0_note": _A0_ARRAY_NOTE,
        "geometry": {
            "positions": {
                device: {
                    "latitude": position.latitude,
                    "longitude": position.longitude,
                    "elevation_m": position.elevation_m,
                    "source": position.source,
                }
                for device, position in geometry.positions.items()
            },
            "distances_m": {
                f"{a}|{b}": round(meters, 3) for (a, b), meters in geometry.distances_m.items()
            },
            "unpositioned": list(result.unpositioned()),
        },
        "stations": {
            device: result_to_dict(result.results[device], _station_ctx(ctx, device))
            for device in result.devices
            if device in result.results
        },
    }


def export_hvsr_array_json(
    result: ArrayHvsrResult | None, path: Path, ctx: ArrayReportContext
) -> None:
    """Write the full reproducible array result as JSON.

    Atomic (the M3-C exports pattern): temp file in the same dir →
    fsync → ``os.replace``, so a mid-write failure never leaves a
    truncated file at the user's destination.
    """
    res = _require_array_exportable(result)
    payload = array_result_to_dict(res, ctx)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def array_comparison_lines(result: ArrayHvsrResult, ctx: ArrayReportContext) -> list[str]:
    """The comparison-page text block (pure, testable without rendering)."""
    del ctx  # context rides the page header/footer, not this block
    lines: list[str] = ["STATIONS"]
    for device in result.devices:
        r = result.results.get(device)
        if r is None:
            error = result.errors.get(device, "")
            note = f"compute failed — {error}" if error else "no result (not enough windows)"
            lines += _wrap_line(f"  {device:16}: {note}")
            continue
        if r.n_windows_valid == 0:
            # A present result whose windows were ALL rejected: its f0/A0
            # are honest NaN — say "no valid windows" instead of printing
            # nan into a scientific report.
            lines += _wrap_line(
                f"  {device:16}: no valid windows "
                f"(all {r.n_windows_total} rejected) — no f0 to report"
            )
            continue
        period = (1.0 / r.f0_hz) if r.f0_hz > 0 else float("nan")
        rel = "PASS" if r.reliability_passed else "FAIL"
        cla = "PASS" if r.clarity_passed else "FAIL"
        lines += _wrap_line(
            f"  {device:16}: f0 {r.f0_hz:.4f} +/- {r.f0_sigma:.4f} Hz   "
            f"T0 {period:.4f} s   A0 {r.a0:.3f}   "
            f"windows {r.n_windows_valid}/{r.n_windows_total}   "
            f"SESAME rel {rel} / clar {cla}"
        )
        lines += _wrap_line(f"  {'':16}  response: {r.same_response_detail}")
    lines += ["", "A0 ACROSS STATIONS"]
    lines += _wrap_line(f"  {_A0_ARRAY_NOTE}")

    geometry = result.geometry
    lines += ["", "GEOMETRY"]
    if not geometry.positions:
        lines.append("  no positioned stations")
    for device in geometry.devices:
        position = geometry.positions[device]
        lines += _wrap_line(
            f"  {device:16}: lat {position.latitude:.6f}  lon {position.longitude:.6f}  "
            f"elev {position.elevation_m:.1f} m  (source: {position.source})"
        )
    unpositioned = result.unpositioned()
    if unpositioned:
        lines += _wrap_line("  no position: " + ", ".join(unpositioned))
    if result.provenance == "archive":
        # Rule 16 honesty: there is no archived position source — the
        # snapshot is resolved at RUN time, which can differ from where a
        # device stood during the recording.
        lines += _wrap_line(
            "  note: positions were resolved when this analysis ran, not when "
            "the data was recorded — a device moved since the recording shows "
            "its CURRENT coordinates."
        )
    if geometry.distances_m:
        lines += ["", "INTER-STATION DISTANCES"]
        for (a, b), meters in sorted(geometry.distances_m.items(), key=lambda kv: kv[1]):
            lines += _wrap_line(f"  {a} - {b}: {meters:.1f} m")
    return lines


def write_hvsr_array_pdf(
    result: ArrayHvsrResult | None, path: Path, ctx: ArrayReportContext
) -> None:
    """Render the array PDF: one comparison page, then per-station pages.

    Station order is the measurement's start order. Stations without a
    valid result appear on the comparison page (with their error / no-data
    note) but get no per-station pages — there is nothing honest to plot.
    """
    res = _require_array_exportable(result)
    import matplotlib

    matplotlib.use("Agg")  # offline, no display
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    # Atomic: the array PDF has 1+2N pages of render surface — a mid-render
    # failure must not leave a truncated PDF at the destination (rule 8).
    tmp = path.with_name(path.name + ".tmp")
    try:
        with PdfPages(str(tmp)) as pdf:
            _pdf_page_array_comparison(plt, pdf, res, ctx)
            for device in res.devices:
                r = res.results.get(device)
                if r is None or r.n_windows_valid == 0:
                    continue
                station_ctx = _station_ctx(ctx, device)
                _pdf_page_plots(plt, pdf, r, station_ctx)
                _pdf_page_numbers(plt, pdf, r, station_ctx)
        # Reopen writable ("rb+", not "rb"): matplotlib wrote+closed the PDF,
        # and on Windows os.fsync (FlushFileBuffers) raises EBADF on a
        # read-only fd. "rb+" gives a writable handle without truncating.
        with tmp.open("rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _pdf_page_array_comparison(
    plt: Any, pdf: Any, res: ArrayHvsrResult, ctx: ArrayReportContext
) -> None:
    fig = plt.figure(figsize=(8.27, 11.69))  # A4 portrait
    n_valid = sum(
        1
        for d in res.devices
        if (r := res.results.get(d)) is not None and r.n_windows_valid > 0
    )
    fig.text(
        0.5,
        0.965,
        f"HVSR array report — {len(res.devices)} stations "
        f"({n_valid} with valid results)\n{ctx.period_label} ({res.provenance})",
        ha="center",
        va="top",
        fontsize=12,
        weight="bold",
    )
    # The N-curve overlay: per-station mean curves, NEVER a cross-station
    # average (skill hvsr-array — that quantity is not defined).
    ax = fig.add_axes((0.1, 0.58, 0.84, 0.32))
    for device in res.devices:
        r = res.results.get(device)
        if r is None or r.n_windows_valid == 0:
            continue  # an all-NaN curve would only fake a legend entry
        mask = r.frequency > 0
        line = ax.plot(r.frequency[mask], r.mean_curve[mask], lw=1.6, label=device)
        if np.isfinite(r.f0_hz) and r.f0_hz > 0:
            ax.axvline(r.f0_hz, color=line[0].get_color(), ls=":", lw=0.9, alpha=0.7)
    ax.set_xscale("log")
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("H/V amplitude")
    ax.grid(True, which="both", alpha=0.25)
    ax.legend(fontsize=8)

    fig.text(
        0.07,
        0.52,
        "\n".join(array_comparison_lines(res, ctx)),
        ha="left",
        va="top",
        family="monospace",
        fontsize=8,
        linespacing=1.3,
    )
    fig.add_artist(plt.Line2D([0.07, 0.93], [0.045, 0.045], color="0.6", lw=0.8))
    fig.text(
        0.07,
        0.03,
        f"Generated {ctx.generated_at} by EchosMonitor v{APP_VERSION}",
        ha="left",
        va="bottom",
        family="monospace",
        fontsize=8,
        color="0.35",
    )
    pdf.savefig(fig)
    plt.close(fig)


# ----------------------------------------------------------------------
def _num(x: object) -> float | None:
    v = float(x)  # type: ignore[arg-type]
    return None if not np.isfinite(v) else v


def _csv_num(x: object) -> str:
    v = float(x)  # type: ignore[arg-type]
    return "" if not np.isfinite(v) else f"{v:.6g}"


def _crit(c: object) -> dict[str, object]:
    return {"name": c.name, "passed": bool(c.passed), "detail": c.detail}  # type: ignore[attr-defined]
