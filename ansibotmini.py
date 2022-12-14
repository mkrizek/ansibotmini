#!/usr/bin/env python3
# Copyright 2022 Martin Krizek <martin.krizek@gmail.com>
# GNU General Public License v3.0+ (see LICENSE or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures
import configparser
import dataclasses
import datetime
import hashlib
import io
import itertools
import json
import logging
import os.path
import pprint
import re
import shelve
import string
import sys
import time
import typing as t
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass

minimal_required_python_version = (3, 11)
if sys.version_info < minimal_required_python_version:
    raise SystemExit(
        f"ansibotmini requires Python {'.'.join((str(e) for e in minimal_required_python_version))} or newer. "
        f"Python version detected: {sys.version.split(' ')[0]}"
    )


BOT_ACCOUNT = "ansibot"

AZP_ARTIFACTS_URL_FMT = "https://dev.azure.com/ansible/ansible/_apis/build/builds/%s/artifacts?api-version=7.0"
AZP_TIMELINE_URL_FMT = "https://dev.azure.com/ansible/ansible/_apis/build/builds/%s/timeline/?api-version=7.0"
AZP_BUILD_URL_FMT = (
    "https://dev.azure.com/ansible/ansible/_apis/build/builds/%s?api-version=7.0"
)
AZP_BUILD_ID_RE = re.compile(
    r"https://dev\.azure\.com/(?P<organization>[^/]+)/(?P<project>[^/]+)/_build/results\?buildId=(?P<buildId>[0-9]+)",
)

GALAXY_URL = "https://galaxy.ansible.com/"
GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
COLLECTIONS_LIST_ENDPOINT = "https://sivel.eng.ansible.com/api/v1/collections/list"
COLLECTIONS_FILEMAP_ENDPOINT = (
    "https://sivel.eng.ansible.com/api/v1/collections/file_map"
)

STALE_CI_DAYS = 7
STALE_ISSUE_DAYS = 7
NEEDS_INFO_WARN_DAYS = 14
NEEDS_INFO_CLOSE_DAYS = 28
WAITING_ON_CONTRIBUTOR_CLOSE_DAYS = 365
SLEEP_SECONDS = 300

CONFIG_FILENAME = os.path.expanduser("~/.ansibotmini.cfg")
CACHE_FILENAME = os.path.expanduser("~/.ansibotmini_cache")
config = configparser.ConfigParser()
config.read(CONFIG_FILENAME)
gh_token = config.get("default", "gh_token")
azp_token = config.get("default", "azp_token")

COMPONENT_RE = re.compile(
    r"#{3,5}\scomponent\sname(.+?)(?=#{3,5}|$)", flags=re.IGNORECASE | re.DOTALL
)
OBJ_TYPE_RE = re.compile(
    r"#{3,5}\sissue\stype(.+?)(?=#{3,5}|$)", flags=re.IGNORECASE | re.DOTALL
)
VERSION_RE = re.compile(r"ansible\s\[core\s([^]]+)]")
COMPONENT_COMMAND_RE = re.compile(
    r"^(?:@ansibot\s)?!component\s([=+-]\S+)$", flags=re.MULTILINE
)

VALID_COMMANDS = (
    "bot_skip",
    "!bot_skip",
    "bot_broken",
    "!bot_broken",
    "needs_info",
    "waiting_on_contributor",
    "!needs_collection_redirect",
)
COMMANDS_RE = re.compile(f"^({'|'.join(VALID_COMMANDS)})\s*$", flags=re.MULTILINE)
RESOLVED_BY_PR_RE = re.compile(r"^resolved_by_pr\s([#0-9]+)\s*$", flags=re.MULTILINE)

ANSIBLE_PLUGINS = frozenset(
    (
        "action",
        "become",
        "cache",
        "callback",
        "cliconf",
        "connection",
        "doc_fragments",
        "filter",
        "httpapi",
        "inventory",
        "lookup",
        "netconf",
        "shell",
        "strategy",
        "terminal",
        "test",
        "vars",
    )
)

# TODO fetch from the actual template
ISSUE_TEMPLATE_SECTIONS = frozenset(
    (
        "Summary",
        "Issue Type",
        "Component Name",
        "Ansible Version",
        "Configuration",
        "OS / Environment",
    )
)

QUERY_NUMBERS_TMPL = """
query ($after: String) {
  rateLimit {
    limit
    cost
    remaining
    resetAt
  }
  repository(owner: "ansible", name: "ansible") {
    %s(states: OPEN, first: 100, after: $after) {
      pageInfo {
          hasNextPage
          endCursor
      }
      nodes {
        number
        updatedAt
        timelineItems(last: 1, itemTypes: [CROSS_REFERENCED_EVENT]) {
          updatedAt
        }
        %s
      }
    }
  }
}
"""

QUERY_ISSUE_NUMBERS = QUERY_NUMBERS_TMPL % ("issues", "")

QUERY_PR_NUMBERS = QUERY_NUMBERS_TMPL % (
    "pullRequests",
    """
commits(last:1) {
  nodes {
    commit {
      checkSuites(last:1) {
        nodes {
          updatedAt
          app {
            slug
          }
        }
      }
    }
  }
}
""",
)

