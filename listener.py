"""
Robot Framework Listener for Continue On Failure
Automatically continues test execution when keywords fail
without modifying any test files
"""

import atexit
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

class ContinueOnFailureListener:
    """Listener to allow tests to continue despite keyword failures"""
    
    ROBOT_LIBRARY_LISTENER = True
    
    def __init__(self):
        self.ROBOT_LISTENER_API_VERSION = 2
        self._keyword_depth = 0
        self._suite_stack = []
        self._current_test_name = ""
        self._current_test_full_name = ""
        self._failure_screenshot_recorded = False
        self._failure_keyword_name = ""
        self._reference_image_name = ""
        self._failure_screenshots = []
        self._capturing_screenshot = False
        self._output_dir = None
        atexit.register(self._finalize_native_report)

    def _debug(self, message):
        print(f"__RF_DEBUG__{message}", flush=True)

    def _test_event(self, event, name, attrs):
        full_name = attrs.get("longname") or ".".join([*self._suite_stack, name])
        payload = {
            "event": event,
            "name": name,
            "full_name": full_name,
            "status": attrs.get("status", "RUNNING" if event == "start" else "UNKNOWN"),
        }
        if event == "start":
            payload["documentation"] = " ".join((attrs.get("doc") or "").split())
        else:
            start_time = attrs.get("starttime")
            end_time = attrs.get("endtime")
            if start_time and end_time:
                try:
                    payload["elapsed_seconds"] = (int(end_time) - int(start_time)) / 1000
                except (TypeError, ValueError):
                    pass
            payload["message"] = attrs.get("message", "")
        print(f"__RF_TEST__{json.dumps(payload, ensure_ascii=True)}", flush=True)

    def _screenshot_slug(self, name):
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")[:80] or "test"
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        return f"{slug}-{digest}"

    def _robot_variable(self, name):
        try:
            from robot.libraries.BuiltIn import BuiltIn

            return BuiltIn().get_variable_value(f"${{{name}}}")
        except Exception:
            return None

    def _failed_result_screenshot(self):
        result_path = self._robot_variable("RESULT_PATH")
        results_folder = self._robot_variable("results_folder")
        if not result_path or not results_folder or not self._current_test_name:
            return None

        result_dir = Path(result_path) / str(results_folder)
        if not result_dir.is_dir():
            return None
        failed_prefix = f"Failed_{self._current_test_name}_"
        failed_images = sorted(
            path for path in result_dir.iterdir()
            if path.is_file() and path.name.startswith(failed_prefix) and path.suffix.lower() == ".png"
        )
        if failed_images:
            return failed_images[0]

        test_prefix = f"{self._current_test_name}_"
        result_images = [
            path for path in result_dir.iterdir()
            if path.is_file() and path.name.startswith(test_prefix) and path.suffix.lower() == ".png"
        ]
        return max(result_images, key=lambda path: path.stat().st_mtime_ns, default=None)

    def _reference_image_path(self, reference_name):
        reference_root = self._robot_variable("ref_image_path")
        component = self._robot_variable("component")
        if not reference_root or not component or not reference_name:
            return None

        filename = Path(str(reference_name)).name
        if not Path(filename).suffix:
            filename += ".png"
        source = Path(reference_root) / str(component) / filename
        if not source.is_file():
            self._debug(f"REFERENCE IMAGE NOT FOUND | {source}")
            return None

        return source.resolve()

    def _artifact_url(self, path):
        source = Path(path).resolve()
        output_dir = self._robot_variable("OUTPUT_DIR")
        if output_dir:
            relative = Path(os.path.relpath(source, Path(output_dir).resolve())).as_posix()
            return quote(relative, safe="/.")
        return source.as_uri()

    def _manifest_source(self, source, kind):
        source = Path(source).resolve()
        if kind == "comparison":
            root = self._robot_variable("RESULT_PATH")
            if root:
                try:
                    return source.relative_to(Path(root).resolve()).as_posix()
                except ValueError:
                    pass
        elif kind == "capture":
            root = self._robot_variable("SCREENSHOT_COMPARE_PATH")
            if root:
                try:
                    return source.relative_to(Path(root).resolve()).as_posix()
                except ValueError:
                    pass
        else:
            for parent in source.parents:
                if parent.name.lower() == "references":
                    return source.relative_to(parent).as_posix()
        return None

    def _record_failure_screenshots(self, paths, keyword_name, reference_path=None):
        output_dir = self._robot_variable("OUTPUT_DIR")
        if not output_dir:
            target_root = os.environ.get("DASHBOARD_SCREENSHOT_DIR")
            output_dir = str(Path(target_root).parent) if target_root else None
        if not output_dir:
            return

        output_path = Path(output_dir)
        self._output_dir = output_path
        for image_path in paths:
            image_kind = "comparison"
            image_source = self._manifest_source(image_path, image_kind)
            if image_source is None:
                image_kind = "capture"
                image_source = self._manifest_source(image_path, image_kind)
            if image_source is None:
                self._debug(f"SCREENSHOT SOURCE OUTSIDE ALLOWED ROOTS | {image_path}")
                continue
            record = {
                "test_name": self._current_test_name,
                "test_full_name": self._current_test_full_name or self._current_test_name,
                "keyword": keyword_name,
                "image_source": image_source,
                "image_kind": image_kind,
            }
            if reference_path:
                reference_source = self._manifest_source(reference_path, "reference")
                if reference_source:
                    record["reference_source"] = reference_source
            self._failure_screenshots.append(record)

            image_href = html.escape(self._artifact_url(image_path), quote=True)
            alt_text = html.escape(f"Failure screenshot after {keyword_name}", quote=True)
            images_html = (
                f'<figure><figcaption>Failed screenshot</figcaption><a href="{image_href}">'
                f'<img src="{image_href}" alt="{alt_text}" '
                'style="max-width:600px;max-height:450px;height:auto;object-fit:contain"></a></figure>'
            )
            if reference_path:
                reference_href = self._artifact_url(reference_path)
                if reference_href:
                    escaped_reference = html.escape(reference_href, quote=True)
                    images_html += (
                        f'<figure><figcaption>Reference image</figcaption><a href="{escaped_reference}">'
                        f'<img src="{escaped_reference}" alt="Reference image for {html.escape(self._current_test_name)}" '
                        'style="max-width:600px;max-height:450px;height:auto;object-fit:contain"></a></figure>'
                    )
            image_message = (
                f'<p>Screenshot comparison after <b>{html.escape(keyword_name)}</b>:</p>'
                f'<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start">{images_html}</div>'
            )
            try:
                from robot.api import logger

                logger.info(image_message, html=True)
            except Exception as exc:
                self._debug(f"SCREENSHOT LOG FAILED | {exc}")

        if self._failure_screenshots:
            manifest_path = output_path / "failure-screenshots.json"
            try:
                manifest_path.write_text(
                    json.dumps(self._failure_screenshots, indent=2),
                    encoding="utf-8",
                )
            except OSError as exc:
                self._debug(f"SCREENSHOT MANIFEST FAILED | {exc}")

    def _finalize_native_report(self):
        if not self._output_dir:
            return
        report_path = self._output_dir / "report.html"
        manifest_path = self._output_dir / "failure-screenshots.json"
        if not report_path.is_file() or not manifest_path.is_file():
            return
        root_dir = Path(__file__).resolve().parent
        if str(root_dir) not in sys.path:
            sys.path.insert(0, str(root_dir))
        try:
            from run_robot_protected import embed_failure_screenshots

            embed_failure_screenshots(report_path, [manifest_path])
        except Exception as exc:
            self._debug(f"REPORT SCREENSHOT EMBED FAILED | {exc}")

    def _capture_failure_screenshot(self, keyword_name, reference_name=None):
        if self._capturing_screenshot or self._failure_screenshot_recorded:
            return
        self._capturing_screenshot = True
        try:
            source = self._failed_result_screenshot()
            saved_paths = [source] if source else []
            reference_path = self._reference_image_path(reference_name)
            self._record_failure_screenshots(saved_paths, keyword_name, reference_path)
            self._failure_screenshot_recorded = bool(saved_paths)
        except Exception as exc:
            self._debug(f"SCREENSHOT CAPTURE FAILED | {keyword_name} | {exc}")
        finally:
            self._capturing_screenshot = False

    def start_suite(self, name, attrs):
        self._suite_stack.append(attrs.get("longname") or name)

    def end_suite(self, name, attrs):
        if self._suite_stack:
            self._suite_stack.pop()

    def start_test(self, name, attrs):
        """Send the current test context to the dashboard debug stream."""
        documentation = " ".join((attrs.get("doc") or "").split())
        suffix = f" :: {documentation}" if documentation else ""
        self._keyword_depth = 0
        self._current_test_name = name
        self._current_test_full_name = attrs.get("longname") or ".".join([*self._suite_stack, name])
        self._failure_screenshot_recorded = False
        self._failure_keyword_name = ""
        self._reference_image_name = ""
        full_name = attrs.get("longname") or ".".join([*self._suite_stack, name])
        self._debug(f"TEST START | {full_name}{suffix}")
        self._test_event("start", name, attrs)

    def end_test(self, name, attrs):
        """Send the final test status and duration to the dashboard."""
        status = attrs.get("status", "UNKNOWN")
        start_time = attrs.get("starttime")
        end_time = attrs.get("endtime")
        duration = ""
        if start_time and end_time:
            try:
                duration = f" | duration={(int(end_time) - int(start_time)) / 1000:.1f}s"
            except (TypeError, ValueError):
                pass
        full_name = attrs.get("longname") or ".".join([*self._suite_stack, name])
        self._debug(f"TEST {status} | {full_name}{duration}")
        if status == "FAIL":
            self._capture_failure_screenshot(
                self._failure_keyword_name or "(test failure)",
                reference_name=self._reference_image_name,
            )
        self._test_event("end", name, attrs)
        self._current_test_name = ""
        self._current_test_full_name = ""

    def start_keyword(self, name, attrs):
        """Stream each keyword as it starts, preserving nested indentation."""
        keyword_type = attrs.get("type", "KEYWORD")
        library = attrs.get("libname")
        source = attrs.get("source")
        line_number = attrs.get("lineno")
        details = [keyword_type]
        if library:
            details.append(f"library={library}")
        if source:
            location = f"{source}:{line_number}" if line_number else source
            details.append(f"source={location}")
        indent = "  " * self._keyword_depth
        self._debug(f"{indent}KEYWORD START | {name} | {' | '.join(details)}")
        self._keyword_depth += 1

    def end_keyword(self, name, attrs):
        """Stream keyword outcome and duration after execution."""
        self._keyword_depth = max(0, self._keyword_depth - 1)
        status = attrs.get("status", "UNKNOWN")
        elapsed = attrs.get("elapsedtime")
        duration = f" | elapsed={elapsed}" if elapsed is not None else ""
        indent = "  " * self._keyword_depth
        self._debug(f"{indent}KEYWORD {status} | {name}{duration}")
        normalized_name = name.casefold().replace("_", " ")
        if "compare screenshot" in normalized_name or "image template match" in normalized_name:
            args = attrs.get("args") or []
            if args:
                self._reference_image_name = str(args[0]).split("=", 1)[-1]
            if status == "FAIL":
                self._failure_keyword_name = name
                self._capture_failure_screenshot(name, reference_name=self._reference_image_name)
        elif status == "FAIL" and not self._failure_keyword_name:
            self._failure_keyword_name = name
