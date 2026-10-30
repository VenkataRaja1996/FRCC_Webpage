#!/usr/bin/env python3
"""Generate testcase-wise report for Robot Framework suites.

The script groups script-level Robot tests (for example, TC_..._0005, TC_..._0006)
under testcase IDs extracted from [Documentation] lines (for example,
TC_FRCCVT-12346).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


TEST_NAME_RE = re.compile(r"^(TC_[^\s]+)\s*$")
DOC_RE = re.compile(r"^\s*\[Documentation\]\s+(.*)\s*$")
CASE_ID_RE = re.compile(r"TC_FRCCVT-\d+|FRCCVT-\d+", re.IGNORECASE)


@dataclass
class ScriptMeta:
    test_name: str
    case_ids: list[str]
    documentation: str
    suite_path: Path
    line_no: int


@dataclass
class ScriptResult:
    status: str
    message: str
    elapsed: str
    test_id: str
    start: str


@dataclass
class RunMeta:
    generator: str
    generated: str
    rpa: str
    schema_version: str
    suite_count: int
    suite_names: list[str]
    total_pass: int = 0
    total_fail: int = 0
    total_skip: int = 0


@dataclass
class ReferenceMeta:
    build_number: str = "-"
    display_size: str = "-"
    variant: str = "-"
    vehicle_program: str = "-"


@dataclass
class SuiteStat:
    name: str
    full_name: str
    suite_id: str
    total: int
    passed: int
    failed: int
    skipped: int
    elapsed: str


def normalize_case_id(case_id: str) -> str:
    token = case_id.upper()
    if token.startswith("FRCCVT-"):
        return "TC_" + token
    return token


def parse_suite_metadata(suite_path: Path) -> tuple[list[ScriptMeta], dict[str, list[str]]]:
    scripts: list[ScriptMeta] = []
    by_case: dict[str, list[str]] = defaultdict(list)
    current_test: str | None = None
    current_test_line = 0

    for index, line in enumerate(suite_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        test_match = TEST_NAME_RE.match(line)
        if test_match:
            current_test = test_match.group(1)
            current_test_line = index
            continue

        doc_match = DOC_RE.match(line)
        if doc_match and current_test:
            documentation = doc_match.group(1).strip()
            raw_ids = CASE_ID_RE.findall(documentation)
            case_ids = [normalize_case_id(item) for item in raw_ids]
            if not case_ids:
                continue

            scripts.append(
                ScriptMeta(
                    test_name=current_test,
                    case_ids=case_ids,
                    documentation=documentation,
                    suite_path=suite_path,
                    line_no=current_test_line,
                )
            )
            for case_id in case_ids:
                if current_test not in by_case[case_id]:
                    by_case[case_id].append(current_test)

    return scripts, by_case


def load_suite_paths(suite_input_path: Path) -> list[Path]:
    if suite_input_path.suffix.lower() == ".robot":
        return [suite_input_path]

    if suite_input_path.suffix.lower() == ".txt":
        paths: list[Path] = []
        for raw in suite_input_path.read_text(encoding="utf-8", errors="replace").splitlines():
            item = raw.strip().strip('"')
            if not item:
                continue
            candidate = Path(item)
            if not candidate.exists():
                continue
            if candidate.suffix.lower() == ".robot":
                paths.append(candidate)
        return paths

    raise ValueError("suite input must be a .robot file or .txt list of .robot files")


def parse_multiple_suites(suite_paths: list[Path]) -> tuple[dict[str, list[str]], dict[str, ScriptMeta]]:
    combined: dict[str, list[str]] = defaultdict(list)
    meta_by_test: dict[str, ScriptMeta] = {}
    for suite_path in suite_paths:
        scripts, by_case = parse_suite_metadata(suite_path)
        for script in scripts:
            if script.test_name not in meta_by_test:
                meta_by_test[script.test_name] = script
        for case_id, tests in by_case.items():
            for test_name in tests:
                if test_name not in combined[case_id]:
                    combined[case_id].append(test_name)
    return combined, meta_by_test


def parse_output_results(output_xml: Path) -> dict[str, ScriptResult]:
    results: dict[str, ScriptResult] = {}

    try:
        tree = ET.parse(output_xml)
        root = tree.getroot()

        for test_elem in root.iter("test"):
            name = test_elem.attrib.get("name", "").strip()
            if not name:
                continue

            status_elem = test_elem.find("status")
            if status_elem is None:
                continue

            status = status_elem.attrib.get("status", "NOT RUN").upper()
            message = (status_elem.text or "").strip()
            elapsed = status_elem.attrib.get("elapsed", "")
            test_id = test_elem.attrib.get("id", "")
            start = status_elem.attrib.get("start", "")
            results[name] = ScriptResult(status=status, message=message, elapsed=elapsed, test_id=test_id, start=start)

        return results
    except ET.ParseError:
        # Fallback for partially written/corrupted output.xml.
        text = output_xml.read_text(encoding="utf-8", errors="replace")
        test_block_re = re.compile(r"<test\b([^>]*)>(.*?)</test>", re.DOTALL)
        status_re = re.compile(
            r"<status\b[^>]*status=\"([^\"]+)\"[^>]*?(?:elapsed=\"([^\"]*)\")?[^>]*>(.*?)</status>",
            re.DOTALL,
        )

        for match in test_block_re.finditer(text):
            attrs = match.group(1)
            block = match.group(2)
            name_match = re.search(r'\bname="([^"]+)"', attrs)
            if not name_match:
                continue
            name = name_match.group(1).strip()
            id_match = re.search(r'\bid="([^"]+)"', attrs)
            status_match = status_re.search(block)
            if not status_match:
                continue

            status = (status_match.group(1) or "NOT RUN").upper()
            elapsed = status_match.group(2) or ""
            message = re.sub(r"\s+", " ", (status_match.group(3) or "").strip())
            test_id = id_match.group(1) if id_match else ""
            start_match = re.search(r'<status\b[^>]*start="([^"]+)"', block)
            start = start_match.group(1) if start_match else ""
            results[name] = ScriptResult(status=status, message=message, elapsed=elapsed, test_id=test_id, start=start)

    return results


def parse_run_metadata(output_xml: Path) -> RunMeta:
    try:
        tree = ET.parse(output_xml)
        root = tree.getroot()
        generator = root.attrib.get("generator", "")
        generated = root.attrib.get("generated", "")
        rpa = root.attrib.get("rpa", "")
        schema_version = root.attrib.get("schemaversion", "")
        top_suites = root.findall("suite")
        suite_names = [suite.attrib.get("name", "") for suite in top_suites if suite.attrib.get("name")]
        total_stat = root.find("statistics/total/stat")
        total_pass = int(total_stat.attrib.get("pass", "0")) if total_stat is not None else 0
        total_fail = int(total_stat.attrib.get("fail", "0")) if total_stat is not None else 0
        total_skip = int(total_stat.attrib.get("skip", "0")) if total_stat is not None else 0
        return RunMeta(
            generator=generator,
            generated=generated,
            rpa=rpa,
            schema_version=schema_version,
            suite_count=len(top_suites),
            suite_names=suite_names,
            total_pass=total_pass,
            total_fail=total_fail,
            total_skip=total_skip,
        )
    except ET.ParseError:
        text = output_xml.read_text(encoding="utf-8", errors="replace")
        header_match = re.search(r"<robot\b([^>]*)>", text)
        attrs = header_match.group(1) if header_match else ""

        def attr_value(key: str) -> str:
            match = re.search(rf'\b{key}="([^"]*)"', attrs)
            return match.group(1) if match else ""

        suite_names = re.findall(r'<suite\b[^>]*\bname="([^"]+)"[^>]*>', text)
        total_match = re.search(r"<total>\s*<stat\b[^>]*pass=\"(\d+)\"[^>]*fail=\"(\d+)\"[^>]*skip=\"(\d+)\"", text)
        return RunMeta(
            generator=attr_value("generator"),
            generated=attr_value("generated"),
            rpa=attr_value("rpa"),
            schema_version=attr_value("schemaversion"),
            suite_count=len(suite_names),
            suite_names=suite_names[:20],
            total_pass=int(total_match.group(1)) if total_match else 0,
            total_fail=int(total_match.group(2)) if total_match else 0,
            total_skip=int(total_match.group(3)) if total_match else 0,
        )


def format_elapsed_seconds(value: str) -> str:
    if not value:
        return "-"
    try:
        total_seconds = max(0, int(round(float(value))))
    except ValueError:
        return value
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_suite_statistics(output_xml: Path) -> list[SuiteStat]:
    try:
        tree = ET.parse(output_xml)
        root = tree.getroot()
        elapsed_by_id: dict[str, str] = {}
        for suite_elem in root.iter("suite"):
            suite_id = suite_elem.attrib.get("id", "")
            if not suite_id:
                continue
            status_elem = suite_elem.find("status")
            elapsed_by_id[suite_id] = format_elapsed_seconds(status_elem.attrib.get("elapsed", "") if status_elem is not None else "")

        suite_stats: list[SuiteStat] = []
        for stat_elem in root.findall("statistics/suite/stat"):
            suite_id = stat_elem.attrib.get("id", "")
            passed = int(stat_elem.attrib.get("pass", "0"))
            failed = int(stat_elem.attrib.get("fail", "0"))
            skipped = int(stat_elem.attrib.get("skip", "0"))
            suite_stats.append(
                SuiteStat(
                    name=stat_elem.attrib.get("name", ""),
                    full_name=(stat_elem.text or stat_elem.attrib.get("name", "")).strip(),
                    suite_id=suite_id,
                    total=passed + failed + skipped,
                    passed=passed,
                    failed=failed,
                    skipped=skipped,
                    elapsed=elapsed_by_id.get(suite_id, "-"),
                )
            )
        return suite_stats
    except ET.ParseError:
        text = output_xml.read_text(encoding="utf-8", errors="replace")
        stats: list[SuiteStat] = []
        for match in re.finditer(
            r'<stat\b[^>]*name="([^"]+)"[^>]*id="([^"]+)"[^>]*pass="(\d+)"[^>]*fail="(\d+)"[^>]*skip="(\d+)"[^>]*>(.*?)</stat>',
            text,
            re.DOTALL,
        ):
            name, suite_id, passed, failed, skipped, full_name = match.groups()
            passed_int = int(passed)
            failed_int = int(failed)
            skipped_int = int(skipped)
            stats.append(
                SuiteStat(
                    name=name,
                    full_name=re.sub(r"\s+", " ", full_name).strip() or name,
                    suite_id=suite_id,
                    total=passed_int + failed_int + skipped_int,
                    passed=passed_int,
                    failed=failed_int,
                    skipped=skipped_int,
                    elapsed="-",
                )
            )
        return stats


def _extract_ref_field(text: str, label: str) -> str:
    pattern = rf'"\*{re.escape(label)}","\*<p>(.*?)\\x3c/p>"'
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return "-"
    value = match.group(1)
    value = value.replace("\\x3c", "<").replace("\\x3e", ">")
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip() or "-"


def parse_reference_metadata(report_html: Path) -> ReferenceMeta:
    if not report_html.exists():
        return ReferenceMeta()
    text = report_html.read_text(encoding="utf-8", errors="replace")
    return ReferenceMeta(
        build_number=_extract_ref_field(text, "Build Number"),
        display_size=_extract_ref_field(text, "Display Size"),
        variant=_extract_ref_field(text, "Variant"),
        vehicle_program=_extract_ref_field(text, "Vehicle Program"),
    )


def case_sort_key(case_id: str) -> int:
    digits = re.findall(r"\d+", case_id)
    return int(digits[-1]) if digits else 0


def summarize_case(statuses: list[str]) -> str:
    if not statuses:
        return "NOT RUN"
    if any(status == "FAIL" for status in statuses):
        return "FAIL"
    filtered = [status for status in statuses if status != "SKIP"]
    if not filtered:
        return "NOT RUN"
    if all(status == "PASS" for status in filtered):
        return "PASS"
    if any(status == "PASS" for status in filtered):
        return "PARTIAL"
    return "NOT RUN"


def make_case_anchor(case_id: str) -> str:
    return "case-" + re.sub(r"[^a-zA-Z0-9]+", "-", case_id.strip().lower()).strip("-")


def relative_href(from_path: Path, to_path: Path) -> str:
    try:
        return str(to_path.resolve().relative_to(from_path.resolve().parent)).replace("\\", "/")
    except ValueError:
        return str(to_path.resolve()).replace("\\", "/")


def screenshot_slug(test_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", test_name).strip("._")[:80] or "test"
    digest = hashlib.sha1(test_name.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"


def resource_root(output_dir: Path, resource_name: str) -> Path | None:
    for ancestor in (output_dir, *output_dir.parents):
        for candidate in (
            ancestor / "gmtools_rtostestautomation" / "gmtools_rtos_resources" / "resources" / resource_name,
            ancestor / "gmtools_rtos_resources" / "resources" / resource_name,
        ):
            if candidate.is_dir():
                return candidate.resolve()
    return None


def result_screenshots(output_dir: Path, test_name: str) -> list[Path]:
    result_roots = []
    for ancestor in (output_dir, *output_dir.parents):
        result_roots.extend((
            ancestor / "gmtools_rtostestautomation" / "gmtools_rtos_resources" / "resources" / "results",
            ancestor / "gmtools_rtos_resources" / "resources" / "results",
        ))

    run_dirs = {
        path.resolve()
        for root in result_roots
        if root.is_dir()
        for path in root.iterdir()
        if path.is_dir()
    }
    prefixes = (f"Failed_{test_name}_", f"{test_name}_")
    for run_dir in sorted(run_dirs, key=lambda path: path.stat().st_mtime_ns, reverse=True):
        for prefix in prefixes:
            images = sorted(
                path for path in run_dir.iterdir()
                if path.is_file() and path.name.startswith(prefix) and path.suffix.lower() == ".png"
            )
            if images:
                return [images[0]]
    return []


def legacy_result_screenshots(output_dir: Path, test_name: str) -> list[Path]:
    slug = screenshot_slug(test_name)
    local_dir = output_dir / "screenshots" / slug
    local_images = sorted(path for path in local_dir.glob("*.png") if path.is_file()) if local_dir.is_dir() else []
    if local_images:
        return local_images[:1]
    return sorted(
        path for screenshot_dir in (output_dir.parent / "screenshots" / slug, *output_dir.glob(f"*/screenshots/{slug}"))
        if screenshot_dir.is_dir()
        for path in screenshot_dir.glob("*.png")
        if path.is_file()
    )[:1]


def test_screenshots(output_dir: Path, test_name: str) -> list[Path]:
    manifest_paths = [output_dir / "failure-screenshots.json", *output_dir.glob("*/failure-screenshots.json")]
    resource_names = {"comparison": "results"}
    for manifest_path in manifest_paths:
        if not manifest_path.is_file():
            continue
        try:
            records = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for record in records:
            if record.get("test_name") != test_name or not record.get("image_source"):
                continue
            image_kind = record.get("image_kind", "comparison")
            root = resource_root(output_dir, resource_names.get(image_kind, ""))
            if not root:
                continue
            image = (root / record["image_source"]).resolve()
            try:
                image.relative_to(root)
            except ValueError:
                continue
            if image.is_file():
                return [image]

    result_images = result_screenshots(output_dir, test_name)
    if result_images:
        return result_images
    return legacy_result_screenshots(output_dir, test_name)


def test_reference_screenshot(output_dir: Path, test_name: str) -> Path | None:
    slug = screenshot_slug(test_name)
    candidates = [output_dir / "screenshots" / slug / "reference.png"]
    candidates.append(output_dir.parent / "screenshots" / slug / "reference.png")
    candidates.extend(output_dir.glob(f"*/screenshots/{slug}/reference.png"))
    legacy_reference = next((path for path in candidates if path.is_file()), None)
    if legacy_reference:
        return legacy_reference

    reference_root = resource_root(output_dir, "references")
    if not reference_root:
        return None
    manifest_paths = [output_dir / "failure-screenshots.json", *output_dir.glob("*/failure-screenshots.json")]
    for manifest_path in manifest_paths:
        if not manifest_path.is_file():
            continue
        try:
            records = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for record in records:
            if record.get("test_name") == test_name and record.get("reference_source"):
                path = (reference_root / record["reference_source"]).resolve()
                try:
                    path.relative_to(reference_root)
                except ValueError:
                    continue
                if path.is_file():
                    return path
    return None


def report_image_href(output_dir: Path, html_output_path: Path, image: Path) -> str:
    resolved = image.resolve()
    for kind, resource_name in (("comparison", "results"), ("capture", "screenshot_comparison"), ("reference", "references")):
        root = resource_root(output_dir, resource_name)
        if not root:
            continue
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            continue
        return Path(os.path.relpath(resolved, html_output_path.parent.resolve())).as_posix()
    return relative_href(html_output_path, resolved)


def apply_screenshot_failure_statuses(output_dir: Path, results: dict[str, ScriptResult]) -> None:
    manifest_path = output_dir / "failure-screenshots.json"
    if not manifest_path.is_file():
        return
    try:
        records = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    for record in records:
        test_name = record.get("test_name", "")
        result = results.get(test_name)
        if not result or result.status == "FAIL":
            continue
        keyword = record.get("keyword", "screenshot comparison")
        results[test_name] = ScriptResult(
            status="FAIL",
            message=f"Screenshot mismatch: {keyword}",
            elapsed=result.elapsed,
            test_id=result.test_id,
            start=result.start,
        )


def comparison_screenshots_html(output_dir: Path, html_output_path: Path, test_name: str) -> str:
    images = [(image, "Failed screenshot") for image in test_screenshots(output_dir, test_name)]
    reference = test_reference_screenshot(output_dir, test_name)
    if reference:
        images.append((reference, "Reference image"))
    if not images:
        return "-"

    figures = []
    for image, label in images:
        image_href = html.escape(report_image_href(output_dir, html_output_path, image))
        figures.append(
            f'<figure><figcaption>{label}</figcaption>'
            f'<a class="failure-screenshot" href="{image_href}" target="_blank" rel="noopener">'
            f'<img src="{image_href}" alt="{label} for {html.escape(test_name)}" loading="lazy"></a></figure>'
        )
    return f'<div class="failure-image-pair">{"".join(figures)}</div>'


def parse_iso_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def format_generated_display(value: str) -> str:
    parsed = parse_iso_datetime(value)
    if not parsed:
        return value or "N/A"
    return parsed.strftime("%Y%m%d %H:%M:%S") + " UTC+05:30"


def format_relative_age(value: str, now: dt.datetime) -> str:
    parsed = parse_iso_datetime(value)
    if not parsed:
        return ""

    delta = now - parsed
    if delta.total_seconds() < 0:
        return "just now"

    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 1:
        return "just now"

    hours, minutes = divmod(total_minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if not parts:
        parts.append("0 minutes")
    return " ".join(parts) + " ago"


def suite_display_name_from_path(suite_path: Path) -> str:
    # Convert robot file stem to the same human-readable suite name style used in reports.
    return re.sub(r"\s+", " ", suite_path.stem.replace("_", " ")).strip()


def build_markdown(
    source_label: str,
    output_xml: Path,
    by_case: dict[str, list[str]],
    results: dict[str, ScriptResult],
    run_meta: RunMeta,
) -> str:
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = []
    lines.append("# Testcase-Wise Robot Report")
    lines.append("")
    lines.append(f"Source suite: {source_label}")
    lines.append(f"Output xml: {output_xml}")
    lines.append(f"Generated at: {generated}")
    lines.append("")

    lines.append("## Run Metadata (From output.xml)")
    lines.append("")
    lines.append(f"- Robot generator: {run_meta.generator or '-'}")
    lines.append(f"- Run generated: {run_meta.generated or '-'}")
    lines.append(f"- RPA mode: {run_meta.rpa or '-'}")
    lines.append(f"- Schema version: {run_meta.schema_version or '-'}")
    lines.append(f"- Top-level suites: {run_meta.suite_count}")
    if run_meta.suite_names:
        lines.append(f"- Suite names: {', '.join(run_meta.suite_names[:10])}")
    lines.append("")

    total_cases = len(by_case)
    case_statuses: list[str] = []
    for case_id, test_names in by_case.items():
        statuses = [results.get(name, ScriptResult("NOT RUN", "", "", "", "")).status for name in test_names]
        case_statuses.append(summarize_case(statuses))

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total testcase IDs: {total_cases}")
    lines.append(f"- PASS: {sum(1 for item in case_statuses if item == 'PASS')}")
    lines.append(f"- FAIL: {sum(1 for item in case_statuses if item == 'FAIL')}")
    lines.append(f"- PARTIAL: {sum(1 for item in case_statuses if item == 'PARTIAL')}")
    lines.append(f"- SKIP: {sum(1 for item in case_statuses if item == 'NOT RUN')}")
    lines.append("")

    lines.append("## Grouped By Testcase ID")
    lines.append("")
    for case_id in sorted(by_case.keys(), key=case_sort_key):
        test_names = by_case[case_id]
        statuses = [results.get(name, ScriptResult("NOT RUN", "", "", "", "")).status for name in test_names]
        overall = summarize_case(statuses)
        lines.append(f"### {case_id} ({overall})")
        for name in test_names:
            result = results.get(name)
            if result:
                elapsed = f", elapsed={result.elapsed}" if result.elapsed else ""
                lines.append(f"- {name}: {result.status}{elapsed}")
                if result.status == "FAIL" and result.message:
                    lines.append(f"  Failure: {result.message}")
            else:
                lines.append(f"- {name}: SKIP")
        lines.append("")

    return "\n".join(lines)


def build_html(
    source_label: str,
    output_xml: Path,
    by_case: dict[str, list[str]],
    results: dict[str, ScriptResult],
    run_meta: RunMeta,
    suite_stats: list[SuiteStat],
    ref_meta: ReferenceMeta,
    meta_by_test: dict[str, ScriptMeta],
    html_output_path: Path,
) -> str:
    now = dt.datetime.now()
    generated_source = run_meta.generated or now.isoformat()
    generated_display = format_generated_display(generated_source)
    generated_age = format_relative_age(generated_source, now)
    total_cases = len(by_case)
    all_names: list[str] = []
    for names in by_case.values():
        for item in names:
            if item not in all_names:
                all_names.append(item)

    scoped_results = [results[name] for name in all_names if name in results]
    script_total = len(scoped_results)
    script_pass = sum(1 for item in scoped_results if item.status == "PASS")
    script_fail = sum(1 for item in scoped_results if item.status == "FAIL")
    script_skip = sum(1 for item in scoped_results if item.status == "SKIP")

    case_statuses: list[str] = []
    for _, test_names in by_case.items():
        statuses = [results.get(name, ScriptResult("NOT RUN", "", "", "", "")).status for name in test_names]
        case_statuses.append(summarize_case(statuses))
    case_pass = sum(1 for item in case_statuses if item == "PASS")
    case_fail = sum(1 for item in case_statuses if item == "FAIL")
    case_partial = sum(1 for item in case_statuses if item == "PARTIAL")
    case_not_run = sum(1 for item in case_statuses if item == "NOT RUN")

    suite_case_statuses: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for case_id, test_names in by_case.items():
        for test_name in test_names:
            meta = meta_by_test.get(test_name)
            if not meta:
                continue
            suite_name = suite_display_name_from_path(meta.suite_path)
            status = results.get(test_name, ScriptResult("NOT RUN", "", "", "", "")).status
            suite_case_statuses[suite_name][case_id].append(status)

    suite_case_counts: dict[str, tuple[int, int, int, int]] = {}
    for suite_name, case_map in suite_case_statuses.items():
        summarized = [summarize_case(statuses) for statuses in case_map.values()]
        suite_case_counts[suite_name] = (
            sum(1 for item in summarized if item == "PASS"),
            sum(1 for item in summarized if item == "FAIL"),
            sum(1 for item in summarized if item == "PARTIAL"),
            sum(1 for item in summarized if item == "NOT RUN"),
        )

    starts = [item.start for item in scoped_results if item.start]
    start_time = min(starts) if starts else (run_meta.generated or "N/A")
    end_time = run_meta.generated or "N/A"

    if case_fail > 0 or case_partial > 0:
        status_text = (
            f"{case_fail} testcases failed, {case_partial} partial, "
            f"{case_pass} passed, {case_not_run} skipped"
        )
        status_class = "fail"
    elif case_pass > 0 and case_not_run > 0:
        status_text = f"{case_pass} testcases passed, {case_not_run} skipped"
        status_class = "pass"
    elif case_pass > 0:
        status_text = f"All {case_pass} testcases passed"
        status_class = "pass"
    elif run_meta.total_fail > 0:
        status_text = (
            f"Underlying run failed: {run_meta.total_fail} scripts failed, "
            f"{run_meta.total_pass} passed, {run_meta.total_skip} skipped"
        )
        status_class = "fail"
    elif run_meta.total_pass > 0:
        status_text = (
            f"Underlying run passed: {run_meta.total_pass} scripts passed, "
            f"{run_meta.total_skip} skipped"
        )
        status_class = "pass"
    else:
        status_text = "No testcase was executed"
        status_class = "skip"

    style = """