QUERY_SINGLE_TMPL = """
query($number: Int!)
{
  repository(owner: "ansible", name: "ansible") {
    %s(number: $number) {
      id
      author {
        login
      }
      number
      title
      body
      labels (first: 20) {
        nodes {
          id
          name
        }
      }
      timelineItems(first: 200, itemTypes: [ISSUE_COMMENT, LABELED_EVENT, UNLABELED_EVENT, CROSS_REFERENCED_EVENT]) {
        pageInfo {
            endCursor
            hasNextPage
        }
        nodes {
          __typename
          ... on IssueComment {
            createdAt
            updatedAt
            author {
              login
            }
            body
          }
          ... on LabeledEvent {
            createdAt
            actor {
              login
            }
            label {
              name
            }
          }
          ... on UnlabeledEvent {
            createdAt
            actor {
              login
            }
            label {
              name
            }
          }
          ... on CrossReferencedEvent {
            createdAt
            source {
              ... on PullRequest {
                number
                repository {
                  name
                  owner {
                    ... on Organization {
                      name
                    }
                  }
                }
              }
            }
          }
        }
      }
      %s
    }
  }
  rateLimit {
    limit
    cost
    remaining
    resetAt
  }
}
"""

QUERY_SINGLE_ISSUE = QUERY_SINGLE_TMPL % ("issue", "")

QUERY_SINGLE_PR = QUERY_SINGLE_TMPL % (
    "pullRequest",
    """
baseRef {
  name
}
files(first: 50) {
  nodes {
    path
  }
}
last_commit: commits(last: 1) {
  nodes {
    commit {
      committedDate
      checkSuites(last: 1) {
        nodes {
          checkRuns(last: 1) {
            nodes {
              detailsUrl
            }
          }
          conclusion
          updatedAt
          status
          app {
            name
          }
        }
      }
    }
  }
}
commits(last: 50) {
  nodes {
    commit {
      parents(last: 2) {
        nodes {
          id
        }
      }
    }
  }
}
mergeable
reviews(last: 10, states: [APPROVED, CHANGES_REQUESTED, DISMISSED]) {
  nodes {
    author {
      login
    }
    state
    updatedAt
  }
}
headRepository {
  name
  owner {
    login
  }
}
closingIssuesReferences(last: 1) {
  nodes {
    number
  }
}
""",
)


@dataclass
class Response:
    status_code: int
    reason: str
    raw_data: bytes

    def json(self) -> t.Any:
        return json.loads(self.raw_data.decode())


@dataclass
class Issue:
    id: str
    author: str
    number: int
    title: str
    body: str
    events: list[dict]
    labels: dict[str, str]
    updated_at: datetime.datetime
    components: list[str]
    last_triaged: datetime.datetime


@dataclass
class PR(Issue):
    branch: str
    files: list[str]
    mergeable: str
    changes_requested: bool
    last_review: datetime.datetime
    last_commit: datetime.datetime
    ci: CI | None
    from_repo: str
    merge_commit: bool
    has_issue: bool


@dataclass
class CI:
    build_id: int
    conclusion: str
    status: str
    updated_at: datetime.datetime


@dataclass
class Command:
    updated_at: datetime.datetime
    arg: t.Optional[str] = None


@dataclass
class Actions:
    to_label: list[str] = dataclasses.field(default_factory=list)
    to_unlabel: list[str] = dataclasses.field(default_factory=list)
    comments: list[str] = dataclasses.field(default_factory=list)
    cancel_ci: bool = False
    close: bool = False


@dataclass
class TriageContext:
    collections_list: dict[str, t.Any]
    collections_file_map: dict[str, t.Any]
    committers: list[str]
    commands_found: dict[str, list[Command]] = dataclasses.field(default_factory=dict)


GH_OBJ = t.TypeVar("GH_OBJ", Issue, PR)
GH_OBJ_T = t.TypeVar("GH_OBJ_T", t.Type[Issue], t.Type[PR])

request_counter = 0


def http_request(
    url: str,
    data: str = "",
    headers: t.Optional[t.MutableMapping[str, str]] = None,
    method: str = "GET",
) -> Response:
    global request_counter
    if headers is None:
        headers = {}

    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                url, data=data.encode("ascii"), headers=headers, method=method.upper()
            ),
        ) as response:
            request_counter += 1
            logging.info(
                f"http request no. {request_counter}: {method} {url}: {response.status}, {response.reason}"
            )
            return Response(
                status_code=response.status,
                reason=response.reason,
                raw_data=response.read(),
            )
    except urllib.error.HTTPError as e:
        return Response(
            status_code=e.status,
            reason=e.reason,
            raw_data=b"",
        )


def send_query(data: str) -> Response:
    return http_request(
        GITHUB_GRAPHQL_URL,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {gh_token}",
        },
        data=data,
    )


def get_label_id(name: str) -> str:
    query = """
    query ($name: String!){
      repository(owner: "ansible", name: "ansible") {
        label(name: $name) {
          id
        }
      }
    }
    """
    resp = send_query(
        json.dumps(
            {
                "query": query,
                "variables": {"name": name},
            }
        )
    )

    data = resp.json()["data"]
    return data["repository"]["label"]["id"]


