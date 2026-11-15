from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import ssh, util
from .ssh import Device, DeviceError, NeverSent, NoAnswer

EXE_TAG = "arete"
EXE_BOOT_SECONDS = 120
GIT_FORGES = {"github": "GitHub", "gitlab": "GitLab", "bitbucket": "Bitbucket", "forgejo": "Forgejo", "gitea": "Gitea"}
GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "gitea.com", "sr.ht", "dev.azure.com",
             "visualstudio.com", "gitee.com")
READONLY_KINDS = ("github",)
PLAIN_NAME = re.compile(r"[\w.-]+")
FIND_GIT_CREDENTIALS = r"""
config=${XDG_CONFIG_HOME:-$HOME/.config}
{
  for f in ~/.git-credentials "$config/git/credentials" ~/.netrc "${GH_CONFIG_DIR:-$config/gh}/hosts.yml"; do
    test -s "$f" && echo "$f"
  done
  git config --get-regexp '^credential\..*helper$' >/dev/null 2>&1 && echo "a git credential.helper"
  git config --get-regexp '^url\..*\.insteadof$' 2>/dev/null | grep -qE 'https?://[^/ ]+@' &&
    echo "a git url.insteadOf that holds a credential"
  for v in GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN; do
    test -n "$(printenv $v)" && echo "\$$v"
  done
  grep -lE '(GH|GITHUB)(_ENTERPRISE)?_TOKEN=' ~/.profile ~/.bash_profile ~/.bash_login ~/.bashrc ~/.zshenv ~/.zprofile \
    ~/.zshrc /etc/environment /etc/profile /etc/bash.bashrc /etc/profile.d/* 2>/dev/null | sed 's/^/a GitHub token in /'
  test ! -e ~/.ssh || grep -rl "PRIVATE KEY" ~/.ssh 2>/dev/null || test $? = 1 || echo "~/.ssh (arete could not read all of it)"
} | sed "s|$HOME/|~/|g"
"""


def exe(d: Device, *command: str, timeout: float = 120) -> dict:
    return exe_reply(d, command, timeout, "an object", lambda reply: isinstance(reply, dict))


def exe_list(d: Device, *command: str) -> list[dict]:
    return exe_reply(d, command, 120, "a list of objects",
                     lambda reply: isinstance(reply, list) and all(isinstance(row, dict) for row in reply))


def exe_reply(d: Device, command: tuple[str, ...], timeout: float, shape: str, has_shape: Callable[[object], bool]):
    res = util.sh([*ssh.ssh_argv(d), shlex.join([*command, "--json"])], timeout=timeout)
    reply = json_reply(res.stdout)
    failed = f"{d.name}: `ssh {d.host} {command[0]}` failed: "
    if isinstance(reply, dict) and reply.get("error"):
        raise DeviceError(failed + util.one_line(str(reply["error"])))
    if has_shape(reply):
        return reply
    if reply is not None:
        raise DeviceError(f"{failed}its answer is not {shape}")
    never_sent = res.returncode == ssh.SSH_FAILED and ssh.SSH_FAILED_BEFORE_SENDING.search(res.stderr or "")
    raise (NeverSent if never_sent else NoAnswer)(failed + ssh.failure(res))


def json_reply(stdout: str):
    for text in (stdout, *stdout.strip().splitlines()[-1:]):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return None


@dataclass(frozen=True)
class Sandbox:
    device: Device
    name: str

    def __str__(self) -> str:
        return f"sandbox {self.name}"

    def delete_command(self) -> str:
        return f"`ssh {self.device.host} rm {self.name}`"

    def delete(self, log) -> str:
        try:
            reply = exe(self.device, "rm", self.name)
        except DeviceError as e:
            return f"could not delete {self}: {e}"
        if self.name in (reply.get("deleted") or []):
            log(f"deleted {self}")
            return ""
        if self.name in (reply.get("not_found") or []):
            return ""
        errors = reply.get("errors")
        why = errors.get(self.name) if isinstance(errors, dict) else errors
        return f"could not delete {self}: {util.one_line(str(why or 'exe.dev gave no reason'))}"


def wait_for_ssh(vm: Device) -> None:
    deadline = time.monotonic() + EXE_BOOT_SECONDS
    while True:
        res = util.sh([*ssh.ssh_argv(vm), "true"], timeout=30)
        if res.returncode == 0:
            return
        if time.monotonic() > deadline:
            raise DeviceError(f"{vm.host} did not answer ssh in {EXE_BOOT_SECONDS}s: {ssh.failure(res)}")
        time.sleep(2)


def boot_sandbox(sandbox: Sandbox, vm: Device) -> None:
    refuse_pushing_integrations(sandbox, sandbox_tags(sandbox))
    wait_for_ssh(vm)
    refuse_git_credentials(sandbox, vm)


