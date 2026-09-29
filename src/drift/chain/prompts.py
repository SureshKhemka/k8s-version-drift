"""Versioned prompt files (CH-8).

File format::

    ---
    id: draft
    version: draft-v1
    ---
    ## system
    ...jinja...
    ## user
    ...jinja...
    ## retry
    ...jinja (optional; sent after a gate failure with {{ errors }})...
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2
import yaml

from drift.llm.client import Message

_SECTION = re.compile(r"^## (system|user|retry)\s*$", re.MULTILINE)
_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=False, autoescape=False)


class PromptError(Exception):
    pass


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    path: str
    system: jinja2.Template
    user: jinja2.Template
    retry: jinja2.Template

    def messages(self, **ctx: Any) -> list[Message]:
        return [Message("system", self.system.render(**ctx).strip()), Message("user", self.user.render(**ctx).strip())]

    def retry_message(self, **ctx: Any) -> Message:
        return Message("user", self.retry.render(**ctx).strip())


DEFAULT_RETRY = (
    "Your previous answer failed these automatic checks:\n{{ errors }}\n\n"
    "Fix every problem and return the complete corrected answer, in the same format, with nothing else."
)


def load_prompt(path: Path) -> Prompt:
    try:
        raw = path.read_text()
    except OSError as exc:
        raise PromptError(f"cannot read prompt {path}: {exc.strerror}") from exc
    m = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", raw, re.DOTALL)
    if not m:
        raise PromptError(f"{path}: missing front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    if not meta.get("id") or not meta.get("version"):
        raise PromptError(f"{path}: front matter needs id and version")
    parts = _SECTION.split(m.group(2))
    sections = {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    if "system" not in sections or "user" not in sections:
        raise PromptError(f"{path}: needs '## system' and '## user' sections")
    try:
        return Prompt(
            id=str(meta["id"]),
            version=str(meta["version"]),
            path=str(path),
            system=_ENV.from_string(sections["system"]),
            user=_ENV.from_string(sections["user"]),
            retry=_ENV.from_string(sections.get("retry", DEFAULT_RETRY)),
        )
    except jinja2.TemplateSyntaxError as exc:
        raise PromptError(f"{path}: template error line {exc.lineno}: {exc.message}") from exc
