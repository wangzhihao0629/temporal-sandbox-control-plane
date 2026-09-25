#!/usr/bin/env bash
# Runs as root only long enough to own the state directories and install the job
# user's egress rules, then becomes sandbox-agent for good. `container run --init`
# gives the VM an init process that reaps zombies and forwards SIGTERM here, where
# the agent turns it into a drain.
set -euo pipefail
chown -R sandbox-agent:sandbox-agent /var/lib/sandbox /etc/sandbox/secrets
chown agent:agent /private/tmp/sandbox
chmod 2775 /private/tmp/sandbox
# The job user's egress rules go in while this is still root (the provider launches
# the VM with CAP_NET_ADMIN for exactly this). A failure here stops the VM from
# booting at all, rather than letting it boot with the network open.
/opt/sandbox/venv/bin/python -m sandbox.vm_agent.egress
# Dropping NET_ADMIN from the bounding set as well means nothing after this line
# can get it back, not even sudo's setuid-root step, so no job can undo the rules.
exec setpriv --reuid=sandbox-agent --regid=sandbox-agent --init-groups \
  --bounding-set=-net_admin \
  /opt/sandbox/venv/bin/python -m sandbox.vm_agent.worker
