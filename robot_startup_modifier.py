"""Inject common startup preconditions without changing Robot source files."""

from pathlib import Path

from robot.api import SuiteVisitor


class RobotStartupModifier(SuiteVisitor):
    """Run the required language setup and startup wait for every Robot file."""

    def visit_suite(self, suite):
        source = getattr(suite, "source", None)
        if source and Path(source).suffix.lower() == ".robot":
            source_path = Path(source).resolve()
            autobot_resource = next(
                (
                    parent / "AutobotResources" / "autobot_resource.robot"
                    for parent in source_path.parents
                    if (parent / "AutobotResources" / "autobot_resource.robot").is_file()
                ),
                None,
            )
            if autobot_resource:
                imported_resources = {
                    str(
                        (source_path.parent / item.name).resolve()
                        if not Path(item.name).is_absolute()
                        else Path(item.name).resolve()
                    )
                    for item in suite.resource.imports
                    if item.type == "RESOURCE"
                }
                if str(autobot_resource.resolve()) not in imported_resources:
                    suite.resource.imports.resource(str(autobot_resource).replace("\\", "/"))

            setup = suite.setup
            original = []
            if setup.name:
                original.extend((setup.name, *setup.args))
                if setup.name.replace(" ", "").lower() == "runsuitesetup":
                    original = ["Autobot Setup"]

            suite_name = Path(source).name
            setup.name = "Run Keywords"
            setup.args = (
                *((*original, "AND") if original else ()),
                "Log To Console",
                f"[startup] {suite_name}: Sleep 30s - starting",
                "AND",
                "Sleep",
                "30s",
                "AND",
                "Log To Console",
                f"[startup] {suite_name}: Sleep 30s - completed",
                "AND",
                "Log To Console",
                f"[startup] {suite_name}: change_language English - starting",
                "AND",
                "Wait Until Keyword Succeeds",
                "2x",
                "30s",
                "change_language",
                "English",
                "AND",
                "Log To Console",
                f"[startup] {suite_name}: change_language English - completed",
            )

        super().visit_suite(suite)