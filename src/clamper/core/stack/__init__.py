"""The CloudFormation templates, generated rather than shipped as files.

Generated because the IAM policy inside the main stack comes from
`actions.yml`, and a static template would be a copy of it to keep in step.
The generator emits **plain YAML with no macros and no transforms**, using
the long form of every intrinsic function, so what you read is what deploys
and any YAML parser can read it.

Two stacks, because a Lambda's code has to exist in S3 before the stack that
references it is created:

- `bootstrap.py` — one S3 bucket for the deployment zip.
- `main.py` — everything else.

`clamper deploy --template-only` writes both to a directory and makes no AWS
calls at all, which is how a careful person evaluates a tool that is going
to be able to stop their infrastructure.
"""

from __future__ import annotations

from typing import Any

import yaml

FORMAT_VERSION = "2010-09-09"


def ref(name: str) -> dict[str, Any]:
    return {"Ref": name}


def get_att(name: str, attribute: str) -> dict[str, Any]:
    return {"Fn::GetAtt": [name, attribute]}


def sub(text: str) -> dict[str, Any]:
    return {"Fn::Sub": text}


def substitute(value: Any) -> Any:
    """Wrap every string carrying a `${AWS::...}` placeholder in `Fn::Sub`.

    The generated IAM policy is built with the placeholders as its account
    and region, so the template needs no AWS call to produce — and
    CloudFormation fills them in at deploy time.
    """
    if isinstance(value, str) and "${AWS::" in value:
        return sub(value)
    if isinstance(value, dict):
        return {key: substitute(item) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item) for item in value]
    return value


class _NoAliasDumper(yaml.SafeDumper):
    """A dumper that never writes `&id001` anchors or `*id001` aliases.

    PyYAML emits them whenever one Python object appears twice in the tree —
    two budget notifications sharing a subscriber list, say. CloudFormation
    rejects aliases unless the template has been through `package`, and
    cfn-lint flags them as W1101. That is how this was found.
    """

    def ignore_aliases(self, data: Any) -> bool:
        return True


def render(template: dict[str, Any], header: str) -> str:
    """YAML, in declaration order, with a comment header.

    `sort_keys=False` because a template read top to bottom should read in
    the order it was written: parameters, then the resources in the order
    they depend on each other, then outputs.
    """
    comment = "".join(f"# {line}".rstrip() + "\n" for line in header.strip().splitlines())
    body = yaml.dump(
        template,
        Dumper=_NoAliasDumper,
        sort_keys=False,
        width=100,
        default_flow_style=False,
    )
    return comment + "\n" + body