body { font-family: Helvetica, Arial, sans-serif; font-size: 0.85em; margin: 0; color: #000; }
body.pass { background: #9be89b; }
body.fail { background: #f2a3a3; }
body.skip { background: #f3e4a2; }
.container { margin: 10px 14px; }
h1 { margin: 0; }
h2 { margin: 14px 0 8px 0; }
#header { display: flex; justify-content: space-between; align-items: flex-start; }
#generated { text-align: right; font-size: 0.95em; }
a { color: #000; text-decoration: none; }
a:hover { text-decoration: underline; }
table { border-collapse: collapse; font-size: 1em; width: 65em; background: #fff; }
th, td { border: 1px solid #ccc; padding: 0.2em 0.4em; vertical-align: top; }
th { background: #ddd; text-align: left; }
.details { width: 65em; }
.summary-row th { width: 140px; }
.pass { color: #000; font-weight: 700; }
.fail { color: #000; font-weight: 700; }
.partial { color: #000; font-weight: 700; }
.skip { color: #000; font-weight: 700; }
.label { display: inline-block; border-radius: 3px; padding: 1px 6px; font-size: 0.78em; font-weight: 700; letter-spacing: 0.03em; }
.label.pass { background: #97bd61; color: #000; }
.label.fail { background: #ce3e01; color: #000; }
.label.partial { background: #d28b54; color: #000; }
.label.skip { background: #fed84f; color: #000; }
.graph { height: 12px; width: 150px; border: 1px solid #bbb; background: #f5f5f5; display: inline-flex; }
.g-pass { background: #97bd61; }
.g-fail { background: #ce3e01; }
.g-partial { background: #d28b54; }
.g-skip { background: #fed84f; }
#test-details { width: 100%; }
#test-details th, #test-details td { border: 1px solid #ccc; }
#test-script-failures { width: 100%; }
.details-col-name { width: 26%; }
.details-col-doc { width: 34%; }
.details-col-status { width: 8%; text-align: center; }
.details-col-msg { width: 18%; }
.details-col-elapsed { width: 7%; text-align: center; }
.details-col-times { width: 15%; text-align: center; }
.details-col-screenshot { width: 12em; }
.failure-screenshot { display: inline-block; margin: 2px; }
.failure-screenshot img { max-width: 600px; max-height: 450px; object-fit: contain; border: 1px solid #bbb; }
.failure-image-pair { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-start; }
.failure-image-pair figure { margin: 0; }
.failure-image-pair figcaption { font-weight: 700; }
.case-anchor { background: #f4f4f4; }
.suite-name { font-weight: 700; }
.suite-full-name { font-size: 0.88em; }
"""

    output_dir = output_xml.parent
    log_href = relative_href(html_output_path, output_dir / "log.html")
    report_href = relative_href(html_output_path, output_dir / "report.html")
    xml_href = relative_href(html_output_path, output_xml)

    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append("<html lang=\"en\"><head><meta charset=\"utf-8\">")
    parts.append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
    parts.append("<title>Testcase-Wise Robot Report</title>")
    parts.append(f"<style>{style}</style></head><body class=\"{status_class}\"><div class=\"container\">")
    parts.append("<div id=\"header\">")
    parts.append(f"<h1>{html.escape((run_meta.suite_names[0] if run_meta.suite_names else 'Robot') + ' Report')}</h1>")
    parts.append("<div id=\"generated\"><div>Generated</div>")
    parts.append(f"<div>{html.escape(generated_display)}</div>")
    if generated_age:
        parts.append(f"<div>{html.escape(generated_age)}</div>")
    parts.append("</div>")
    parts.append("</div>")

    parts.append("<h2>Summary Information</h2>")
    parts.append('<table class="details">')
    parts.append("<tr class=\"summary-row\"><th>Status:</th><td>")
    parts.append(f"<span class=\"{status_class}\">{html.escape(status_text)}</span></td></tr>")
    parts.append(f"<tr class=\"summary-row\"><th>Build Number:</th><td>{html.escape(ref_meta.build_number)}</td></tr>")
    parts.append(f"<tr class=\"summary-row\"><th>Display Size:</th><td>{html.escape(ref_meta.display_size)}</td></tr>")
    parts.append(f"<tr class=\"summary-row\"><th>Variant:</th><td>{html.escape(ref_meta.variant)}</td></tr>")
    parts.append(f"<tr class=\"summary-row\"><th>Vehicle Program:</th><td>{html.escape(ref_meta.vehicle_program)}</td></tr>")
    parts.append(f"<tr class=\"summary-row\"><th>Start Time:</th><td>{html.escape(start_time)}</td></tr>")
    parts.append(f"<tr class=\"summary-row\"><th>End Time:</th><td>{html.escape(end_time)}</td></tr>")
    parts.append(f"<tr class=\"summary-row\"><th>Elapsed Time:</th><td>{html.escape(run_meta.generated or 'N/A')}</td></tr>")
    parts.append(
        f"<tr class=\"summary-row\"><th>Log File:</th><td><a href=\"{html.escape(log_href)}\">log.html</a> | "
        f"<a href=\"{html.escape(report_href)}\">report.html</a> | <a href=\"{html.escape(xml_href)}\">output.xml</a></td></tr>"
    )
    parts.append("</table>")

    parts.append("<h2>Test Statistics</h2>")
    parts.append('<table class="details">')
    parts.append("<tr><th>Total Statistics</th><th>Total</th><th>Pass</th><th>Fail</th><th>Partial</th><th>Skip</th><th>Pass / Fail / Partial / Skip</th></tr>")
    total_bar = total_cases if total_cases else 1
    pass_w = int((case_pass / total_bar) * 150)
    fail_w = int((case_fail / total_bar) * 150)
    partial_w = int((case_partial / total_bar) * 150)
    skip_w = max(0, 150 - pass_w - fail_w - partial_w)
    parts.append(
        "<tr><td>All Testcases</td>"
        f"<td>{total_cases}</td><td>{case_pass}</td><td>{case_fail}</td><td>{case_partial}</td><td>{case_not_run}</td>"
        f"<td><span class=\"graph\"><span class=\"g-pass\" style=\"width:{pass_w}px\"></span>"
        f"<span class=\"g-fail\" style=\"width:{fail_w}px\"></span>"
        f"<span class=\"g-partial\" style=\"width:{partial_w}px\"></span>"
        f"<span class=\"g-skip\" style=\"width:{skip_w}px\"></span></span></td></tr>"
    )
    parts.append("</table>")

    if suite_stats:
        parts.append("<h2>Statistics by Suite</h2>")
        parts.append('<table class="details">')
        parts.append("<tr><th>Statistics by Suite</th><th>Total</th><th>Pass</th><th>Fail</th><th>Partial</th><th>Skip</th><th>Elapsed</th><th>Pass / Fail / Partial / Skip</th></tr>")
        for suite_stat in suite_stats:
            if "-" not in suite_stat.suite_id:
                passed, failed, partial, skipped = case_pass, case_fail, case_partial, case_not_run
            else:
                passed, failed, partial, skipped = suite_case_counts.get(
                    suite_stat.name,
                    (suite_stat.passed, suite_stat.failed, 0, suite_stat.skipped),
                )

            total = passed + failed + partial + skipped
            total_bar = total if total else 1
            pass_w = int((passed / total_bar) * 150)
            fail_w = int((failed / total_bar) * 150)
            partial_w = int((partial / total_bar) * 150)
            skip_w = max(0, 150 - pass_w - fail_w - partial_w)
            parts.append("<tr>")
            parts.append(
                f"<td><div class=\"suite-name\">{html.escape(suite_stat.name)}</div>"
                f"<div class=\"suite-full-name\">{html.escape(suite_stat.full_name)}</div></td>"
            )
            parts.append(f"<td>{total}</td>")
            parts.append(f"<td>{passed}</td>")
            parts.append(f"<td>{failed}</td>")
            parts.append(f"<td>{partial}</td>")
            parts.append(f"<td>{skipped}</td>")
            parts.append(f"<td>{html.escape(suite_stat.elapsed)}</td>")
            parts.append(
                f"<td><span class=\"graph\"><span class=\"g-pass\" style=\"width:{pass_w}px\"></span>"
                f"<span class=\"g-fail\" style=\"width:{fail_w}px\"></span>"
                f"<span class=\"g-partial\" style=\"width:{partial_w}px\"></span>"
                f"<span class=\"g-skip\" style=\"width:{skip_w}px\"></span></span></td>"
            )
            parts.append("</tr>")
        parts.append("</table>")

    failed_scripts = [(name, result) for name, result in results.items() if result.status == "FAIL"]
    if failed_scripts:
        parts.append("<h2>Test Script Failures</h2>")
        parts.append('<table id="test-script-failures"><thead><tr><th>Test Script</th><th>Failure</th><th>Failure Screenshots</th></tr></thead><tbody>')
        for name, result in failed_scripts:
            if result.test_id:
                name_cell = f'<a href="{html.escape(log_href)}#{html.escape(result.test_id)}">{html.escape(name)}</a>'
            else:
                name_cell = html.escape(name)
            screenshot_html = comparison_screenshots_html(output_dir, html_output_path, name)
            parts.append(
                f"<tr><td>{name_cell}</td><td>{html.escape(result.message or '')}</td>"
                f"<td>{screenshot_html}</td></tr>"
            )
        parts.append("</tbody></table>")

    parts.append('<table id="test-details">')
    parts.append(
        "<thead><tr><th class=\"details-col-name\">Name</th><th class=\"details-col-doc\">Documentation</th>"
        "<th>Tags</th><th class=\"details-col-status\">Status</th><th class=\"details-col-msg\">Message</th>"
        "<th class=\"details-col-elapsed\">Elapsed</th><th class=\"details-col-times\">Start / End</th>"
        "<th class=\"details-col-screenshot\">Failure Screenshot</th></tr></thead><tbody>"
    )

    for case_id in sorted(by_case.keys(), key=case_sort_key):
        parts.append(f"<tr class=\"case-anchor\"><td colspan=\"8\"><a id=\"{html.escape(make_case_anchor(case_id))}\"></a><strong>{html.escape(case_id)}</strong></td></tr>")
        test_names = by_case[case_id]
        statuses = [results.get(name, ScriptResult("NOT RUN", "", "", "", "")).status for name in test_names]
        overall = summarize_case(statuses)
        for name in test_names:
            meta = meta_by_test.get(name)
            result = results.get(name)
            if result and result.test_id:
                name_cell = f'<a href="{html.escape(log_href)}#{html.escape(result.test_id)}">{html.escape(name)}</a>'
            else:
                name_cell = html.escape(name)

            if result:
                status_class_map = {"PASS": "pass", "FAIL": "fail", "SKIP": "skip", "NOT RUN": "skip"}
                label_class = status_class_map.get(result.status, "skip")
                doc_text = html.escape(meta.documentation) if meta else "-"
                msg = html.escape(result.message or "")
                elapsed = html.escape(result.elapsed or "-")
                start = html.escape(result.start or "-")
                end = html.escape(end_time)
                screenshot_html = "-"
                if result.status == "FAIL":
                    screenshot_html = comparison_screenshots_html(output_dir, html_output_path, name)
                parts.append("<tr>")
                parts.append(f"<td>{name_cell}</td>")
                parts.append(f"<td>{doc_text}</td>")
                parts.append(f"<td>{html.escape(case_id)}</td>")
                parts.append(f"<td><span class=\"label {label_class}\">{html.escape('SKIP' if result.status == 'NOT RUN' else result.status)}</span></td>")
                parts.append(f"<td>{msg}</td>")
                parts.append(f"<td>{elapsed}</td>")
                parts.append(f"<td>{start} / {end}</td>")
                parts.append(f"<td>{screenshot_html}</td>")
                parts.append("</tr>")
            else:
                doc_text = html.escape(meta.documentation) if meta else "-"
                parts.append("<tr>")
                parts.append(f"<td>{name_cell}</td>")
                parts.append(f"<td>{doc_text}</td>")
                parts.append(f"<td>{html.escape(case_id)}</td>")
                parts.append("<td><span class=\"label skip\">SKIP</span></td>")
                parts.append("<td>-</td>")
                parts.append("<td>-</td>")
                parts.append("<td>- / -</td>")
                parts.append("<td>-</td>")
                parts.append("</tr>")
    parts.append("</tbody></table>")

    parts.append("</div></body></html>")
    return "".join(parts)


def default_output_path(suite_path: Path) -> Path:
    return suite_path.with_name(f"{suite_path.stem}_TESTCASE_WISE_REPORT.md")


def default_html_output_path(suite_path: Path) -> Path:
    return suite_path.with_name(f"{suite_path.stem}_TESTCASE_WISE_REPORT.html")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate testcase-wise report from Robot suite and output.xml")
    parser.add_argument("suite", type=Path, help="Path to a .robot file or .txt suite list")
    parser.add_argument("outputxml", type=Path, help="Path to Robot output.xml")
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output report file path")
    parser.add_argument("--html-output", type=Path, default=None, help="Optional HTML report output path")
    parser.add_argument("--html-only", action="store_true", help="Generate HTML without writing a Markdown report")
    args = parser.parse_args()

    suite_input_path = args.suite.resolve()
    output_xml = args.outputxml.resolve()
    report_path = args.output.resolve() if args.output else default_output_path(suite_input_path)
    html_report_path = args.html_output.resolve() if args.html_output else default_html_output_path(suite_input_path)

    if not suite_input_path.exists():
        raise FileNotFoundError(f"Suite input not found: {suite_input_path}")
    if not output_xml.exists():
        raise FileNotFoundError(f"output.xml not found: {output_xml}")

    suite_paths = load_suite_paths(suite_input_path)
    if not suite_paths:
        raise RuntimeError("No valid .robot suite files found in suite input")

    by_case, meta_by_test = parse_multiple_suites(suite_paths)
    source_label = str(suite_paths[0]) if len(suite_paths) == 1 else f"{len(suite_paths)} suites from {suite_input_path}"
    results = parse_output_results(output_xml)
    apply_screenshot_failure_statuses(output_xml.parent, results)
    run_meta = parse_run_metadata(output_xml)
    suite_stats = parse_suite_statistics(output_xml)
    ref_meta = parse_reference_metadata(output_xml.parent / "report.html")
    if not args.html_only:
        md_report = build_markdown(source_label, output_xml, by_case, results, run_meta)
        report_path.write_text(md_report, encoding="utf-8")
    html_report = build_html(source_label, output_xml, by_case, results, run_meta, suite_stats, ref_meta, meta_by_test, html_report_path)
    html_report_path.write_text(html_report, encoding="utf-8")

    if not args.html_only:
        print(f"Report generated: {report_path}")
    print(f"HTML report generated: {html_report_path}")
    print(f"Total testcase IDs: {len(by_case)}")


if __name__ == "__main__":
    main()
