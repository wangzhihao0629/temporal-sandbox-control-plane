# The VM image. Stands in for the Packer AMI: an OS, the toolchain jobs need,
# two users, the state directories, and the VM agent. Nothing in here knows
# about workflows.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip git sudo util-linux procps ca-certificates curl \
      golang-go nftables \
    && rm -rf /var/lib/apt/lists/*

# Two users. sandbox-agent runs the worker and owns the state directories.
# agent runs jobs and owns the workspace. The sudo rule is the privilege
# boundary: the worker may become agent, agent can never become the worker —
# and only to run the commands listed in sandbox-agent.sudoers, not anything
# else. visudo fails the build on a syntax error; `make check-sudoers` checks
# what the rules actually allow and refuse.
RUN groupadd --system agent \
 && useradd --system --create-home --gid agent --shell /bin/bash agent \
 && groupadd --system sandbox-agent \
 && useradd --system --create-home --gid sandbox-agent --groups agent --shell /bin/bash sandbox-agent
COPY images/vm/sandbox-agent.sudoers /etc/sudoers.d/sandbox-agent
RUN chmod 0440 /etc/sudoers.d/sandbox-agent \
 && visudo -c -f /etc/sudoers.d/sandbox-agent

# /var/lib/sandbox is traversable so agent can read extracted artifacts, but
# the job records under jobs/ are the worker's alone. The workspace is setgid
# so files the worker drops in are group-readable by agent.
RUN install -d -m 0755 -o sandbox-agent -g sandbox-agent /var/lib/sandbox \
 && install -d -m 0700 -o sandbox-agent -g sandbox-agent /var/lib/sandbox/jobs \
 && install -d -m 0755 -o sandbox-agent -g sandbox-agent /var/lib/sandbox/artifacts \
 && install -d -m 0750 -o sandbox-agent -g sandbox-agent /etc/sandbox/secrets \
 && install -d -m 2775 -o agent -g agent /private/tmp/sandbox

# Dependencies come from the lockfile scripts/build-image.sh exports, and the
# sandbox package is installed with --no-deps on top, so pip never resolves a
# version the tests have not seen.
COPY pyproject.toml /opt/sandbox/src/pyproject.toml
COPY images/vm/requirements.txt /opt/sandbox/src/requirements.txt
COPY sandbox /opt/sandbox/src/sandbox
RUN python3 -m venv /opt/sandbox/venv \
 && /opt/sandbox/venv/bin/pip install --no-cache-dir -r /opt/sandbox/src/requirements.txt \
 && /opt/sandbox/venv/bin/pip install --no-cache-dir --no-deps /opt/sandbox/src

# Seed repositories the demo clones from, so a session works offline. Built by
# the same code the tests use, so the image and the tests agree on the seed.
# Git refuses to read a repository owned by another user (its safe.directory
# check), so permissions alone are not enough — the seed is handed to agent,
# the user that actually clones it.
COPY images/vm/seed /opt/sandbox/seed
RUN /opt/sandbox/venv/bin/python -m sandbox.runner.seed /opt/sandbox/seed /srv/repos \
 && chmod -R a+rX /srv/repos \
 && chown -R agent:agent /srv/repos

# Dummy secrets so ExecSpec.secrets=["github_token"] resolves locally. In
# production these are written by the host's token refresher.
RUN printf 'dummy-github-token\n' > /etc/sandbox/secrets/github_token \
 && printf 'dummy-llm-key\n' > /etc/sandbox/secrets/llm_gateway \
 && chown sandbox-agent:sandbox-agent /etc/sandbox/secrets/* \
 && chmod 0600 /etc/sandbox/secrets/*

COPY images/vm/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

ENV SANDBOX_RUN_AS_USER=agent
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
