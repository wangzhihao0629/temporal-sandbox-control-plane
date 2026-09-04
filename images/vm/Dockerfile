# The VM image. Stands in for the Packer AMI: an OS, the toolchain jobs need,
# two users, the state directories, and the VM agent. Nothing in here knows
# about workflows.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip git sudo util-linux procps ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Two users. sandbox-agent runs the worker and owns the state directories.
# agent runs jobs and owns the workspace. The single sudo rule is the privilege
# boundary: the worker may become agent, agent can never become the worker.
RUN groupadd --system agent \
 && useradd --system --create-home --gid agent --shell /bin/bash agent \
 && groupadd --system sandbox-agent \
 && useradd --system --create-home --gid sandbox-agent --groups agent --shell /bin/bash sandbox-agent \
 && echo 'sandbox-agent ALL=(agent) NOPASSWD: ALL' > /etc/sudoers.d/sandbox-agent \
 && chmod 0440 /etc/sudoers.d/sandbox-agent

# /var/lib/sandbox is traversable so agent can read extracted artifacts, but
# the job records under jobs/ are the worker's alone. The workspace is setgid
# so files the worker drops in are group-readable by agent.
RUN install -d -m 0755 -o sandbox-agent -g sandbox-agent /var/lib/sandbox \
 && install -d -m 0700 -o sandbox-agent -g sandbox-agent /var/lib/sandbox/jobs \
 && install -d -m 0755 -o sandbox-agent -g sandbox-agent /var/lib/sandbox/artifacts \
 && install -d -m 0750 -o sandbox-agent -g sandbox-agent /etc/sandbox/secrets \
 && install -d -m 2775 -o agent -g agent /private/tmp/sandbox

COPY pyproject.toml /opt/sandbox/src/pyproject.toml
COPY sandbox /opt/sandbox/src/sandbox
RUN python3 -m venv /opt/sandbox/venv \
 && /opt/sandbox/venv/bin/pip install --no-cache-dir /opt/sandbox/src

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
