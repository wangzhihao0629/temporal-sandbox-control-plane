"""The network policy and the nftables ruleset it becomes. Enforcement itself is
checked on a real VM by `make check-network`; this checks what gets rendered."""

from sandbox.contract.network_policy import DEMO_NETWORK_POLICY, NetworkPolicy
from sandbox.vm_agent import egress

RESOLVED = {"github.com": ["140.82.112.3", "2606:50c0::1"]}


def _render(policy=DEMO_NETWORK_POLICY, resolved=RESOLVED, dns=("192.168.64.1",)):
    return egress.render(policy, 999, resolved, list(dns), ("192.168.64.1", 5050))


def test_demo_policy_allows_only_https_to_github():
    assert DEMO_NETWORK_POLICY.allow_hosts == ("github.com",)
    assert DEMO_NETWORK_POLICY.allow_ports == (443,)


def test_the_ruleset_only_binds_the_job_user_and_ends_in_reject():
    rules = _render()
    lines = [line.strip() for line in rules.splitlines()]
    assert "meta skuid != 999 accept" in lines
    assert lines.index("meta skuid != 999 accept") < lines.index("counter reject")
    assert lines[-3] == "counter reject", "reject must be the last rule in the chain"


def test_the_ruleset_opens_dns_the_object_store_and_the_allowed_hosts():
    rules = _render()
    assert "ip daddr { 192.168.64.1 } udp dport 53 accept" in rules
    assert "ip daddr 192.168.64.1 tcp dport 5050 accept" in rules
    assert "ip daddr { 140.82.112.3 } tcp dport { 443 } accept" in rules
    assert "ip6 daddr { 2606:50c0::1 } tcp dport { 443 } accept" in rules


def test_a_policy_with_no_hosts_emits_no_empty_sets():
    # nft rejects `{ }`, so an empty allowlist must drop the rule, not render it empty.
    rules = _render(policy=NetworkPolicy(allow_hosts=()), resolved={})
    assert "{  }" not in rules and "{ }" not in rules
    assert "tcp dport { 443 }" not in rules


def test_the_ruleset_replaces_its_own_table_idempotently():
    rules = _render()
    assert rules.startswith(
        "table inet sandbox_egress\ndelete table inet sandbox_egress\ntable inet sandbox_egress {"
    )


def test_env_override_replaces_the_host_list_and_empty_means_none():
    assert NetworkPolicy.from_env({}).allow_hosts == ("github.com",)
    env = {"SANDBOX_EGRESS_ALLOW_HOSTS": "github.com, proxy.golang.org"}
    override = NetworkPolicy.from_env(env)
    assert override.allow_hosts == ("github.com", "proxy.golang.org")
    assert NetworkPolicy.from_env({"SANDBOX_EGRESS_ALLOW_HOSTS": ""}).allow_hosts == ()


def test_nameservers_and_endpoint_parsing(tmp_path):
    conf = tmp_path / "resolv.conf"
    conf.write_text("# comment\nnameserver 192.168.64.1\noptions ndots:0\nnameserver 8.8.8.8\n")
    assert egress.nameservers(conf) == ["192.168.64.1", "8.8.8.8"]
    assert egress.endpoint("http://192.168.64.1:5050") == ("192.168.64.1", 5050)
    assert egress.endpoint("https://10.0.0.5") == ("10.0.0.5", 443)


def test_allowed_hosts_are_pinned_in_etc_hosts_replacing_any_earlier_pin():
    original = "127.0.0.1 localhost\n::1 localhost\n"
    once = egress.pin_hosts(original, {"github.com": ["140.82.116.3"]})
    assert once.startswith(original)
    assert "140.82.116.3 github.com\n" in once
    # Applying again (a restarted entrypoint) replaces the block, never stacks it.
    twice = egress.pin_hosts(once, {"github.com": ["140.82.116.4"]})
    assert "140.82.116.3" not in twice and "140.82.116.4 github.com\n" in twice
    assert twice.count("BEGIN sandbox egress") == 1
    assert twice.startswith(original)
