# OnionAccelerator's Tor farm as a container: the process backend, supervised by --farm run.
#
#   docker build -t onionaccelerator .
#   docker run -d --name oa-farm --network host -e OA_COUNT=8 onionaccelerator
#
# The SOCKS ports (127.0.0.1:OA_BASE_PORT ...) are bound to the container's loopback,
# which is the host's loopback under --network host and nobody else's. Publishing them
# with -p would not work, and would be the wrong thing to want: a SOCKS proxy that
# answers on an interface is an open relay into Tor for whoever can reach it.
FROM python:3.13-slim

# tor from the distribution. Onion-service proof-of-work needs tor >= 0.4.8 built with the
# pow module; the farm warns at start-up if this one lacks it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/onionaccelerator
COPY pyproject.toml README.md LICENSE OnionAccelerator.py fullhelp.py ./
COPY oacore ./oacore
COPY crawler ./crawler
COPY eventstream ./eventstream
COPY remote_viewer/rvtree ./remote_viewer/rvtree
COPY remote_viewer/pyproject.toml ./remote_viewer/pyproject.toml
RUN pip install --no-cache-dir .

# Not root: the process backend needs no privilege, and a tor that can only write its own
# state directory is a smaller thing to get wrong.
RUN useradd --create-home --uid 10001 oa
USER oa
ENV OA_COUNT=8 OA_BASE_PORT=5000
VOLUME ["/home/oa/.local/state/onionaccelerator"]

# exec form through sh -c only so the two settings expand; `exec` keeps onionaccelerator as
# PID 1's child-less self, so SIGTERM from `docker stop` reaches --farm run directly and
# it shuts the instances down before the container exits.
ENTRYPOINT ["sh", "-c", "exec onionaccelerator --farm run --count \"$OA_COUNT\" --base-port \"$OA_BASE_PORT\" --bootstrap-timeout 300"]
