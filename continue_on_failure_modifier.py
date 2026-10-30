"""Pre-run modifier to wrap test keywords with
`Run Keyword And Continue On Failure` without editing .robot files.
"""

from robot.api import SuiteVisitor


def _normalize(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch not in " _")


class ContinueOnFailureModifier(SuiteVisitor):
    """Wrap keyword calls so execution continues after failures."""

    WRAP_KEYWORD = "Run Keyword And Continue On Failure"
    _SKIP = {
        _normalize("Run Keyword And Continue On Failure"),
        _normalize("Run Keyword"),
        _normalize("Run Keywords"),
        _normalize("Run Keyword If"),
        _normalize("Run Keyword Unless"),
        _normalize("Run Keyword And Ignore Error"),
        _normalize("Run Keyword And Return Status"),
        _normalize("Run Keyword And Warn On Failure"),
        _normalize("Run Keyword And Expect Error"),
        _normalize("Return From Keyword"),
        _normalize("Return From Keyword If"),
        _normalize("Run Keyword And Return"),
        _normalize("Run Keyword And Return If"),
        _normalize("Pass Execution"),
        _normalize("Pass Execution If"),
        _normalize("Skip"),
        _normalize("Skip If"),
        _normalize("Fatal Error"),
    }

    def visit_keyword(self, keyword):
        name = (keyword.name or "").strip()
        if not name:
            return

        if _normalize(name) in self._SKIP:
            return

        # Transform:
        #     Some Keyword    arg1    arg2
        # into:
        #     Run Keyword And Continue On Failure    Some Keyword    arg1    arg2
        keyword.name = self.WRAP_KEYWORD
        keyword.args = (name, *keyword.args)
