"""Robot Framework runner that guarantees report generation even after Ctrl+C."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote


def find_robot_inputs(robot_args: list[str]) -> list[str]:
    """Find robot files or directories passed to Robot, preserving order."""
    inputs = []
    seen = set()
    for value in robot_args:
        path = Path(value.strip('"'))
        if not path.exists() or (not path.is_file() and not path.is_dir()):
            continue
        if not path.is_dir() and path.suffix.lower() != ".robot":
            continue
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            inputs.append(resolved)
    return inputs


def disable_signals_before_run(script_path: Path, robot_args: list[str]) -> None:
    robot_inputs = find_robot_inputs(robot_args)
    if not robot_inputs:
        print("[runner] No .robot input found; skipping signal disable step.")
        return

    print("[runner] Disabling test-script signals before starting Robot...")
    result = subprocess.run([sys.executable, str(script_path), *robot_inputs])
    if result.returncode != 0:
        raise SystemExit(
            f"[runner] Signal disable step failed with exit code {result.returncode}. "
            "Robot will not be started."
        )


def read_variant(root_dir: Path) -> str:
    config_path = root_dir / "gmtools_rtostestautomation" / "RTOSAutomation" / "src" / "rtos" / "rtos_configuration.py"
    if not config_path.exists():
        config_path = root_dir / "RTOSAutomation" / "src" / "rtos" / "rtos_configuration.py"
    if not config_path.exists():
        return "UNKNOWN"

    for line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^\s*vehicle_program\s*=\s*['\"]([^'\"]+)['\"]", line)
        if match:
            return match.group(1).split("_", 1)[0] or "UNKNOWN"
    return "UNKNOWN"


def ensure_robot_reports(output_xml: Path, output_dir: Path) -> None:
    log_path = output_dir / "log.html"
    report_path = output_dir / "report.html"
    if log_path.exists() and report_path.exists():
        return

    print("[runner] Generating missing Robot log/report from output.xml...")
    subprocess.run([
        sys.executable,
        "-m",
        "robot.rebot",
        "--output",
        "NONE",
        "--log",
        str(log_path.resolve()),
        "--report",
        str(report_path.resolve()),
        str(output_xml.resolve()),
    ])


def artifact_root(report_path: Path, kind: str) -> Path | None:
    resource_name = {"comparison": "results", "reference": "references", "capture": "screenshot_comparison"}.get(kind)
    if not resource_name:
        return None
    for ancestor in (report_path.parent, *report_path.parent.parents):
        for candidate in (
            ancestor / "gmtools_rtostestautomation" / "gmtools_rtos_resources" / "resources" / resource_name,
            ancestor / "gmtools_rtos_resources" / "resources" / resource_name,
        ):
            if candidate.is_dir():
                return candidate.resolve()
    return None


def comparison_result_image(report_path: Path, test_name: str) -> Path | None:
    result_root = artifact_root(report_path, "comparison")
    if not result_root:
        return None
    run_dirs = sorted((path for path in result_root.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for run_dir in run_dirs:
        for prefix in (f"Failed_{test_name}_", f"{test_name}_"):
            matches = sorted(
                path for path in run_dir.iterdir()
                if path.is_file() and path.name.startswith(prefix) and path.suffix.lower() == ".png"
            )
            if matches:
                return matches[0]
    return None


def rewrite_failure_artifact_links(log_path: Path, manifest_paths: list[Path]) -> None:
    if not log_path.is_file():
        return
    page = log_path.read_text(encoding="utf-8", errors="replace")
    original_page = page
    for manifest_path in manifest_paths:
        if not manifest_path.is_file():
            continue
        try:
            records = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for record in records:
            image_kind = record.get("image_kind", "comparison")
            if image_kind == "capture" and record.get("image_source"):
                capture_root = artifact_root(log_path, "capture")
                capture_source = (capture_root / record["image_source"]).resolve() if capture_root else None
                result_image = comparison_result_image(log_path, record.get("test_name", ""))
                if capture_source and result_image:
                    try:
                        capture_source.relative_to(capture_root)
                    except ValueError:
                        continue
                    replacement = quote(Path(os.path.relpath(result_image, log_path.parent.resolve())).as_posix(), safe="/.")
                    relative_capture = record["image_source"].replace("\\", "/")
                    capture_uri_pattern = re.compile(
                        r'file:///[^\s"\'<>]*?/gmtools_rtos_resources/resources/screenshot_comparison/'
                        + re.escape(relative_capture),
                        re.IGNORECASE,
                    )
                    page = capture_uri_pattern.sub(replacement, page)
                    page = page.replace(
                        f"/artifacts/capture/{quote(record['image_source'], safe='/')}",
                        replacement,
                    )
                continue
            for source_key, kind in (("image_source", record.get("image_kind", "comparison")), ("reference_source", "reference")):
                source = record.get(source_key)
                root = artifact_root(log_path, kind) if source else None
                if not source or not root:
                    continue
                source_path = (root / source).resolve()
                try:
                    source_path.relative_to(root)
                except ValueError:
                    continue
                if not source_path.is_file():
                    continue
                old_url = f"/artifacts/{kind}/{quote(source, safe='/')}"
                relative_url = Path(os.path.relpath(source_path, log_path.parent.resolve())).as_posix()
                replacement = quote(relative_url, safe="/.")
                page = page.replace(old_url, replacement)
                page = page.replace(source_path.as_uri(), replacement)
    if page != original_page:
        log_path.write_text(page, encoding="utf-8")


def embed_failure_screenshots(report_path: Path, manifest_paths: list[Path]) -> None:
    if not report_path.is_file():
        return
    rewrite_failure_artifact_links(report_path.parent / "log.html", manifest_paths)
    page = report_path.read_text(encoding="utf-8", errors="replace")
    section_match = re.search(r'<section id="failure-screenshots".*?</section>', page, re.DOTALL)
    if section_match:
        page = page[:section_match.start()] + page[section_match.end():]

    rows = []
    seen = set()
    for manifest_path in manifest_paths:
        if not manifest_path.is_file():
            continue
        try:
            records = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for record in records:
            image_kind = record.get("image_kind", "comparison")
            image_source = record.get("image_source")
            if image_kind == "capture":
                image_path = comparison_result_image(report_path, record.get("test_name", ""))
                image_root = artifact_root(report_path, "comparison")
            else:
                image_root = artifact_root(report_path, image_kind) if image_source else None
                image_path = (
                    (image_root / image_source).resolve()
                    if image_root and image_source
                    else (manifest_path.parent / record.get("image", "")).resolve()
                )
            if image_path is None:
                continue
            if not image_path.is_file():
                continue
            if image_source and image_root:
                try:
                    image_path.relative_to(image_root)
                    image_href = Path(os.path.relpath(image_path, report_path.parent.resolve())).as_posix()
                except ValueError:
                    continue
            else:
                image_href = Path(os.path.relpath(image_path, report_path.parent.resolve())).as_posix()
            reference_source = record.get("reference_source")
            reference_root = artifact_root(report_path, "reference") if reference_source else None
            reference_path = (
                (reference_root / reference_source).resolve()
                if reference_root and reference_source
                else (manifest_path.parent / record.get("reference_image", "")).resolve()
            )
            reference_href = (
                Path(os.path.relpath(reference_path, report_path.parent.resolve())).as_posix()
                if reference_source and reference_root and reference_path.is_file()
                else Path(os.path.relpath(reference_path, report_path.parent.resolve())).as_posix()
                if record.get("reference_image") and reference_path.is_file()
                else None
            )
            key = (record.get("test_full_name"), record.get("keyword"), image_href)
            if key in seen:
                continue
            seen.add(key)
            comparison_images = (
                f'<figure style="margin:0"><figcaption>Failed screenshot</figcaption>'
                f'<a href="{html.escape(image_href, quote=True)}" target="_blank" rel="noopener">'
                f'<img src="{html.escape(image_href, quote=True)}" alt="Failed screenshot" '
                'style="max-width:600px;max-height:450px;height:auto;object-fit:contain"></a></figure>'
            )
            if reference_href:
                escaped_reference = html.escape(reference_href, quote=True)
                comparison_images += (
                    f'<figure style="margin:0"><figcaption>Reference image</figcaption>'
                    f'<a href="{escaped_reference}" target="_blank" rel="noopener">'
                    f'<img src="{escaped_reference}" alt="Reference image" '
                    'style="max-width:600px;max-height:450px;height:auto;object-fit:contain"></a></figure>'
                )
            rows.append(
                "<tr>"
                f"<td>{html.escape(record.get('test_full_name', record.get('test_name', '')))}</td>"
                f"<td>{html.escape(record.get('keyword', ''))}</td>"
                f'<td><div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start">{comparison_images}</div></td>'
                "</tr>"
            )
    if not rows:
        if section_match:
            report_path.write_text(page, encoding="utf-8")
        return

    section = (
        '<section id="failure-screenshots" style="margin:24px 0">'
        "<h2>Failure Screenshots</h2>"
        '<table style="width:100%;border-collapse:collapse">'
        "<thead><tr><th>Test</th><th>Failed keyword</th><th>Failed / Reference</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )
    anchor = '<div id="test-details-container"></div>'
    if anchor in page:
        page = page.replace(anchor, section + anchor, 1)
    else:
        body_end = page.lower().rfind("</body>")
        if body_end < 0:
            return
        page = page[:body_end] + section + page[body_end:]
    report_path.write_text(page, encoding="utf-8")


def robot_args_without_outputs(robot_args: list[str], suite_paths: list[Path]) -> list[str]:
    suite_values = {str(path.resolve()).lower() for path in suite_paths}
    filtered: list[str] = []
    skip_next = False
    output_options = {"--outputdir", "--output", "--log", "--report"}
    for value in robot_args:
        if skip_next:
            skip_next = False
            continue
        if value in output_options:
            skip_next = True
            continue
        try:
            if str(Path(value.strip('"')).resolve()).lower() in suite_values:
                continue
        except (OSError, ValueError):
            pass
        filtered.append(value)
    return filtered


def generate_casewise_report(
    report_script: Path,
    suite_input: Path,
    output_xml: Path,
    markdown_path: Path,
    html_path: Path,
    html_only: bool,
) -> None:
    command = [sys.executable, str(report_script), str(suite_input), str(output_xml)]
    if not html_only:
        command.extend(["--output", str(markdown_path)])
    command.extend(["--html-output", str(html_path)])
    if html_only:
        command.append("--html-only")
    subprocess.run(command)


def run_bulk_sequentially(
    root_dir: Path,
    output_dir: Path,
    report_script: Path,
    robot_args: list[str],
    suite_paths: list[Path],
    html_only: bool,
) -> int:
    base_args = robot_args_without_outputs(robot_args, suite_paths)
    completed_outputs: list[Path] = []
    exit_code = 0
    variant = read_variant(root_dir)

    for suite_path in suite_paths:
        suite_output_dir = output_dir / f"{suite_path.stem}_{variant}"
        suite_output_dir.mkdir(parents=True, exist_ok=True)
        suite_xml = suite_output_dir / "output.xml"
        print(f"\n[runner] Starting {suite_path.name}")
        command = [
            sys.executable,
            "-m",
            "robot",
            "--pythonpath",
            str(root_dir),
            "--prerunmodifier",
            "robot_startup_modifier.RobotStartupModifier",
            *base_args,
            "--outputdir",
            str(suite_output_dir.resolve()),
            "--output",
            str(suite_xml.resolve()),
            "--log",
            str((suite_output_dir / "log.html").resolve()),
            "--report",
            str((suite_output_dir / "report.html").resolve()),
            str(suite_path),
        ]
        result = subprocess.run(command)
        exit_code = max(exit_code, result.returncode)
        if not suite_xml.exists():
            print(f"[runner] No output.xml was produced for {suite_path.name}; continuing.")
            continue

        ensure_robot_reports(suite_xml, suite_output_dir)
        embed_failure_screenshots(
            suite_output_dir / "report.html",
            [suite_output_dir / "failure-screenshots.json"],
        )
        completed_outputs.append(suite_xml)
        generate_casewise_report(
            report_script,
            suite_path,
            suite_xml,
            suite_output_dir / "TESTCASE_WISE_REPORT.md",
            suite_output_dir / "TESTCASE_WISE_REPORT.html",
            html_only,
        )
        print(f"[runner] Individual reports ready: {suite_output_dir}")

    if completed_outputs:
        subprocess.run([
            sys.executable,
            "-m",
            "robot.rebot",
            "--output",
            str((output_dir / "output.xml").resolve()),
            "--log",
            str((output_dir / "log.html").resolve()),
            "--report",
            str((output_dir / "report.html").resolve()),
            *[str(path.resolve()) for path in completed_outputs],
        ])
        manifests = [output_dir / "failure-screenshots.json"]
        manifests.extend(output_dir.glob("*/failure-screenshots.json"))
        embed_failure_screenshots(output_dir / "report.html", manifests)
    return exit_code


def generate_individual_reports(
    root_dir: Path,
    output_dir: Path,
    output_xml: Path,
    report_script: Path,
    suite_paths: list[Path],
    html_only: bool,
) -> None:
    variant = read_variant(root_dir)
    for suite_path in suite_paths:
        suite_name = suite_path.stem
        robot_suite_name = suite_name.replace("_", " ")
        suite_output_dir = output_dir / f"{suite_name}_{variant}"
        suite_output_dir.mkdir(parents=True, exist_ok=True)
        suite_xml = suite_output_dir / "output.xml"
        filtered_xml = suite_output_dir / ".filtered_output.xml"
        print(f"Generating individual report for {suite_name} ({variant})...")

        tree = ET.parse(output_xml)
        root = tree.getroot()
        root_suite = root.find("suite")
        target_suite = None
        if root_suite is not None:
            target_source = suite_path.resolve().as_posix().lower()
            for candidate in root_suite.iter("suite"):
                source = candidate.attrib.get("source", "")
                if source and Path(source).resolve().as_posix().lower() == target_source:
                    target_suite = candidate
                    break
            if target_suite is None:
                for candidate in root_suite.iter("suite"):
                    if candidate.attrib.get("name") == robot_suite_name:
                        target_suite = candidate
                        break
            if target_suite is None:
                print(f"[runner] Suite source not found in combined output: {suite_path}")
                continue
            root_suite.attrib["name"] = robot_suite_name
            for child in list(root_suite):
                if child.tag == "suite" and child is not target_suite:
                    root_suite.remove(child)
            tree.write(filtered_xml, encoding="utf-8", xml_declaration=True)

        rebot_result = subprocess.run([
            sys.executable,
            "-m",
            "robot.rebot",
            "--name",
            robot_suite_name,
            "--outputdir",
            str(suite_output_dir.resolve()),
            "--output",
            str(suite_xml.resolve()),
            "--log",
            str((suite_output_dir / "log.html").resolve()),
            "--report",
            str((suite_output_dir / "report.html").resolve()),
            str(filtered_xml.resolve()),
        ])
        filtered_xml.unlink(missing_ok=True)
        if not suite_xml.exists():
            print(f"[runner] Could not create Robot report for {suite_name}.")
            continue

        embed_failure_screenshots(
            suite_output_dir / "report.html",
            [
                suite_output_dir / "failure-screenshots.json",
                output_dir / "failure-screenshots.json",
            ],
        )

        generate_casewise_report(
            report_script,
            suite_path,
            suite_xml,
            suite_output_dir / "TESTCASE_WISE_REPORT.md",
            suite_output_dir / "TESTCASE_WISE_REPORT.html",
            html_only,
        )


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--suite-list-txt", default=None)
    parser.add_argument("--single-robot", default=None)
    parser.add_argument("--report-script", required=True)
    parser.add_argument("--html-only", action="store_true", help="Generate HTML reports without Markdown files")
    parser.add_argument("--robot-args", nargs=argparse.REMAINDER, default=[])
    args = parser.parse_args()

    root_dir = Path(args.root_dir)
    output_dir = Path(args.output_dir)
    output_xml = output_dir / "output.xml"
    report_script = Path(args.report_script)
    signal_script = root_dir / "disable_testscript_signals.py"

    robot_cmd = [
        sys.executable, "-m", "robot",
        "--pythonpath", str(root_dir),
        "--prerunmodifier", "robot_startup_modifier.RobotStartupModifier",
    ] + args.robot_args

    if not signal_script.exists():
        raise SystemExit(f"[runner] Signal disabler not found: {signal_script}")
    disable_signals_before_run(signal_script, args.robot_args)

    robot_exit = 0
    interrupted = False
    try:
        if args.suite_list_txt:
            suite_paths = [
                Path(line.strip().strip('"'))
                for line in Path(args.suite_list_txt).read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
            ]
            robot_exit = run_bulk_sequentially(
                root_dir,
                output_dir,
                report_script,
                args.robot_args,
                suite_paths,
                args.html_only,
            )
            if robot_exit:
                print(f"[runner] Bulk execution completed with failures: {robot_exit}")
        else:
            proc = subprocess.run(robot_cmd)
            robot_exit = proc.returncode
    except KeyboardInterrupt:
        interrupted = True
        print("\n[runner] Ctrl+C detected — robot interrupted. Generating report from partial results...")

    # Always attempt report generation if output.xml exists
    if not report_script.exists():
        sys.exit(robot_exit)

    if not output_xml.exists():
        print("[runner] No output.xml found — skipping report generation.")
        sys.exit(robot_exit)

    try:
        ensure_robot_reports(output_xml, output_dir)
        manifests = [output_dir / "failure-screenshots.json"]
        manifests.extend(output_dir.glob("*/failure-screenshots.json"))
        embed_failure_screenshots(output_dir / "report.html", manifests)
        if args.single_robot:
            print(f"\nGenerating testcase-wise report for: {args.single_robot}")
            generate_casewise_report(
                report_script,
                Path(args.single_robot),
                output_xml,
                output_dir / "ALL_TESTCASE_WISE_REPORT.md",
                output_dir / "ALL_TESTCASE_WISE_REPORT.html",
                args.html_only,
            )
            generate_individual_reports(
                root_dir,
                output_dir,
                output_xml,
                report_script,
                [Path(args.single_robot)],
                args.html_only,
            )
        elif args.suite_list_txt:
            print("\nGenerating combined testcase-wise report...")
            generate_casewise_report(
                report_script,
                Path(args.suite_list_txt),
                output_xml,
                output_dir / "ALL_TESTCASE_WISE_REPORT.md",
                output_dir / "ALL_TESTCASE_WISE_REPORT.html",
                args.html_only,
            )
            suite_paths = [
                Path(line.strip().strip('"'))
                for line in Path(args.suite_list_txt).read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
            ]
            generate_individual_reports(
                root_dir,
                output_dir,
                output_xml,
                report_script,
                suite_paths,
                args.html_only,
            )
    except KeyboardInterrupt:
        print("[runner] Interrupted during report generation — partial report may exist.")

    if interrupted:
        print("[runner] Run was interrupted. Partial results saved.")
    sys.exit(robot_exit)


if __name__ == "__main__":
    main()
