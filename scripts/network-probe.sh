#!/bin/sh
# Runs inside the VM image as root, via scripts/check-network.sh, with the same
# CAP_NET_ADMIN the provider grants. Applies the egress rules the way the
# entrypoint does, then checks what each user can reach. "allow" means the
# connection succeeded; "deny" means it did not.
export S3_ENDPOINT="${S3_ENDPOINT:-http://192.168.64.1:5050}"
fails=0
check() { # name, expect, then a command that exits 0 on success
  name=$1; expect=$2; shift 2
  if "$@" >/dev/null 2>&1; then got=allow; else got=deny; fi
  if [ "$got" = "$expect" ]; then status=PASS; else status=FAIL; fails=$((fails + 1)); fi
  printf '%s  %-44s expected %-5s got %s\n' "$status" "$name" "$expect" "$got"
}
as() { user=$1; shift; runuser -u "$user" -- "$@"; }
get() { curl -sS -o /dev/null --max-time 8 "$1"; }

echo "== applying the egress rules (as the entrypoint does)"
/opt/sandbox/venv/bin/python -m sandbox.vm_agent.egress || { echo "FAIL  egress rules did not apply"; exit 1; }

echo "== the job user (agent)"
check "agent -> https://github.com"                 allow as agent curl -sS -o /dev/null --max-time 8 https://github.com
check "agent can resolve names (DNS)"               allow as agent getent hosts example.com
check "agent -> https://example.com"                deny  as agent curl -sS -o /dev/null --max-time 8 https://example.com
check "agent -> http://1.1.1.1"                     deny  as agent curl -sS -o /dev/null --max-time 8 http://1.1.1.1
check "agent -> github.com on port 80 (not 443)"    deny  as agent curl -sS -o /dev/null --max-time 8 http://github.com
check "agent -> host gateway ssh"                   deny  as agent curl -sS -o /dev/null --max-time 5 telnet://192.168.64.1:22

echo "== everyone else is untouched"
check "root -> https://example.com"                 allow get https://example.com
check "sandbox-agent (the worker) -> example.com"   allow as sandbox-agent curl -sS -o /dev/null --max-time 8 https://example.com

echo "== the rules cannot be undone after the privilege drop"
check "agent changes the rules"                     deny  as agent nft add table inet evil
check "root without NET_ADMIN in its bounding set"  deny  setpriv --bounding-set=-net_admin nft delete table inet sandbox_egress
check "rules still in place afterwards"             allow nft list table inet sandbox_egress

if [ "$fails" -eq 0 ]; then echo "all network checks passed"; else echo "$fails network check(s) FAILED"; fi
exit "$fails"
