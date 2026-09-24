#!/bin/sh
# Runs inside the VM image, as root, via scripts/check-sudoers.sh. Each case runs
# `sudo -n -u agent -- <argv>` as sandbox-agent — the exact shape the VM agent
# uses — and checks that sudo allowed or refused it. A command that sudo allows
# but that then fails on its own (kill of a pid that doesn't exist) still counts
# as allowed: only sudo's own refusal counts as denied.
DIGEST=$(printf 'a%.0s' $(seq 64))
RUNNER_DIR=/var/lib/sandbox/artifacts/$DIGEST/bin
mkdir -p "$RUNNER_DIR" /private/tmp/sandbox
printf '#!/bin/sh\necho runner-ran "$@"\n' > "$RUNNER_DIR/runner"
chmod -R a+rX /var/lib/sandbox/artifacts
chown -R agent:agent /private/tmp/sandbox

fails=0
t() { # name, expect (allow|deny), argv...
  name=$1; expect=$2; shift 2
  out=$(runuser -u sandbox-agent -- sudo -n -u agent -- "$@" 2>&1)
  case "$out" in
    *"a password is required"*|*"is not allowed to"*|*"not allowed to execute"*) got=deny ;;
    *) got=allow ;;
  esac
  if [ "$got" = "$expect" ]; then status=PASS; else status=FAIL; fails=$((fails + 1)); fi
  printf '%s  %-34s expected %-5s got %s\n' "$status" "$name" "$expect" "$got"
}

echo "== what a workflow may ask a VM to run (SANDBOX_JOBS)"
t "runner clone"              allow /bin/sh "$RUNNER_DIR/runner" clone --workspace /private/tmp/sandbox/x
t "runner export"             allow /bin/sh "$RUNNER_DIR/runner" export --workspace /private/tmp/sandbox/x
t "uname -a"                  allow uname -a
t "id"                        allow id
t "sleep 1"                   allow sleep 1

# The agent hands a job its environment with --preserve-env (JobStore._wrap), and
# sudo refuses that for any command not tagged SETENV, so it is checked separately.
te() { # name, expect, then the sudo arguments after `-u agent`
  name=$1; expect=$2; shift 2
  out=$(FOO=1 BAR=2 runuser -u sandbox-agent -- sudo -n -u agent --preserve-env=FOO,BAR -- "$@" 2>&1)
  case "$out" in
    *"a password is required"*|*"is not allowed to"*|*"not allowed to execute"*|*"not allowed to set"*) got=deny ;;
    *) got=allow ;;
  esac
  if [ "$got" = "$expect" ]; then status=PASS; else status=FAIL; fails=$((fails + 1)); fi
  printf '%s  %-34s expected %-5s got %s\n' "$status" "$name" "$expect" "$got"
}
te "runner with --preserve-env"   allow /bin/sh "$RUNNER_DIR/runner" clone --workspace /private/tmp/sandbox/x
te "uname with --preserve-env"    allow uname -a
te "housekeeping + --preserve-env" deny  mkdir -p /private/tmp/sandbox/wf-2

echo "== what the VM agent runs itself (SANDBOX_HOUSEKEEPING)"
t "mkdir -p workspace dir"    allow mkdir -p /private/tmp/sandbox/wf-1
t "kill -TERM -- -pgid"       allow kill -TERM -- -999999
t "kill -KILL -- -pgid"       allow kill -KILL -- -999999
t "pgrep -g pgid"             allow pgrep -g 999999
t "pkill -KILL -u agent"      allow pkill -KILL -u agent
t "find ... rm -rf (wipe)"    allow find /private/tmp/sandbox -mindepth 1 -maxdepth 1 -exec rm -rf {} +

echo "== everything else is refused"
t "rm -rf"                    deny  rm -rf /private/tmp/sandbox
t "sh -c arbitrary"           deny  /bin/sh -c id
t "sh on another script"      deny  /bin/sh /tmp/evil.sh
t "bash -c"                   deny  /bin/bash -c id
t "curl"                      deny  curl http://example.com
t "uname -r (only -a is ok)"  deny  uname -r
t "kill with no group"        deny  kill -KILL 1
t "pkill another user"        deny  pkill -KILL -u root
t "find with another -exec"   deny  find / -maxdepth 1 -exec cat {} +
t "mkdir outside workspace"   deny  mkdir -p /etc/evil
t "cat /etc/shadow"           deny  cat /etc/shadow

if [ "$fails" -eq 0 ]; then echo "all sudoers checks passed"; else echo "$fails sudoers check(s) FAILED"; fi
exit "$fails"