def sandbox_tags(sandbox: Sandbox) -> list[str]:
    if not sandbox.device.template:
        return [EXE_TAG]
    tags = exe(sandbox.device, "tag", sandbox.name, EXE_TAG).get("tags")
    if isinstance(tags, list) and EXE_TAG in tags:
        return tags
    raise DeviceError(f"exe.dev did not say which tags {sandbox} carries, so arete cannot tell which integrations "
                      f"reach it")


def refuse_pushing_integrations(sandbox: Sandbox, tags: list[str]) -> None:
    for integration in exe_list(sandbox.device, "integrations", "ls"):
        if (push := how_it_pushes(integration)) and (through := reached_through(integration, sandbox, tags)):
            name = integration.get("name") or "NAME"
            fix = "attach it only to VMs you keep"
            if kind_of(integration) in READONLY_KINDS:
                fix = f"make it read-only with `ssh {sandbox.device.host} integrations edit {name} --readonly`, or {fix}"
            raise DeviceError(f"{sandbox} could {push}: the integration {name} reaches it through "
                              f"{', '.join(through)}; {fix}")


def how_it_pushes(integration: dict) -> str | None:
    kind = kind_of(integration)
    if kind in READONLY_KINDS and read_only(integration):
        return None
    if forge := next((GIT_FORGES[s] for s in services_of(integration) if s in GIT_FORGES), None):
        return f"push to {forge}"
    if kind == "http-proxy":
        return how_a_proxy_pushes(integration.get("target"))
    if kind == "ssh-key":
        return "push over ssh with a key exe.dev holds"
    if kind == "exe-dev":
        return "push through a GitHub integration it attaches to itself, using exe.dev as you"
    return None


def kind_of(integration: dict) -> str:
    return normal_name(integration.get("type"))


def services_of(integration: dict) -> set[str]:
    return {kind_of(integration), normal_name(integration.get("catalogService")),
            normal_name(integration_config(integration).get("service"))}


def normal_name(value) -> str:
    return re.sub(r"[\W_]+", "-", str(value or "").lower()).strip("-")


def how_a_proxy_pushes(target) -> str | None:
    if not target:
        return None
    host = proxied_host(target)
    if not host:
        return "push through a proxy target arete cannot read"
    if any(host == h or host.endswith(f".{h}") for h in GIT_HOSTS):
        return f"push to {host}"
    return None


def proxied_host(target) -> str | None:
    if not isinstance(target, str):
        return None
    try:
        host = urlsplit(target if "://" in target else f"//{target}").hostname
    except ValueError:
        return None
    return host.rstrip(".") if host else None


def reached_through(integration: dict, sandbox: Sandbox, tags: list[str]) -> list[str]:
    attachments = integration.get("attachments") or []
    if not isinstance(attachments, list) or not all(isinstance(a, str) for a in attachments):
        return ["attachments arete cannot read"]
    if not attachments and is_true(integration.get("isTeam")):
        return ["the team, which exe.dev lists no attachments for"]
    return sorted(a for a in attachments if reaches(a, sandbox.name, tags))


def reaches(attachment: str, vm_name: str, tags: list[str]) -> bool:
    kind, _, target = attachment.partition(":")
    if kind == "vm" and PLAIN_NAME.fullmatch(target):
        return target == vm_name
    if kind == "tag" and PLAIN_NAME.fullmatch(target):
        return target in tags
    return True


def read_only(integration: dict) -> bool:
    settings = {**integration, **integration_config(integration)}
    return any(is_any_spelling_of_readonly(key) and is_true(value) for key, value in settings.items())


def is_any_spelling_of_readonly(undocumented_key) -> bool:
    return normal_name(undocumented_key).replace("-", "") == "readonly"


def integration_config(integration: dict) -> dict:
    settings = integration.get("config")
    return settings if isinstance(settings, dict) else {}


def is_true(value) -> bool:
    return str(value).lower() == "true"


def refuse_git_credentials(sandbox: Sandbox, vm: Device) -> None:
    res = util.sh([*ssh.ssh_argv(vm), f"sh -c {shlex.quote(FIND_GIT_CREDENTIALS)}"], timeout=30)
    if res.returncode != 0:
        raise DeviceError(f"could not check {sandbox} for git credentials: {ssh.failure(res)}")
    if found := [line for line in res.stdout.splitlines() if line.strip()]:
        source = f"the template {sandbox.device.template}" if sandbox.device.template else "exe.dev's image"
        raise DeviceError(f"{sandbox} holds git credentials ({', '.join(found)}), so an agent there could push; "
                          f"take them off {source}")
