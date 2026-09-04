#!/usr/bin/env bash
# Runs as root only long enough to own the state directories, then becomes
# sandbox-agent for good. `container run --init` gives the VM an init process
# that reaps zombies and forwards SIGTERM here, where the agent turns it into
# a drain.
set -euo pipefail
chown -R sandbox-agent:sandbox-agent /var/lib/sandbox /etc/sandbox/secrets
chown agent:agent /private/tmp/sandbox
chmod 2775 /private/tmp/sandbox
exec setpriv --reuid=sandbox-agent --regid=sandbox-agent --init-groups \
  /opt/sandbox/venv/bin/python -m sandbox.vm_agent.worker
