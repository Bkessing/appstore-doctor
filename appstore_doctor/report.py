"""Finding model and output rendering.

A finding is deliberately more than pass/fail. Every App Store failure in the
catalogue below was originally diagnosed the hard way because the error Apple
surfaces names a symptom rather than a cause: a missing distribution
certificate reports as "No Accounts", a locked keychain reports as
errSecInternalComponent on an unrelated framework. So each finding carries the
cause AND the fix, not just a red X.
"""

import json
import sys
from dataclasses import dataclass, field, asdict

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

_ORDER = {FAIL: 0, WARN: 1, SKIP: 2, OK: 3}

_GLYPH = {OK: "PASS", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}

# ANSI, disabled when not a tty so piping to a file or an agent stays clean.
_COLOR = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", SKIP: "\033[90m"}
_RESET = "\033[0m"


@dataclass
class Finding:
    check: str
    status: str
    summary: str
    detail: str = ""
    fix: str = ""
    docs: str = ""
    data: dict = field(default_factory=dict)


class Report:
    def __init__(self):
        self.findings = []

    def add(self, finding: Finding):
        self.findings.append(finding)
        return finding

    def ok(self, check, summary, **kw):
        return self.add(Finding(check, OK, summary, **kw))

    def warn(self, check, summary, **kw):
        return self.add(Finding(check, WARN, summary, **kw))

    def fail(self, check, summary, **kw):
        return self.add(Finding(check, FAIL, summary, **kw))

    def skip(self, check, summary, **kw):
        return self.add(Finding(check, SKIP, summary, **kw))

    @property
    def failed(self):
        return [f for f in self.findings if f.status == FAIL]

    @property
    def warned(self):
        return [f for f in self.findings if f.status == WARN]

    def exit_code(self):
        return 1 if self.failed else 0

    def to_json(self):
        return json.dumps(
            {
                "summary": {
                    "fail": len(self.failed),
                    "warn": len(self.warned),
                    "ok": len([f for f in self.findings if f.status == OK]),
                    "skip": len([f for f in self.findings if f.status == SKIP]),
                },
                "findings": [asdict(f) for f in self.findings],
            },
            indent=2,
        )

    def render(self, stream=sys.stdout, color=None):
        if color is None:
            color = stream.isatty()

        def paint(status, text):
            return f"{_COLOR[status]}{text}{_RESET}" if color else text

        ordered = sorted(self.findings, key=lambda f: (_ORDER[f.status], f.check))

        print("", file=stream)
        for f in ordered:
            print(f"  {paint(f.status, _GLYPH[f.status])}  {f.check}: {f.summary}", file=stream)
            if f.detail:
                for line in f.detail.splitlines():
                    print(f"          {line}", file=stream)
            if f.fix and f.status in (FAIL, WARN):
                for i, line in enumerate(f.fix.splitlines()):
                    prefix = "     fix: " if i == 0 else "          "
                    print(f"     {prefix.strip().ljust(5)} {line}" if i == 0
                          else f"           {line}", file=stream)
            if f.docs and f.status in (FAIL, WARN):
                print(f"           see: {f.docs}", file=stream)

        n_fail, n_warn = len(self.failed), len(self.warned)
        print("", file=stream)
        if n_fail:
            print(paint(FAIL, f"  {n_fail} blocking issue{'s' if n_fail != 1 else ''}"
                              + (f", {n_warn} warning{'s' if n_warn != 1 else ''}" if n_warn else "")),
                  file=stream)
        elif n_warn:
            print(paint(WARN, f"  no blocking issues, {n_warn} warning{'s' if n_warn != 1 else ''}"),
                  file=stream)
        else:
            print(paint(OK, "  no issues found"), file=stream)
        print("", file=stream)
