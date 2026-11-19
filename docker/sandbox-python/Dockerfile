# v1.6.2 D38: the image platform re-runs execute in. No compiler, no network at run time, not root.
# Update the base digest at each release, together with the main Dockerfile.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /tmp/sandbox-requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /tmp/sandbox-requirements.txt && rm /tmp/sandbox-requirements.txt

# The sandbox has no network and installs nothing, so pip is only attack surface.
RUN python -m pip uninstall -y pip && rm -rf /usr/local/lib/python3.12/ensurepip

# Third-party notices of exactly what ships; fails on an unreviewed copyleft package.
COPY third_party_notices.py /tmp/third_party_notices.py
RUN python /tmp/third_party_notices.py --image rewardhacking-sandbox-python --out /licenses/THIRD_PARTY_NOTICES.txt && \
    rm /tmp/third_party_notices.py

LABEL org.opencontainers.image.title="RewardHacking sandbox (Python)" \
      org.opencontainers.image.description="Image that RewardHacking platform re-runs execute in. Contains third-party open-source software: see /licenses/THIRD_PARTY_NOTICES.txt." \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/leo459028/RewardHacking"

USER 65534:65534
WORKDIR /workspace