def add_labels(obj: GH_OBJ, labels: list[str]) -> None:
    # TODO gather label IDs globally from processed issues to limit API calls to get IDs
    query = """
    mutation($input: AddLabelsToLabelableInput!) {
      addLabelsToLabelable(input:$input) {
        clientMutationId
      }
    }
    """
    send_query(
        json.dumps(
            {
                "query": query,
                "variables": {
                    "input": {
                        "labelIds": [get_label_id(label) for label in labels],
                        "labelableId": obj.id,
                    },
                },
            }
        )
    )


def remove_labels(obj: GH_OBJ, labels: list[str]) -> None:
    query = """
    mutation($input: RemoveLabelsFromLabelableInput!) {
      removeLabelsFromLabelable(input:$input) {
        clientMutationId
      }
    }
    """
    send_query(
        json.dumps(
            {
                "query": query,
                "variables": {
                    "input": {
                        "labelIds": [obj.labels[label] for label in labels],
                        "labelableId": obj.id,
                    },
                },
            }
        )
    )


def add_comment(obj: GH_OBJ, body: str) -> None:
    query = """
    mutation($input: AddCommentInput!) {
      addComment(input:$input) {
        clientMutationId
      }
    }
    """
    send_query(
        json.dumps(
            {
                "query": query,
                "variables": {
                    "input": {
                        "body": body,
                        "subjectId": obj.id,
                    },
                },
            }
        )
    )


def close_issue(obj_id: str) -> None:
    query = """
    mutation($input: CloseIssueInput!) {
      closeIssue(input:$input) {
        clientMutationId
      }
    }
    """
    send_query(
        json.dumps(
            {
                "query": query,
                "variables": {
                    "input": {
                        "issueId": obj_id,
                    },
                },
            }
        )
    )


def close_pr(obj_id: str) -> None:
    query = """
    mutation($input: ClosePullRequestInput!) {
      closePullRequest(input:$input) {
        clientMutationId
      }
    }
    """
    send_query(
        json.dumps(
            {
                "query": query,
                "variables": {
                    "input": {
                        "pullRequestId": obj_id,
                    },
                },
            }
        )
    )


def close_object(obj: GH_OBJ) -> None:
    logging.info(f"{obj.__class__.__name__} #{obj.number}: closing")
    if isinstance(obj, Issue):
        close_issue(obj.id)
    elif isinstance(obj, PR):
        close_pr(obj.id)


def get_pr_state(number: int) -> str:
    query = """
    query($number: Int!)
    {
      repository(owner: "ansible", name: "ansible") {
        pullRequest(number: $number) {
          state
        }
      }
    }
    """
    resp = send_query(
        json.dumps(
            {
                "query": query,
                "variables": {"number": number},
            }
        )
    )

    return resp.json()["data"]["repository"]["pullRequest"]["state"]


def get_committers() -> list[str]:
    query = """
    query {
      organization(login: "ansible") {
        team(slug: "ansible-commit") {
          members {
            nodes {
              login
            }
          }
        }
      }
    }
    """
    resp = send_query(json.dumps({"query": query}))

    return [
        n["login"]
        for n in resp.json()["data"]["organization"]["team"]["members"]["nodes"]
    ]


def process_component(data):
    rv = []
    for line in (
        l
        for l in data.strip("\t\n\r").splitlines()
        if l and not ("<!--" in l or "-->" in l)
    ):
        for comma_split in line.split(","):
            space_split = comma_split.split(" ")
            if len(space_split) > 5:
                continue
            for c in space_split:
                c = c.strip()
                if "/" in c:
                    if "#" in c:
                        c = c.split("#")[0]
                    c.replace("\\", "").strip()
                else:
                    c = (
                        c.lower()
                        .removeprefix("the ")
                        .removeprefix("module ")
                        .removeprefix("plugin ")
                        .removesuffix(" module")
                        .removesuffix(" plugin")
                        .replace("ansible.builtin.", "")
                        .replace("ansible.legacy.", "")
                        .replace(".py", "")
                        .replace(".ps1", "")
                    )

                if c := re.sub(r"[^a-zA-Z/._-]", "", c):
                    if (
                        flatten := re.sub(
                            r"(lib/ansible/modules)/(.*)(/.+\.(?:py|ps1))", r"\1\3", c
                        )
                    ) != c:
                        rv.append(flatten)
                    if len(c) > 1:
                        rv.append(c)

    return rv


def get_template_path(name: str) -> str:
    return os.path.join(os.path.dirname(__file__), "templates", f"{name}.tmpl")


def match_existing_components(filenames: list[str]) -> list[str]:
    if not filenames:
        return []

    query_fmt = """
        {
          repository(owner: "ansible", name: "ansible") {
            %s
          }
        }
    """
    file_fmt = """
        %s: object(expression: "HEAD:%s") {
          ... on Blob {
            byteSize
          }
        }
    """
    paths = ["lib/ansible/modules/", "bin/", "lib/ansible/cli/"]
    paths.extend((f"lib/ansible/plugins/{name}/" for name in ANSIBLE_PLUGINS))
    files = []
    component_to_path = {}
    for i, filename in enumerate(filenames):
        if "/" in filename:
            query_name = f"file{i}"
            files.append(file_fmt % (query_name, filename))
            component_to_path[query_name] = filename
        else:
            for j, path in enumerate(paths):
                query_name = f"file{i}{j}"
                fname = f"{path}{filename}.py"
                files.append(file_fmt % (query_name, fname))
                component_to_path[query_name] = fname

    resp = send_query(json.dumps({"query": query_fmt % " ".join(files)}))
    return [
        component_to_path[file]
        for file, res in resp.json()["data"]["repository"].items()
        if res is not None
    ]


def last_labeled(obj: GH_OBJ, name: str) -> datetime.datetime:
    return max(
        (
            e["created_at"]
            for e in obj.events
            if e["name"] == "LabeledEvent" and e["label"] == name
        )
    )


def last_commented_by(obj: GH_OBJ, name: str) -> datetime.datetime | None:
    return max(
        (
            e["created_at"]
            for e in obj.events
            if e["name"] == "IssueComment" and e["author"] == name
        ),
        default=None,
    )


def last_boilerplate(obj: GH_OBJ, name: str) -> dict[str, t.Any] | None:
    return max(
        (
            e
            for e in obj.events
            if e["name"] == "IssueComment"
            and e["author"] == BOT_ACCOUNT
            and f"<!--- boilerplate: {name} --->" in e["body"]
        ),
        key=lambda x: x["created_at"],
        default=None,
    )


def days_since(when: datetime.datetime) -> int:
    return (datetime.datetime.now(datetime.timezone.utc) - when).days


def resolved_by_pr(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if commands := ctx.commands_found.get("resolved_by_pr"):
        if all(
            get_pr_state(int(command.arg)).lower() == "merged" for command in commands
        ):
            actions.close = True


def match_components(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    existing_components = []
    if isinstance(obj, PR):
        existing_components = obj.files
    elif isinstance(obj, Issue):
        processed_components = []
        if match := COMPONENT_RE.search(obj.body):
            processed_components = process_component(match.group(1))
            existing_components = match_existing_components(processed_components)

        command_components = []
        for command in ctx.commands_found.get("component", []):
            op, path = command.arg[0], command.arg[1:]
            command_components.append(path)
            match op:
                case "=":
                    existing_components = [path]
                case "+":
                    existing_components.append(path)
                case "-":
                    if path in existing_components:
                        existing_components.remove(path)
                case _:
                    raise ValueError(
                        f"Incorrect operation for the component command: {op}"
                    )
        # FIXME check whether command components are valid

        post_comment = True
        if (comment := last_boilerplate(obj, "components_banner")) is not None:
            last_components = [
                re.sub(r"[*`\[\]]", "", re.sub(r"\([^)]+\)", "", line)).strip()
                for line in comment["body"].splitlines()
                if line.startswith("*")
            ]
            post_comment = existing_components != last_components

        if post_comment:
            entries = [f"* `{component}`" for component in existing_components]
            with open(get_template_path("components_banner")) as f:
                actions.comments.append(
                    string.Template(f.read()).substitute(
                        components="\n".join(entries) if entries else None
                    )
                )

        if "!needs_collection_redirect" not in ctx.commands_found:
            entries = []
            # components such as namespace.collection_name.plugin_name
            for component in processed_components:
                fqcn = component.split(".")
                if len(fqcn) != 3:
                    continue
                if collection_data := ctx.collections_list.get(".".join(fqcn[:2])):
                    entries.append(
                        (
                            component,
                            collection_data["manifest"]["collection_info"],
                        )
                    )

            for component in itertools.chain(processed_components, command_components):
                if "/" not in component:
                    continue
                # TODO all plugins
                flatten = re.sub(
                    r"lib/ansible/(plugins/connection)/(.*)(/.+\.(?:py|ps1))",
                    r"\1\3",
                    component,
                ).replace("lib/ansible/", "")
                for fqcn in ctx.collections_file_map.get(flatten, []):
                    entries.append(
                        (
                            component,
                            ctx.collections_list[fqcn]["manifest"]["collection_info"],
                        )
                    )
                if entries:
                    break
            else:
                for candidate in [
                    f"plugins/{plugin_type}/{component}.{ext}"
                    for component, plugin_type, ext in itertools.product(
                        (c for c in processed_components if "/" not in c),
                        (itertools.chain(ANSIBLE_PLUGINS, ["modules"])),
                        ("py", "ps1"),
                    )
                ]:
                    for fqcn in ctx.collections_file_map.get(candidate, []):
                        entries.append(
                            (
                                candidate,
                                ctx.collections_list[fqcn]["manifest"][
                                    "collection_info"
                                ],
                            )
                        )

            if entries:
                if len(entries) > 1:
                    entries = list(
                        filter(
                            lambda x: x[1]["namespace"] in x[1]["repository"]
                            and x[1]["name"] in x[1]["repository"],
                            entries,
                        )
                    )
                assembled_entries = []
                for candidate, collection_info in entries:
                    assembled_entries.append(
                        f"* {candidate} -> {collection_info['repository']} "
                        f"({GALAXY_URL}{collection_info['namespace']}.{collection_info['name']})"
                    )
                with open(get_template_path("collection_redirect")) as f:
                    actions.comments.append(
                        string.Template(f.read()).substitute(
                            components="\n".join(assembled_entries)
                        )
                    )
                actions.to_label.append("bot_closed")
                actions.close = True

    obj.components = existing_components

    logging.info(
        f"{obj.__class__.__name__} #{obj.number}: identified components: {', '.join(obj.components)}"
    )


def needs_triage(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if not any(
        e
        for e in obj.events
        if e["name"] == "LabeledEvent" and e["label"] in ("needs_triage", "triage")
    ):
        actions.to_label.append("needs_triage")


def waiting_on_contributor(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if "waiting_on_contributor" in ctx.commands_found:
        actions.to_label.append("waiting_on_contributor")
    if (
        "waiting_on_contributor" in obj.labels
        and days_since(last_labeled(obj, "waiting_on_contributor"))
        > WAITING_ON_CONTRIBUTOR_CLOSE_DAYS
    ):
        actions.close = True
        actions.to_label.append("bot_closed")
        actions.to_unlabel.append("waiting_on_contributor")
        with open(get_template_path("waiting_on_contributor")) as f:
            actions.comments.append(f.read())


def needs_info(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if "needs_info" in ctx.commands_found:
        actions.to_label.append("needs_info")

    if "needs_info" in obj.labels or "needs_info" in actions.to_label:
        labeled_datetime = last_labeled(obj, "needs_info")
        commented_datetime = last_commented_by(obj, obj.author)
        if commented_datetime is None or labeled_datetime > commented_datetime:
            days_labeled = days_since(labeled_datetime)
            if days_labeled > NEEDS_INFO_CLOSE_DAYS:
                actions.close = True
                with open(get_template_path("needs_info_close")) as f:
                    actions.comments.append(
                        string.Template(f.read()).substitute(
                            author=obj.author, object_type=obj.__class__.__name__
                        )
                    )
            elif days_labeled > NEEDS_INFO_WARN_DAYS:
                last_warned = last_boilerplate(obj, "needs_info_warn")
                if last_warned is None:
                    last_warned = last_boilerplate(obj, "needs_info_base")
                if last_warned is None or last_warned["created_at"] < labeled_datetime:
                    with open(get_template_path("needs_info_warn")) as f:
                        actions.comments.append(
                            string.Template(f.read()).substitute(
                                author=obj.author,
                                object_type=obj.__class__.__name__,
                            )
                        )
        else:
            if "needs_info" in actions.to_label:
                actions.to_label.remove("needs_info")
            actions.to_unlabel.append("needs_info")


def match_object_type(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if match := OBJ_TYPE_RE.search(obj.body):
        data = re.sub(r"~[^~]+~", "", match.group(1).lower())
        if "feature" in data:
            actions.to_label.append("feature")
        if "bug" in data:
            actions.to_label.append("bug")
        if "documentation" in data or "docs" in data:
            actions.to_label.append("docs")
        if "test" in data:
            actions.to_label.append("test")


def match_version(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if match := VERSION_RE.search(obj.body):
        label_name = f"affects_{'.'.join(match.group(1).split('.')[:2])}"
        if not any(
            e
            for e in obj.events
            if e["name"] == "UnlabeledEvent"
            and e["author"] in ctx.committers
            and e["label"] == label_name
        ):
            actions.to_label.append(label_name)


def ci_comments(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if not isinstance(obj, PR) or obj.ci is None:
        return
    resp = http_request(AZP_TIMELINE_URL_FMT % obj.ci.build_id)
    if resp.status_code == 404:
        # not available anymore
        if obj.ci.conclusion == "success":
            actions.to_unlabel.append("ci_verified")
        return
    failed_job_ids = [
        r["id"]
        for r in resp.json()["records"]
        if r["type"] == "Job" and r["result"] == "failed"
    ]
    if not failed_job_ids:
        actions.to_unlabel.append("ci_verified")
        return
    ci_comment = []
    ci_verifieds = []
    for url in (
        a["resource"]["downloadUrl"]
        for a in http_request(AZP_ARTIFACTS_URL_FMT % obj.ci.build_id).json()["value"]
        if a["name"].startswith("Bot ") and a["source"] in failed_job_ids
    ):
        zfile = zipfile.ZipFile(io.BytesIO(http_request(url).raw_data))
        for filename in zfile.namelist():
            if "ansible-test-" not in filename:
                continue
            with zfile.open(filename) as f:
                artifact_data = json.load(f)
                ci_verifieds.append(artifact_data["verified"])
                for r in artifact_data["results"]:
                    ci_comment.append(f"{r['message']}\n```\n{r['output']}\n```\n")
    if ci_comment:
        results = "\n".join(ci_comment)
        r_hash = hashlib.md5(results.encode()).hexdigest()
        if not any(
            e
            for e in obj.events
            if e["name"] == "IssueComment"
            and e["author"] == BOT_ACCOUNT
            and "<!--- boilerplate: ci_test_result --->" in e["body"]
            and f"<!-- r_hash: {r_hash} -->" in e["body"]
        ):
            with open(get_template_path("ci_test_results")) as f:
                actions.comments.append(
                    string.Template(f.read()).substitute(
                        results=results,
                        r_hash=r_hash,
                    )
                )
    # ci_verified
    if all(ci_verifieds) and len(ci_verifieds) == len(failed_job_ids):
        actions.to_label.append("ci_verified")
    else:
        actions.to_unlabel.append("ci_verified")


def needs_revision(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if not isinstance(obj, PR) or obj.ci is None:
        return
    if obj.changes_requested or obj.ci.conclusion != "success":
        actions.to_label.append("needs_revision")
    else:
        actions.to_unlabel.append("needs_revision")


def needs_ci(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if not isinstance(obj, PR):
        return
    label = "needs_ci"
    if obj.ci is None or obj.ci.status != "completed":
        if "pre_azp" not in obj.labels:
            actions.to_label.append(label)
    else:
        actions.to_unlabel.append(label)
        actions.to_unlabel.append("pre_azp")


def stale_ci(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if not isinstance(obj, PR) or obj.ci is None:
        return
    if days_since(obj.ci.updated_at) > STALE_CI_DAYS:
        actions.to_label.append("stale_ci")
    else:
        actions.to_unlabel.append("stale_ci")


def docs_only(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if not isinstance(obj, PR):
        return
    if all(c.startswith("docs/") for c in obj.components):
        actions.to_label.append("docs_only")
        if last_boilerplate(obj, "docs_team_info") is None:
            with open(get_template_path("docs_team_info")) as f:
                actions.comments.append(f.read())


def backport(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if not isinstance(obj, PR):
        return
    if obj.branch.startswith("stable-"):
        actions.to_label.append("backport")
    else:
        actions.to_unlabel.append("backport")


def is_module(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if any(c.startswith("lib/ansible/modules/") for c in obj.components):
        actions.to_label.append("module")
    else:
        actions.to_unlabel.append("module")


def needs_rebase(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if not isinstance(obj, PR):
        return
    if obj.mergeable == "conflicting":
        actions.to_label.append("needs_rebase")
    else:
        actions.to_unlabel.append("needs_rebase")


def stale_review(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if not isinstance(obj, PR) or obj.last_review is None:
        return
    if obj.last_review < obj.last_commit:
        actions.to_label.append("stale_review")
    else:
        actions.to_unlabel.append("stale_review")


def pr_from_upstream(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if not isinstance(obj, PR) or obj.from_repo != "ansible/ansible":
        return
    actions.close = True
    with open(get_template_path("pr_from_upstream")) as f:
        actions.comments.append(string.Template(f.read()).substitute(author=obj.author))
    if obj.ci is not None:
        actions.cancel_ci = True


def cancel_ci(build_id: int) -> None:
    logging.info("Cancelling CI buildId %d", build_id)
    resp = http_request(
        url=AZP_BUILD_URL_FMT % build_id,
        method="patch",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Basic {}".format(
                base64.b64encode(f":{azp_token}".encode()).decode()
            ),
        },
        data=json.dumps({"status": "Cancelling"}),
    )
    logging.info("Cancelled with status_code: %d", resp.status_code)


def bad_pr(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if not isinstance(obj, PR):
        return
    if obj.merge_commit:
        actions.cancel_ci = True


def linked_objs(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if isinstance(obj, PR):
        if obj.has_issue:
            actions.to_label.append("has_issue")
        else:
            actions.to_unlabel.append("has_issue")
    elif isinstance(obj, Issue):
        if [e for e in obj.events if e["name"] == "CrossReferencedEvent"]:
            actions.to_label.append("has_pr")
        else:
            actions.to_unlabel.append("has_pr")


def needs_template(obj: GH_OBJ, actions: Actions, ctx: TriageContext) -> None:
    if isinstance(obj, PR):
        return
    missing = []
    for section in ISSUE_TEMPLATE_SECTIONS:
        if (
            re.search(
                r"^#{3,5}\s*%s\s*$" % section,
                obj.body,
                flags=re.IGNORECASE | re.DOTALL | re.MULTILINE,
            )
            is None
        ):
            missing.append(section)
    if missing:
        actions.to_label.append("needs_template")
        actions.to_label.append("needs_info")
        if last_boilerplate(obj, "issue_missing_data") is None:
            with open(get_template_path("issue_missing_data")) as f:
                actions.comments.append(
                    string.Template(f.read()).substitute(
                        author=obj.author,
                        obj_type=obj.__class__.__name__,
                        missing_sections="\n".join((f"- {s}" for s in missing)),
                    )
                )
    else:
        actions.to_unlabel.append("needs_template")
        if (
            not [
                e
                for e in obj.events
                if e["name"] == "LabeledEvent"
                and e["label"] == "needs_info"
                and e["author"] != BOT_ACCOUNT
            ]
            and "needs_info" not in ctx.commands_found
        ):
            actions.to_unlabel.append("needs_info")


bot_funcs = [
    match_components,  # must be executed first, other funcs use detected components
    resolved_by_pr,
    needs_triage,
    waiting_on_contributor,
    needs_info,
    match_object_type,
    match_version,
    ci_comments,
    needs_revision,
    needs_ci,
    stale_ci,
    docs_only,
    backport,
    is_module,
    needs_rebase,
    stale_review,
    pr_from_upstream,
    bad_pr,
    linked_objs,
    needs_template,
]


def triage(objects: dict[str, GH_OBJ], dry_run: t.Optional[bool] = None) -> None:
    ctx = TriageContext(
        collections_list=http_request(COLLECTIONS_LIST_ENDPOINT).json(),
        collections_file_map=http_request(COLLECTIONS_FILEMAP_ENDPOINT).json(),
        committers=get_committers(),
    )
    for obj in objects.values():
        logging.info(f"Triaging {obj.__class__.__name__} {obj.title} (#{obj.number})")
        # commands
        bodies = itertools.chain(
            ((obj.body, obj.updated_at),),
            (
                (e["body"], e["updated_at"])
                for e in obj.events
                if e["name"] == "IssueComment"
            ),
        )
        ctx.commands_found = collections.defaultdict(list)
        for body, updated_at in bodies:
            for command in COMMANDS_RE.findall(body):
                ctx.commands_found[command].append(Command(updated_at=updated_at))
            if match := RESOLVED_BY_PR_RE.search(body):
                ctx.commands_found["resolved_by_pr"].append(
                    Command(updated_at=updated_at, arg=match.group(1).removeprefix("#"))
                )
            for component in COMPONENT_COMMAND_RE.findall(body):
                ctx.commands_found["component"].append(
                    Command(updated_at=updated_at, arg=component)
                )

        is_bot_broken = "bot_broken" in ctx.commands_found and (
            "!bot_broken" not in ctx.commands_found
            or ctx.commands_found["bot_broken"][-1].updated_at
            > ctx.commands_found["!bot_broken"][-1].updated_at
        )
        is_bot_skip = "bot_skip" in ctx.commands_found and (
            "!bot_skip" not in ctx.commands_found
            or ctx.commands_found["bot_skip"][-1].updated_at
            > ctx.commands_found["!bot_skip"][-1].updated_at
        )
        if is_bot_broken:
            logging.info(
                f"Skipping {obj.__class__.__name__} {obj.title} (#{obj.number}) due to bot_broken"
            )
            if not dry_run:
                add_labels(obj, ["bot_broken"])
            continue
        else:
            if not dry_run:
                remove_labels(obj, ["bot_broken"])
        if is_bot_skip:
            logging.info(
                f"Skipping {obj.__class__.__name__} {obj.title} (#{obj.number}) due to bot_skip"
            )
            continue

        # triage
        actions = Actions()
        for f in bot_funcs:
            f(obj, actions, ctx)

        logging.debug(pprint.pformat(actions))
        actions.to_label = [l for l in actions.to_label if l not in obj.labels]
        actions.to_unlabel = [l for l in actions.to_unlabel if l in obj.labels]

        if common_labels := set(actions.to_label).intersection(actions.to_unlabel):
            raise AssertionError(
                f"The following labels were scheduled to be both added and removed {', '.join(common_labels)}"
            )

        logging.info(pprint.pformat(actions))
        if not dry_run:
            if actions.to_label:
                add_labels(obj, actions.to_label)
            if actions.to_unlabel:
                remove_labels(obj, actions.to_unlabel)

            for comment in actions.comments:
                add_comment(obj, comment)

            if actions.cancel_ci:
                cancel_ci(obj.ci.build_id)

            if actions.close:
                close_object(obj)

        logging.info(
            f"Done triaging {obj.__class__.__name__} {obj.title} (#{obj.number})"
        )


def process_events(issue: dict[str, t.Any]) -> list[dict[str, str]]:
    rv = []
    for node in issue["timelineItems"]["nodes"]:
        event = dict(
            name=node["__typename"],
            created_at=datetime.datetime.fromisoformat(node["createdAt"]),
        )
        if node["__typename"] in ["LabeledEvent", "UnlabeledEvent"]:
            event["label"] = node["label"]["name"]
            event["author"] = node["actor"]["login"]
        elif node["__typename"] == "IssueComment":
            event["body"] = node["body"]
            event["updated_at"] = datetime.datetime.fromisoformat(node["updatedAt"])
            event["author"] = (
                node["author"]["login"] if node["author"] is not None else ""
            )
        elif node["__typename"] == "CrossReferencedEvent" and node["source"]:
            event["number"] = node["source"]["number"]
            event["repo"] = node["source"]["repository"]
            event["owner"] = (
                node["source"]["repository"].get("owner", {}).get("name", "")
            )
        else:
            continue
        rv.append(event)

    return rv


def get_gh_objects(obj_name: str) -> list[tuple[str, datetime.datetime]]:
    query = QUERY_ISSUE_NUMBERS if obj_name == "issues" else QUERY_PR_NUMBERS
    rv = []
    variables = {}
    while True:
        resp = send_query(
            json.dumps(
                {
                    "query": query,
                    "variables": variables,
                }
            )
        )
        data = resp.json()["data"]
        logging.info(data["rateLimit"])

        objs = data["repository"][obj_name]
        for node in objs["nodes"]:
            updated_ats = [
                node["updatedAt"],
                node["timelineItems"]["updatedAt"],
            ]
            if obj_name == "pullRequests":
                last_commit = node["commits"]["nodes"][0]["commit"]
                if ci_results := last_commit["checkSuites"]["nodes"]:
                    updated_ats.append(ci_results[0]["updatedAt"])
            rv.append(
                (
                    str(node["number"]),
                    max(map(datetime.datetime.fromisoformat, updated_ats)),
                )
            )

        if objs["pageInfo"]["hasNextPage"]:
            variables["after"] = objs["pageInfo"]["endCursor"]
        else:
            break

    return rv


def fetch_object(
    number: str,
    obj: GH_OBJ_T,
    object_name: str,
    updated_at: t.Optional[datetime.datetime] = None,
) -> GH_OBJ:
    query = QUERY_SINGLE_ISSUE if object_name == "issue" else QUERY_SINGLE_PR
    resp = send_query(
        json.dumps(
            {
                "query": query,
                "variables": {"number": int(number)},
            }
        )
    )
    data = resp.json()["data"]
    logging.info(data["rateLimit"])
    o = data["repository"][object_name]
    if o is None:
        raise ValueError(f"{number} not found")

    kwargs = dict(
        id=o["id"],
        author=o["author"]["login"] if o["author"] else "ghost",
        number=o["number"],
        title=o["title"],
        body=o["body"],
        events=process_events(o),
        labels={node["name"]: node["id"] for node in o["labels"].get("nodes", [])},
        updated_at=updated_at,
        components=[],
        last_triaged=datetime.datetime.now(datetime.timezone.utc),
    )
    if object_name == "pullRequest":
        kwargs["branch"] = o["baseRef"]["name"]
        kwargs["files"] = [f["path"] for f in o["files"]["nodes"]]
        kwargs["mergeable"] = o["mergeable"].lower()
        reviews = {}
        for review in reversed(o["reviews"]["nodes"]):
            state = review["state"].lower()
            if state not in ("changes_requested", "dismissed"):
                continue
            author = review["author"]["login"]
            if author not in reviews:
                reviews[author] = state
        kwargs["changes_requested"] = "changes_requested" in reviews.values()
        kwargs["last_review"] = max(
            (r["updatedAt"] for r in o["reviews"]["nodes"]), default=None
        )
        if kwargs["last_review"]:
            kwargs["last_review"] = datetime.datetime.fromisoformat(
                kwargs["last_review"]
            )
        kwargs["last_commit"] = datetime.datetime.fromisoformat(
            o["last_commit"]["nodes"][0]["commit"]["committedDate"]
        )
        if check_suite := o["last_commit"]["nodes"][0]["commit"]["checkSuites"][
            "nodes"
        ]:
            check_suite = check_suite[0]
            conclusion = check_suite["conclusion"]
            if conclusion is not None:
                conclusion = conclusion.lower()
            kwargs["ci"] = CI(
                build_id=AZP_BUILD_ID_RE.search(
                    check_suite["checkRuns"]["nodes"][0]["detailsUrl"]
                ).group("buildId"),
                conclusion=conclusion,
                status=check_suite["status"].lower(),
                updated_at=datetime.datetime.fromisoformat(check_suite["updatedAt"]),
            )
        else:
            kwargs["ci"] = None
        repo = o["headRepository"]
        kwargs["from_repo"] = (
            f"{repo['owner']['login']}/{repo['name']}" if repo else "ghost/ghost"
        )
        kwargs["merge_commit"] = any(
            len(n["commit"]["parents"]["nodes"]) > 1 for n in o["commits"]["nodes"]
        )
        kwargs["has_issue"] = len(o["closingIssuesReferences"]["nodes"]) > 0

    return obj(**kwargs)


def fetch_object_by_number(number: str) -> GH_OBJ:
    try:
        obj = fetch_object(number, Issue, "issue")
    except ValueError:
        obj = fetch_object(number, PR, "pullRequest")

    return obj


def fetch_objects() -> dict[str, GH_OBJ]:
    with shelve.open(CACHE_FILENAME) as cache:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(get_gh_objects, "issues"): "issues",
                executor.submit(get_gh_objects, "pullRequests"): "prs",
            }
            number_map = collections.defaultdict(list)
            for future in concurrent.futures.as_completed(futures):
                issue_type = futures[future]
                number_map[issue_type] = [
                    (number, updated_at)
                    for number, updated_at in future.result()
                    if number not in cache
                    or cache[str(number)].updated_at < updated_at
                    or days_since(cache[str(number)].last_triaged) >= STALE_ISSUE_DAYS
                ]

        if not number_map["issues"] and not number_map["prs"]:
            return {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            for object_name, obj, data in (
                ("issue", Issue, number_map["issues"]),
                ("pullRequest", PR, number_map["prs"]),
            ):
                for number, updated_at in data:
                    futures.append(
                        executor.submit(
                            fetch_object,
                            number,
                            obj,
                            object_name,
                            updated_at,
                        )
                    )

            data = {}
            for future in concurrent.futures.as_completed(futures):
                obj = future.result()
                data[str(obj.number)] = obj

            cache.update(data)
            return data


def daemon(dry_run: t.Optional = None) -> None:
    global request_counter
    while True:
        request_counter = 0
        start = time.time()
        objs = fetch_objects()
        if objs:
            triage(objs, dry_run)
            with shelve.open(CACHE_FILENAME) as cache:
                for number, obj in objs.items():
                    obj.last_triaged = datetime.datetime.now(datetime.timezone.utc)
                    cache[str(number)] = obj
            logging.info(
                f"Took {time.time() - start:.2f} seconds to triage {len(objs)} issues/PRs"
                f" and {request_counter} HTTP requests"
            )
        else:
            logging.info("No new issues/PRs")
            logging.info(
                f"Took {time.time() - start:.2f} seconds to check for new issues/PRs"
                f" and {request_counter} HTTP requests"
            )
        logging.info(f"Sleeping for {SLEEP_SECONDS // 60} minutes")
        time.sleep(SLEEP_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ansibotmini",
        description="Triages github.com/ansible/ansible issues and PRs",
    )
    parser.add_argument("--number", help="Github issue or pull request number")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
        level=logging.DEBUG if args.debug else logging.INFO,
        stream=sys.stderr,
    )
    if args.number:
        obj = fetch_object_by_number(args.number)
        triage({args.number: obj}, dry_run=args.dry_run)
    else:
        daemon(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
